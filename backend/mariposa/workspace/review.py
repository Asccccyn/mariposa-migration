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

#: 委托动作名 → revise/submit 入口的字段/决议映射
_REVIEWER_ACTIONS = {"summary_body": "revise_summary",
                     "forget_tags": "revise_tags",
                     "release": "release_clean"}

#: generate 单次调用最多翻的到期队列页数（RET-09：跳过项不占名额）
_GENERATE_MAX_PAGES = 20

#: 摘要禁用概括称谓/措辞（V2-REV-09）；原文含这些词不受影响
BANNED_SUMMARY_TERMS = ("用户", "AI", "助手", "角色扮演", "角色", "扮演",
                        "模拟", "互动")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_default_delegation() -> None:
    """开发默认委托：林石见可改候选摘要/tags、放行无疑点到期项、上报。

    生产替换为显式签发的 delegation 记录（valid_from/valid_to/scope）。
    历史默认清单（缺 escalate_jiaming）就地升级——只认逐字等于旧默认的
    行，不动运营签发的窄权限委托。
    """
    default_actions = ["revise_summary", "revise_tags", "release_clean",
                       "escalate_retain", "escalate_owner", "escalate_jiaming"]
    legacy_default = default_actions[:5]
    with db.formal() as conn:
        row = conn.execute(
            "SELECT delegation_id, allowed_actions FROM review_delegations"
            " WHERE reviewed_principal=? AND revoked=0",
            (REVIEWER,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO review_delegations(delegation_id,"
                " reviewed_principal, allowed_actions, resource_scope,"
                " valid_from, valid_to, revoked)"
                " VALUES(?,?,?,?,?,NULL,0)",
                (f"dlg_{uuid.uuid4().hex[:10]}", REVIEWER,
                 json.dumps(default_actions),
                 "forget_review", _now()))
            return
        try:
            granted = json.loads(row["allowed_actions"])
        except (TypeError, ValueError):
            granted = None
        if granted == legacy_default:
            conn.execute(
                "UPDATE review_delegations SET allowed_actions=? WHERE"
                " delegation_id=?",
                (json.dumps(default_actions), row["delegation_id"]))


def _require_reviewer(principal, action: str | None = None) -> None:
    """审查者身份 + 活跃委托 + （若指定）委托动作授权（§8.1）。

    allowed_actions 是委托的真实边界：窄权限委托不得越权改稿/放行。
    """
    if principal.principal_id != REVIEWER:
        raise Forbidden("restricted review is delegated to linshijian only",
                        principal=principal.principal_id)
    with db.formal() as conn:
        row = conn.execute(
            "SELECT allowed_actions FROM review_delegations WHERE"
            " reviewed_principal=? AND revoked=0 AND (valid_to IS NULL OR"
            " valid_to>?)",
            (REVIEWER, _now())).fetchone()
    if not row:
        raise Forbidden("no active review delegation", principal=REVIEWER)
    if action:
        try:
            allowed = set(json.loads(row["allowed_actions"]))
        except (TypeError, ValueError):
            allowed = set()
        if action not in allowed:
            raise Forbidden(
                f"review delegation does not grant: {action}",
                code="DELEGATION_ACTION_NOT_GRANTED", action=action)


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
    if not isinstance(summary_body, str):
        raise Forbidden("summary_body must be a string",
                        code="INVALID_ARGUMENT")
    hay = summary_body.lower()
    for term in BANNED_SUMMARY_TERMS:
        if term.lower() in hay:
            raise Forbidden(
                f"候选摘要含禁用概括措辞：{term}（V2-REV-09）；改写或转疑难",
                code="SUMMARY_TERM_BANNED", term=term)


def _eligible_target(conn, memory_id: str) -> bool:
    """到期且可建审查项：真正到期、非确定留、无未终局工作项。"""
    m = conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                     (memory_id,)).fetchone()
    if m is None:
        return False
    r = ret_mod.get(conn, memory_id)
    if r and r["status"] == "retained":
        # RET-11：确定留是终局，不再周期送审；显式拒绝而非静默跳过
        raise Forbidden("确定留的桶不再进入自动遗忘审查（终局）",
                        code="RETAINED_FINAL", memory_id=memory_id)
    if not ret_mod.is_due(conn, memory_id):
        return False  # 只有真正到期（business_today >= due_date）才生成
    with db.workspace() as wconn:
        exists = wconn.execute(
            "SELECT 1 FROM v2_review_items WHERE target_id=? AND"
            " state NOT IN ('forgotten','retained','withdrawn',"
            " 'stale','failed')", (memory_id,)).fetchone()
    return not exists


def _insert_review_item(fconn, principal_id: str, memory_id: str,
                        candidate_summary: str, candidate_tags_json: str,
                        now: str) -> dict:
    """落 v2_review_items（+首版候选稿）；返回 {item_id, state}。"""
    m = fconn.execute("SELECT current_version_no FROM memories WHERE"
                      " memory_id=?", (memory_id,)).fetchone()
    has_summary = bool(candidate_summary.strip())
    item_id = f"rev_{uuid.uuid4().hex[:12]}"
    hints = json.dumps(_collect_retain_hints(fconn, memory_id),
                       ensure_ascii=False)
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
                (item_id, "memory", memory_id,
                 "in_review" if has_summary else "generated",
                 candidate_summary.strip(), candidate_tags_json,
                 m["current_version_no"], _fields_hash(fconn, memory_id),
                 (ret_mod.get(fconn, memory_id) or {}).get("retention_revision"),
                 ret_mod.POLICY_VERSION, principal_id, hints, now, now))
            if has_summary:
                wconn.execute(
                    "INSERT INTO v2_proposal_versions(item_id, revision,"
                    " summary_body, forget_tags, created_by, created_at)"
                    " VALUES(?,1,?,?,?,?)",
                    (item_id, candidate_summary.strip(), candidate_tags_json,
                     principal_id, now))
            wconn.execute("COMMIT")
        except Exception:
            wconn.execute("ROLLBACK")
            raise
    return {"item_id": item_id, "target_id": memory_id,
            "state": "in_review" if has_summary else "generated"}


