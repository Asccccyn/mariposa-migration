"""v2 生成→林石见审查→生效/留 状态机（spec_v2 §8 / R13-R16）。

权限分离（§8.1）：
- 后台 worker/API 模型：只能 generate/draft 候选，无正式写入权；
- 林石见（linshijian）：凭 review_delegations 受限委托，只可改候选
  summary_body 与 forget_tags，放行"确定到期且无疑点"项，报留/疑难；
- 乔生/周家明：memory.retention.decide 终裁 keep/continue/defer；
  共同话语 needs_jiaming_decision 限定周家明；
- 任何原始字段修改在审查路径一律拒绝，不是忽略。

状态机：generating → generated → in_review → ready_to_apply → forgotten
        in_review → needs_owner_decision / needs_jiaming_decision
        owner_decision → retained | forgotten | deferred
        未执行阶段 → stale | withdrawn | failed
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .. import audit as audit_mod
from .. import config, db
from ..errors import Forbidden, NotFound, ProposalStale
from ..memory import categories as cats_mod
from ..memory import our_words as ow_mod
from ..memory import recollections as rec_mod
from ..memory import retention as ret_mod
from ..memory import service as memory
from ..retrieval import projection

REVIEWER = "linshijian"
GENERATORS = {"worker", "qiaosheng", "jiaming"}
OWNERS = {"qiaosheng", "jiaming"}

#: 审查者可修改的字段白名单（R13）；其余一律拒绝
REVIEWABLE_FIELDS = {"summary_body", "forget_tags"}

#: 摘要禁用概括称谓/措辞（V2-REV-09）；原文含这些词不受影响
BANNED_SUMMARY_TERMS = ("用户", "AI", "助手", "角色扮演", "角色", "扮演",
                        "模拟", "互动")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_default_delegation() -> None:
    """开发默认委托：林石见可改候选摘要/tags、放行无疑点到期项、上报。
    生产替换为显式签发的 delegation 记录（valid_from/valid_to/scope）。
    """
    with db.formal() as conn:
        row = conn.execute(
            "SELECT delegation_id FROM review_delegations WHERE"
            " reviewed_principal=? AND revoked=0",
            (REVIEWER,)).fetchone()
        if row:
            return
        conn.execute(
            "INSERT INTO review_delegations(delegation_id, reviewed_principal,"
            " allowed_actions, resource_scope, valid_from, valid_to, revoked)"
            " VALUES(?,?,?,?,?,NULL,0)",
            (f"dlg_{uuid.uuid4().hex[:10]}", REVIEWER,
             json.dumps(["revise_summary", "revise_tags", "release_clean",
                         "escalate_retain", "escalate_owner"]),
             "forget_review", _now()))


def _require_reviewer(principal) -> None:
    if principal.principal_id != REVIEWER:
        raise Forbidden("restricted review is delegated to linshijian only",
                        principal=principal.principal_id)
    with db.formal() as conn:
        row = conn.execute(
            "SELECT 1 FROM review_delegations WHERE reviewed_principal=?"
            " AND revoked=0 AND (valid_to IS NULL OR valid_to>?)",
            (REVIEWER, _now())).fetchone()
    if not row:
        raise Forbidden("no active review delegation", principal=REVIEWER)


def _fields_hash(conn, memory_id: str) -> str:
    m = conn.execute("SELECT memory_date, current_version_no FROM memories"
                     " WHERE memory_id=?", (memory_id,)).fetchone()
    v = conn.execute(
        "SELECT original_title, event_text, hold_text FROM memory_versions"
        " WHERE memory_id=? AND version_no=?",
        (memory_id, m["current_version_no"])).fetchone()
    r = ret_mod.get(conn, memory_id)
    payload = {
        "version": m["current_version_no"],
        "title": v["original_title"],
        "text": v["event_text"] or v["hold_text"],
        "categories": cats_mod.list_of(conn, memory_id),
        "retention_revision": r["retention_revision"] if r else None,
        "policy_version": ret_mod.POLICY_VERSION,
    }
    return memory.canonical_hash(payload)


def _collect_retain_hints(conn, memory_id: str) -> list[dict]:
    """保留线索（D06/R14）：只标疑似，附字段与位置，不机械定留。"""
    hints: list[dict] = []
    words = ow_mod.list_for(memory_id)
    for h in ow_mod.has_shared_expression(words):
        h["decision_owner"] = "jiaming"  # 共同话语终裁限定周家明
        hints.append(h)
    if rec_mod.list_for(memory_id):
        hints.append({"kind": "has_recollections",
                      "decision_owner": "owner"})
    v = conn.execute(
        "SELECT original_title, event_text, hold_text FROM memory_versions"
        " WHERE memory_id=? AND version_no=(SELECT current_version_no FROM"
        " memories WHERE memory_id=?)", (memory_id, memory_id)).fetchone()
    for field, text in (("original_title", v["original_title"]),
                        ("event_text", (v["event_text"] or v["hold_text"] or ""))):
        if not text:
            continue
        for kw in ("第一次", "留"):
            idx = text.find(kw)
            if idx >= 0:
                start = max(0, idx - 8)
                hints.append({
                    "kind": "keyword_retain_hint",
                    "keyword": kw,
                    "field": field,
                    "position": idx,
                    "context": text[start:idx + len(kw) + 8],
                    "decision_owner": "owner"})
    return hints


def _check_summary_terms(summary_body: str) -> None:
    for term in BANNED_SUMMARY_TERMS:
        if term in summary_body:
            raise Forbidden(
                f"候选摘要含禁用概括措辞：{term}（V2-REV-09）；改写或转疑难",
                code="SUMMARY_TERM_BANNED", term=term)


def generate(principal, memory_id: str | None = None,
             candidate_summary: str = "",
             candidate_tags: list[str] | None = None,
             from_queue: bool = True) -> dict:
    """从到期队列/指定桶生成审查项（worker 等生成者；无正式写入权）。

    未配置外部 provider 时接受调用方带入的候选稿（本地/合成流程）；
    没有候选稿的项停在 generated，等待草稿。
    """
    if principal.principal_id not in GENERATORS:
        raise Forbidden("generators only", principal=principal.principal_id)
    targets: list[str] = []
    if memory_id:
        targets = [memory_id]
    elif from_queue:
        from . import due_queue
        today = ret_mod.business_today().isoformat()
        targets = [i["target_id"] for i in due_queue.due_items(today)
                   if i["target_kind"] == "memory"]
    if not targets:
        return {"created": [], "note": "无到期目标（due queue 为空）"}
    if candidate_summary.strip():
        _check_summary_terms(candidate_summary)
    created = []
    now = _now()
    for mid in targets:
        with db.formal() as conn:
            m = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                             (mid,)).fetchone()
            if m is None:
                continue
            r = ret_mod.get(conn, mid)
            if r and r["status"] == "retained":
                # RET-11：确定留是终局，不再周期送审；显式拒绝而非静默跳过
                raise Forbidden(
                    "确定留的桶不再进入自动遗忘审查（终局）",
                    code="RETAINED_FINAL", memory_id=mid)
            if not ret_mod.is_due(conn, mid):
                continue  # 只有真正到期（business_today >= due_date）才生成
            with db.workspace() as wconn:
                exists = wconn.execute(
                    "SELECT 1 FROM v2_review_items WHERE target_id=? AND"
                    " state NOT IN ('forgotten','retained','withdrawn',"
                    " 'stale','failed')", (mid,)).fetchone()
            if exists:
                continue
            item_id = f"rev_{uuid.uuid4().hex[:12]}"
            state = "in_review" if candidate_summary.strip() else "generated"
            hints = json.dumps(_collect_retain_hints(conn, mid),
                               ensure_ascii=False)
            if hints != "[]" and state == "in_review":
                state = "in_review"  # 疑点项同样可领取，但 release 会被拦
            with db.workspace() as wconn:
                wconn.execute("BEGIN IMMEDIATE")
                try:
                    wconn.execute(
                        "INSERT INTO v2_review_items(item_id, target_kind,"
                        " target_id, state, current_revision, generated_summary,"
                        " generated_tags, source_version, source_fields_hash,"
                        " retention_revision, policy_version, created_by,"
                        " retain_hints, created_at, updated_at)"
                        " VALUES(?,?,?,?,1,?,?,?,?,?,?,?, ?,?,?)",
                        (item_id, "memory", mid, state,
                         candidate_summary.strip(),
                         json.dumps(candidate_tags or [], ensure_ascii=False),
                         m["current_version_no"], _fields_hash(conn, mid),
                         (ret_mod.get(conn, mid) or {}).get("retention_revision"),
                         ret_mod.POLICY_VERSION, principal.principal_id,
                         hints, now, now))
                    if candidate_summary.strip():
                        wconn.execute(
                            "INSERT INTO v2_proposal_versions(item_id, revision,"
                            " summary_body, forget_tags, created_by, created_at)"
                            " VALUES(?,1,?,?,?,?)",
                            (item_id, candidate_summary.strip(),
                             json.dumps(candidate_tags or [],
                                        ensure_ascii=False),
                             principal.principal_id, now))
                    wconn.execute("COMMIT")
                except Exception:
                    wconn.execute("ROLLBACK")
                    raise
            created.append({"item_id": item_id, "target_id": mid,
                            "state": state})
    return {"created": created}


def draft(principal, item_id: str, summary_body: str,
          forget_tags: list[str] | None = None) -> dict:
    """生成者补候选稿（generated → in_review）。仍无正式写入权。"""
    if principal.principal_id not in GENERATORS:
        raise Forbidden("generators only", principal=principal.principal_id)
    _check_summary_terms(summary_body)
    now = _now()
    with db.workspace() as wconn:
        item = _item(wconn, item_id)
        if item["state"] != "generated":
            raise Forbidden("only generated items accept drafts",
                            state=item["state"])
        rev = item["current_revision"] + 1
        wconn.execute("BEGIN IMMEDIATE")
        try:
            wconn.execute(
                "INSERT INTO v2_proposal_versions(item_id, revision,"
                " summary_body, forget_tags, created_by, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (item_id, rev, summary_body,
                 json.dumps(forget_tags or [], ensure_ascii=False),
                 principal.principal_id, now))
            wconn.execute(
                "UPDATE v2_review_items SET current_revision=?,"
                " generated_summary=?, generated_tags=?, state='in_review',"
                " updated_at=? WHERE item_id=?",
                (rev, summary_body,
                 json.dumps(forget_tags or [], ensure_ascii=False),
                 now, item_id))
            wconn.execute("COMMIT")
        except Exception:
            wconn.execute("ROLLBACK")
            raise
    return {"item_id": item_id, "revision": rev, "state": "in_review"}


def _item(wconn, item_id: str):
    row = wconn.execute("SELECT * FROM v2_review_items WHERE item_id=?",
                        (item_id,)).fetchone()
    if row is None:
        raise NotFound("review item not found", item_id=item_id)
    return row


def claim(principal) -> dict:
    """林石见领取最早的待审项。"""
    _require_reviewer(principal)
    now = _now()
    with db.workspace() as wconn:
        wconn.execute("BEGIN IMMEDIATE")
        try:
            row = wconn.execute(
                "SELECT item_id FROM v2_review_items WHERE state='in_review'"
                " AND (claimed_by IS NULL OR claimed_by=?)"
                " ORDER BY created_at, item_id LIMIT 1",
                (principal.principal_id,)).fetchone()
            if row is None:
                conn_empty = True
            else:
                wconn.execute(
                    "UPDATE v2_review_items SET claimed_by=?, claimed_at=?,"
                    " updated_at=? WHERE item_id=?",
                    (principal.principal_id, now, now, row["item_id"]))
                conn_empty = False
            wconn.execute("COMMIT")
        except Exception:
            wconn.execute("ROLLBACK")
            raise
    if conn_empty:
        return {"item_id": None, "note": "队列为空；审查者未在线时等待"}
    return get_item(principal, row["item_id"])


def get_item(principal, item_id: str) -> dict:
    """审查材料：原标题/正文/心情/分类 + 生成稿与最新修订 + 线索 + 到期信息。"""
    if principal.principal_id not in ({REVIEWER} | OWNERS | GENERATORS):
        raise Forbidden("not allowed to read review items",
                        principal=principal.principal_id)
    with db.workspace() as wconn:
        item = _item(wconn, item_id)
        versions = wconn.execute(
            "SELECT revision, summary_body, forget_tags, created_by,"
            " created_at FROM v2_proposal_versions WHERE item_id=?"
            " ORDER BY revision", (item_id,)).fetchall()
    with db.formal() as conn:
        mem = memory.get(conn, item["target_id"])
        ret = ret_mod.get(conn, item["target_id"])
    first = json.loads(versions[0]["forget_tags"]) if versions else []
    latest = json.loads(versions[-1]["forget_tags"]) if versions else []
    return {
        "item_id": item_id, "target_id": item["target_id"],
        "state": item["state"], "revision": item["current_revision"],
        "claimed_by": item["claimed_by"],
        "original_title": mem.get("original_title"),
        "event_text": mem.get("text") if mem.get("representation") == "full" else None,
        "current_summary": (mem.get("text") if mem.get("representation")
                            == "forgotten_summary" else None),
        "mood": mem.get("mood"),
        "categories": mem.get("categories", []),
        "due_date": (ret or {}).get("due_date"),
        "retention_revision": (ret or {}).get("retention_revision"),
        "retain_hints": json.loads(item["retain_hints"]),
        "generated": {"summary_body": item["generated_summary"],
                      "forget_tags": json.loads(item["generated_tags"])},
        "versions": [dict(v) for v in versions],
        "tags_diff": {"first": first, "latest": latest,
                      "changed": first != latest},
        "note": "原始字段只读；仅可改 summary_body/forget_tags",
    }


def revise(principal, item_id: str, changes: dict) -> dict:
    """林石见修订：仅接受 summary_body / forget_tags；其余字段一律拒绝。"""
    _require_reviewer(principal)
    if not isinstance(changes, dict) or not changes:
        raise Forbidden("changes required", code="INVALID_ARGUMENT")
    extra = set(changes) - REVIEWABLE_FIELDS
    if extra:
        # V2-REV-03：拒绝而不是忽略
        raise Forbidden(
            f"审查者不可修改原始字段：{sorted(extra)}；只允许 "
            f"{sorted(REVIEWABLE_FIELDS)}",
            code="FIELD_NOT_REVIEWABLE", fields=sorted(extra))
    if "summary_body" in changes:
        _check_summary_terms(changes["summary_body"])
    now = _now()
    with db.workspace() as wconn:
        item = _item(wconn, item_id)
        if item["state"] not in ("in_review",):
            raise Forbidden("item not in review", state=item["state"])
        if item["claimed_by"] != principal.principal_id:
            raise Forbidden("item not claimed by this reviewer")
        prev = wconn.execute(
            "SELECT summary_body, forget_tags FROM v2_proposal_versions"
            " WHERE item_id=? AND revision=?",
            (item_id, item["current_revision"])).fetchone()
        summary = changes.get("summary_body",
                              prev["summary_body"] if prev
                              else item["generated_summary"])
        tags = changes.get("forget_tags",
                           json.loads(prev["forget_tags"]) if prev
                           else json.loads(item["generated_tags"]))
        if isinstance(tags, list):
            tags = [str(t) for t in tags]
        else:
            raise Forbidden("forget_tags must be a list",
                            code="INVALID_ARGUMENT")
        rev = item["current_revision"] + 1
        wconn.execute("BEGIN IMMEDIATE")
        try:
            wconn.execute(
                "INSERT INTO v2_proposal_versions(item_id, revision,"
                " summary_body, forget_tags, created_by, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (item_id, rev, summary,
                 json.dumps(tags, ensure_ascii=False),
                 principal.principal_id, now))
            wconn.execute(
                "UPDATE v2_review_items SET current_revision=?, updated_at=?"
                " WHERE item_id=?", (rev, now, item_id))
            wconn.execute(
                "INSERT INTO workspace_audit(event_id, occurred_at, actor,"
                " action, item_id, detail) VALUES(?,?,?,?,?,?)",
                (f"evt_{uuid.uuid4().hex[:16]}", now,
                 principal.principal_id, "review.revised", item_id,
                 json.dumps(sorted(changes), ensure_ascii=False)))
            wconn.execute("COMMIT")
        except Exception:
            wconn.execute("ROLLBACK")
            raise
    return {"item_id": item_id, "revision": rev,
            "summary_body": summary, "forget_tags": tags}


def _reverify(conn, item) -> None:
    """执行前再核验（§8.4）：到期、源版本、字段hash、实时保留线索。"""
    mid = item["target_id"]
    m = conn.execute("SELECT current_version_no FROM memories WHERE"
                     " memory_id=?", (mid,)).fetchone()
    if m is None:
        raise ProposalStale("target memory missing", item_id=item["item_id"])
    if m["current_version_no"] != item["source_version"]:
        raise ProposalStale(
            "source content version moved",
            expected=item["source_version"], current=m["current_version_no"])
    if _fields_hash(conn, mid) != item["source_fields_hash"]:
        raise ProposalStale("source fields changed (title/text/categories/"
                            "retention)", item_id=item["item_id"])
    if not ret_mod.is_due(conn, mid):
        raise Forbidden("not due anymore (opened/renewed/category changed)",
                        code="NOT_DUE")
    # 线索实时重收（生成后新写入的回忆/话语同样要拦，不只看快照）
    if _collect_retain_hints(conn, mid):
        raise Forbidden("保留线索未裁决；转 owner/jiaming 决定",
                        code="RETAIN_HINT_PENDING")


def _apply_forget(conn, item, summary: str, tags: list[str],
                  applied_by: str) -> dict:
    """正式生效（正式库事务内）：新表示版本 + 摘要版本 + 投影 + 队列。"""
    mid = item["target_id"]
    m = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                     (mid,)).fetchone()
    new_version = m["current_version_no"] + 1
    now = _now()
    version_payload = {"representation": "forgotten_summary",
                       "compressed_summary": summary,
                       "proposal_id": item["item_id"]}
    conn.execute(
        "INSERT INTO memory_versions(memory_id, version_no, representation,"
        " hold_text, compressed_summary, why_remember, authored_by,"
        " confirmed_by, origin_kind, payload_hash, created_at)"
        " VALUES(?,?,'forgotten_summary',NULL,?,NULL,'worker',?,"
        "'forget_approval',?,?)",
        (mid, new_version, summary, applied_by,
         memory.canonical_hash(version_payload), now))
    conn.execute(
        "UPDATE memories SET current_version_no=?,"
        " compression_state='forgotten_summary',"
        " representation_state=representation_state+1, updated_at=?"
        " WHERE memory_id=?", (new_version, now, mid))
    # 索引只取 summary_body（+审查后 tags；标题永不进索引）
    projection.upsert(conn, mid, new_version, "forgotten_summary",
                      projection.build_forgotten(
                          summary + "\n" + " ".join(tags)))
    sv = conn.execute(
        "SELECT COALESCE(MAX(summary_version),0)+1 AS n FROM"
        " memory_summary_versions WHERE memory_id=?",
        (mid,)).fetchone()["n"]
    conn.execute(
        "INSERT INTO memory_summary_versions(memory_id, summary_version,"
        " summary_body, forget_tags, source_version, source_hash,"
        " proposal_id, applied_at, applied_by) VALUES(?,?,?,?,?,?,?,?,?)",
        (mid, sv, summary, json.dumps(tags, ensure_ascii=False),
         item["source_version"], item["source_fields_hash"],
         item["item_id"], now, applied_by))
    audit_mod.record(conn, "memory.forgotten", applied_by,
                     resource_id=mid, resource_version=new_version,
                     payload={"proposal_id": item["item_id"],
                              "v2_review": True, "reversible": True})
    # 队列完成：直接用本事务连接，不再开新连接（避免持锁嵌套连接死锁）
    conn.execute(
        "UPDATE forgetting_due_queue SET status='done', lease_id=NULL,"
        " updated_at=? WHERE target_kind='memory' AND target_id=? AND"
        " status IN ('pending','leased','failed')", (now, mid))
    return {"memory_id": mid, "new_version": new_version,
            "summary_version": sv}


def submit(principal, item_id: str, decision: str,
           expected_revision: int, expected_hash: str | None = None) -> dict:
    """林石见提交：release（放行干净到期项）或 escalate（转终裁）。"""
    _require_reviewer(principal)
    if decision not in ("release", "escalate_retain", "escalate_owner",
                        "escalate_jiaming"):
        raise Forbidden("decision must be release/escalate_retain/"
                        "escalate_owner/escalate_jiaming")
    with db.workspace() as wconn:
        item = _item(wconn, item_id)
        if item["state"] != "in_review":
            raise Forbidden("item not in review", state=item["state"])
        if item["claimed_by"] != principal.principal_id:
            raise Forbidden("item not claimed by this reviewer")
        if expected_revision != item["current_revision"]:
            raise ProposalStale("revision moved",
                                expected=expected_revision,
                                current=item["current_revision"])
        vrow = wconn.execute(
            "SELECT summary_body, forget_tags FROM v2_proposal_versions"
            " WHERE item_id=? AND revision=?",
            (item_id, item["current_revision"])).fetchone()
        if vrow is None:
            raise Forbidden("no candidate version to release")
        payload_hash = memory.canonical_hash(
            {"summary_body": vrow["summary_body"],
             "forget_tags": vrow["forget_tags"],
             "source_version": item["source_version"],
             "source_fields_hash": item["source_fields_hash"]})
        if expected_hash and expected_hash != payload_hash:
            raise ProposalStale("candidate hash mismatch",
                                expected=expected_hash,
                                current=payload_hash)
        summary = vrow["summary_body"]
        tags = json.loads(vrow["forget_tags"])
    _check_summary_terms(summary)

    now = _now()
    if decision == "release":
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _reverify(conn, item)  # 到期+版本+hash+无疑点，一步不缺
                result = _apply_forget(conn, item, summary, tags,
                                       principal.principal_id)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        new_state = "forgotten"
        detail = {"decision": "release", **result}
    else:
        result = {}
        new_state = {"escalate_retain": "needs_owner_decision",
                     "escalate_owner": "needs_owner_decision",
                     "escalate_jiaming": "needs_jiaming_decision"}[decision]
        detail = {"decision": decision}
    with db.workspace() as wconn:
        wconn.execute(
            "UPDATE v2_review_items SET state=?, updated_at=? WHERE item_id=?",
            (new_state, now, item_id))
        wconn.execute(
            "INSERT INTO workspace_audit(event_id, occurred_at, actor, action,"
            " item_id, detail) VALUES(?,?,?,?,?,?)",
            (f"evt_{uuid.uuid4().hex[:16]}", now, principal.principal_id,
             f"review.{decision}", item_id,
             json.dumps(detail, ensure_ascii=False)))
    return {"item_id": item_id, "state": new_state, **detail}


def decide_retention(principal, item_id: str, decision: str) -> dict:
    """终裁（R14/R15）：keep=确定留终局；continue=现在生效；defer=挂起。

    needs_jiaming_decision（共同话语）只有周家明能裁。
    """
    pid = principal.principal_id
    if pid not in OWNERS:
        raise Forbidden("only qiaosheng/jiaming decide retention",
                        principal=pid)
    if decision not in ("keep", "continue", "defer"):
        raise Forbidden("decision must be keep/continue/defer")
    with db.workspace() as wconn:
        item = _item(wconn, item_id)
        if item["state"] not in ("needs_owner_decision",
                                 "needs_jiaming_decision", "deferred"):
            raise Forbidden("item awaits owner decision", state=item["state"])
        if item["state"] == "needs_jiaming_decision" and pid != "jiaming":
            raise Forbidden("共同话语的终裁限定周家明", principal=pid)
    now = _now()
    result: dict = {}
    if decision == "keep":
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ret_mod.mark_retained(conn, item["target_id"],
                                      "owner_decision", pid)
                audit_mod.record(conn, "memory.retention.retained", pid,
                                 resource_id=item["target_id"],
                                 payload={"item_id": item_id})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        new_state = "retained"
    elif decision == "continue":
        vrow = None
        with db.workspace() as wconn:
            vrow = wconn.execute(
                "SELECT summary_body, forget_tags FROM v2_proposal_versions"
                " WHERE item_id=? AND revision=?",
                (item_id, item["current_revision"])).fetchone()
        if vrow is None:
            raise Forbidden("no candidate to apply")
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                m = conn.execute(
                    "SELECT current_version_no FROM memories WHERE"
                    " memory_id=?", (item["target_id"],)).fetchone()
                if m["current_version_no"] != item["source_version"]:
                    raise ProposalStale("source version moved")
                result = _apply_forget(conn, item, vrow["summary_body"],
                                       json.loads(vrow["forget_tags"]), pid)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        new_state = "forgotten"
    else:
        result = {}
        new_state = "deferred"
    with db.workspace() as wconn:
        wconn.execute(
            "UPDATE v2_review_items SET state=?, updated_at=? WHERE item_id=?",
            (new_state, now, item_id))
        wconn.execute(
            "INSERT INTO workspace_audit(event_id, occurred_at, actor, action,"
            " item_id, detail) VALUES(?,?,?,?,?,?)",
            (f"evt_{uuid.uuid4().hex[:16]}", now, pid,
             f"retention.{decision}", item_id, ""))
    return {"item_id": item_id, "state": new_state, **(result or {})}


def list_items(states: list[str] | None = None,
               principal_id: str | None = None) -> list[dict]:
    with db.workspace() as wconn:
        q = "SELECT item_id, target_id, state, current_revision, claimed_by,"\
            " created_at, updated_at, retain_hints FROM v2_review_items"
        params: tuple = ()
        if states:
            q += " WHERE state IN (" + ",".join("?" * len(states)) + ")"
            params = tuple(states)
        q += " ORDER BY created_at DESC"
        rows = wconn.execute(q, params).fetchall()
    return [{**dict(r), "retain_hints": json.loads(r["retain_hints"])}
            for r in rows]