def generate(principal, memory_id: str | None = None,
             candidate_summary: str = "",
             candidate_tags: list[str] | None = None,
             from_queue: bool = True) -> dict:
    """从到期队列/指定桶生成审查项（worker 等生成者；无正式写入权）。

    未配置外部 provider 时接受调用方带入的候选稿（本地/合成流程）；
    没有候选稿的项停在 generated，等待草稿。

    队列健壮性（RET-09/RET-11）：keyset 翻页越过跳过项；单个已终局
    （retained）目标只记 skipped，不中断整批。
    """
    if principal.principal_id not in GENERATORS:
        raise Forbidden("generators only", principal=principal.principal_id)
    if candidate_summary.strip():
        _check_summary_terms(candidate_summary)
    tags_json = json.dumps(candidate_tags or [], ensure_ascii=False)
    created: list[dict] = []
    skipped: list[dict] = []
    now = _now()

    def _process(mid: str, explicit: bool = False) -> None:
        with db.formal() as conn:
            _stale_superseded(conn, mid)
            try:
                if not _eligible_target(conn, mid):
                    return
            except Forbidden as e:
                if e.code == "RETAINED_FINAL":
                    # 批处理（队列驱动）：确定留终局只记 skipped，不炸整批；
                    # 显式点名该桶：如实抛错（调用方要看这个桶的结果）
                    if explicit:
                        raise
                    skipped.append({"memory_id": mid, "reason": e.code})
                    return
                raise
            created.append(_insert_review_item(
                conn, principal.principal_id, mid, candidate_summary,
                tags_json, now))

    if memory_id:
        _process(memory_id, explicit=True)
    elif from_queue:
        from . import due_queue
        today = ret_mod.business_today().isoformat()
        after: tuple[str, str] | None = None
        for _ in range(_GENERATE_MAX_PAGES):
            page = due_queue.due_items(today, after=after)
            if not page:
                break
            for i in page:
                if i["target_kind"] == "memory":
                    _process(i["target_id"])
            after = (page[-1]["due_date"], page[-1]["target_id"])
            if len(page) < 50:  # due_items 默认页大小；不足一页=扫完
                break
    out: dict = {"created": created}
    if skipped:
        out["skipped"] = skipped
    if not created and not skipped:
        out["note"] = "无新到期目标（队列无待生成项或全部已有未终局审查项）"
    return out


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
    if not isinstance(changes, dict) or not changes:
        raise Forbidden("changes required", code="INVALID_ARGUMENT")
    extra = set(changes) - REVIEWABLE_FIELDS
    if extra:
        # V2-REV-03：拒绝而不是忽略
        raise Forbidden(
            f"审查者不可修改原始字段：{sorted(extra)}；只允许 "
            f"{sorted(REVIEWABLE_FIELDS)}",
            code="FIELD_NOT_REVIEWABLE", fields=sorted(extra))
    for field in changes:
        _require_reviewer(principal, _REVIEWER_ACTIONS[field])
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


def _reverify_source(conn, item) -> None:
    """执行前源核验（§8.4 前半）：源版本、字段hash、到期状态。

    release 与 owner-continue 两条生效路径都必须走这一步；明确打开
    续期（RET-08）会改变 retention_revision → 字段hash 失配 → 拒绝。
    """
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


def _reverify(conn, item) -> None:
    """执行前再核验（§8.4）：到期、源版本、字段hash、实时保留线索。"""
    _reverify_source(conn, item)
    # 线索实时重收（生成后新写入的回忆/话语同样要拦，不只看快照）
    if _collect_retain_hints(conn, item["target_id"]):
        raise Forbidden("保留线索未裁决；转 owner/jiaming 决定",
                        code="RETAIN_HINT_PENDING")


def _mark_stale(item_id: str, reason: str) -> None:
    """in_review 项落 stale 终态（带审计）。幂等：状态不符时不动。"""
    now = _now()
    with db.workspace() as wconn:
        cur = wconn.execute(
            "UPDATE v2_review_items SET state='stale', updated_at=? WHERE"
            " item_id=? AND state='in_review'", (now, item_id))
        if cur.rowcount:
            wconn.execute(
                "INSERT INTO workspace_audit(event_id, occurred_at, actor,"
                " action, item_id, detail) VALUES(?,?,?,?,?,?)",
                (f"evt_{uuid.uuid4().hex[:16]}", now, "system",
                 "review.stale", item_id, json.dumps({"reason": reason})))


def _stale_superseded(conn, memory_id: str) -> int:
    """自愈（状态机"未执行阶段→stale"）：in_review 项的源已变/不再到期
    时标 stale，防止它作为"未终局工作项"把同一桶的后续审查永久卡死。
    待终裁项（needs_owner/jiaming/deferred）不在此列——裁决仍有效，
    生效路径（_owner_continue）另有核验。"""
    with db.workspace() as wconn:
        rows = wconn.execute(
            "SELECT item_id, source_version, source_fields_hash FROM"
            " v2_review_items WHERE target_id=? AND state='in_review'",
            (memory_id,)).fetchall()
    if not rows:
        return 0
    stale: list[str] = []
    for r in rows:
        probe = {"item_id": r["item_id"], "target_id": memory_id,
                 "source_version": r["source_version"],
                 "source_fields_hash": r["source_fields_hash"]}
        try:
            _reverify_source(conn, probe)
        except (ProposalStale, Forbidden):
            stale.append(r["item_id"])
    if not stale:
        return 0
    now = _now()
    with db.workspace() as wconn:
        for item_id in stale:
            wconn.execute(
                "UPDATE v2_review_items SET state='stale', updated_at=?"
                " WHERE item_id=? AND state='in_review'", (now, item_id))
            wconn.execute(
                "INSERT INTO workspace_audit(event_id, occurred_at, actor,"
                " action, item_id, detail) VALUES(?,?,?,?,?,?)",
                (f"evt_{uuid.uuid4().hex[:16]}", now, "system",
                 "review.auto_stale", item_id,
                 json.dumps({"memory_id": memory_id})))
    return len(stale)


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
                          summary + "\n" + " ".join(tags)),
                      whitelist_body=projection.normalize_search_text(summary))
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
    if decision == "release":
        _require_reviewer(principal, "release_clean")
    else:
        _require_reviewer(principal, decision)
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
        if decision in ("escalate_retain", "escalate_owner"):
            # REV-05：共同话语线索（decision_owner=jiaming）不得被路由到
            # owner 决策位——终裁权属于周家明，必须走 escalate_jiaming
            hints = json.loads(item["retain_hints"] or "[]")
            if any(h.get("decision_owner") == "jiaming" for h in hints):
                raise Forbidden(
                    "共同话语线索必须 escalate_jiaming（终裁限定周家明）",
                    code="ESCALATE_TARGET_REQUIRED", item_id=item_id)
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
            except (ProposalStale, Forbidden) as e:
                conn.execute("ROLLBACK")
                if e.code in ("NOT_DUE", "PROPOSAL_STALE"):
                    # 状态机"未执行阶段→stale"：源已变/已续期的项落
                    # stale，不再作为未终局工作项卡住同一桶的后续审查
                    _mark_stale(item_id, f"submit:{e.code}")
                raise
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
        cur = wconn.execute(
            "UPDATE v2_review_items SET state=?, updated_at=? WHERE item_id=?"
            " AND state='in_review'",
            (new_state, now, item_id))
        if cur.rowcount == 0:
            raise Forbidden("item state moved during submit; re-read",
                            code="REVIEW_STATE_MOVED", item_id=item_id)
        wconn.execute(
            "INSERT INTO workspace_audit(event_id, occurred_at, actor, action,"
            " item_id, detail) VALUES(?,?,?,?,?,?)",
            (f"evt_{uuid.uuid4().hex[:16]}", now, principal.principal_id,
             f"review.{decision}", item_id,
             json.dumps(detail, ensure_ascii=False)))
    return {"item_id": item_id, "state": new_state, **detail}


def _owner_keep(item, pid: str, item_id: str) -> dict:
    """keep：确定留终局（R15），retention 行正式化，不再周期送审。"""
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
    return {}


def _owner_continue(item, pid: str, item_id: str) -> dict:
    """continue：按当前候选稿现在生效。

    与 release 同标准的执行前核验（§8.4，一步不缺）：源版本、字段hash
    （RET-08 明确打开续期会改变它）、到期状态、禁用词。保留线索按快照
    比对——裁决者已看过快照内的线索；终裁之后新出现的线索必须重新审查，
    不得凭旧裁决直接生效。
    """
    with db.workspace() as wconn:
        vrow = wconn.execute(
            "SELECT summary_body, forget_tags FROM v2_proposal_versions"
            " WHERE item_id=? AND revision=?",
            (item_id, item["current_revision"])).fetchone()
    if vrow is None:
        raise Forbidden("no candidate to apply")
    _check_summary_terms(vrow["summary_body"])
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _reverify_source(conn, item)
            live_hints = _collect_retain_hints(conn, item["target_id"])
            snapshot_hints = json.loads(item["retain_hints"] or "[]")
            fresh = [h for h in live_hints if h not in snapshot_hints]
            if fresh:
                raise Forbidden(
                    "终裁后出现新保留线索；重新审查后再决定",
                    code="RETAIN_HINT_PENDING", new_hints=fresh)
            result = _apply_forget(conn, item, vrow["summary_body"],
                                   json.loads(vrow["forget_tags"]), pid)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return result


def decide_retention(principal, item_id: str, decision: str) -> dict:
    """终裁（R14/R15）：keep=确定留终局；continue=现在生效；defer=挂起。

    needs_jiaming_decision（共同话语）只有周家明能裁；REV-05 的边界
    不依赖审查者是否选对升级目标——只要项上挂着 decision_owner=jiaming
    的线索（共同话语），keep/continue 终裁就限定周家明本人。
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
        hints = json.loads(item["retain_hints"] or "[]")
        jiaming_owned = any(h.get("decision_owner") == "jiaming"
                            for h in hints)
        if pid != "jiaming" and (item["state"] == "needs_jiaming_decision"
                                 or jiaming_owned):
            raise Forbidden("共同话语的终裁限定周家明", principal=pid)
    now = _now()
    if decision == "keep":
        result, new_state = _owner_keep(item, pid, item_id), "retained"
    elif decision == "continue":
        result, new_state = _owner_continue(item, pid, item_id), "forgotten"
    else:
        result, new_state = {}, "deferred"
    with db.workspace() as wconn:
        cur = wconn.execute(
            "UPDATE v2_review_items SET state=?, updated_at=? WHERE item_id=?"
            " AND state IN ('needs_owner_decision','needs_jiaming_decision',"
            " 'deferred')",
            (new_state, now, item_id))
        if cur.rowcount == 0:
            raise Forbidden("item state moved during decision; re-read",
                            code="REVIEW_STATE_MOVED", item_id=item_id)
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
