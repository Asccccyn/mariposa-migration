"""Recall Session 运行库持久化：CAS、操作幂等、过期清理（v1.4 §9.4/§10.2）。

runtime 库只存短期状态：查询计划 JSON、候选引用/状态、预算快照与回执；
不存长期记忆正文，不进 embedding，不进遗忘摘要。每次写 session 用
expected_revision/CAS 防丢失更新；operation_key 幂等只重放引用与结果。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from .. import config, db
from ..errors import Forbidden, IdempotencyConflict, NotFound


class OperationRaceLost(Exception):
    """commit-at-end 输家信号：最终事务写锁内发现同 key operation
    已由并发方完成。携带已有行，调用方整体回滚后重放赢家结果。"""
    def __init__(self, row):
        super().__init__("operation completed concurrently")
        self.row = row

from .models import ACTIVE_STATUSES, SESSION_STATUSES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    return f"rs_{uuid.uuid4().hex[:12]}"


def new_session_draft(principal_id: str, conversation_scope: str,
                      query_plan: dict) -> dict:
    """commit-at-end 阶段 B：内存生成执行上下文，不落库。

    session_id 仅作为本轮内部关联 ID 使用；检索/精排/组装全部完成后，
    由 commit_start 在最终事务内一次性写入 session 及其全部派生行。
    """
    sid = new_session_id()
    now = _now()
    expires = (datetime.now(timezone.utc) +
               timedelta(hours=config.RECALL_SESSION_TTL_HOURS)).isoformat()
    return {
        "session_id": sid, "principal_id": principal_id,
        "conversation_scope": conversation_scope or "", "status": "ACTIVE",
        "current_revision": 1, "current_burst": 1, "rounds_used": 0,
        "bursts_used": 1, "expires_at": expires,
        "_draft": {"query_plan": query_plan, "created_at": now},
    }


def insert_session(conn, draft: dict) -> None:
    """最终事务内写入 session 与首版 query revision（commit_start 调用）。"""
    d = draft["_draft"]
    conn.execute(
        "INSERT INTO recall_sessions(session_id, principal_id,"
        " conversation_scope, status, current_revision, current_burst,"
        " rounds_used, bursts_used, policy_version, created_at,"
        " updated_at, expires_at)"
        " VALUES(?,?,?,?,1,1,0,1,?,?,?,?)",
        (draft["session_id"], draft["principal_id"],
         draft["conversation_scope"], "ACTIVE",
         config.RECALL_POLICY_VERSION, d["created_at"], d["created_at"],
         draft["expires_at"]))
    conn.execute(
        "INSERT INTO recall_query_revisions(session_id, revision,"
        " request_ref, query_plan, change_reason, burst_no, created_at)"
        " VALUES(?,1,?,?, 'start', 1, ?)",
        (draft["session_id"], d["query_plan"].get("request_ref"),
         json.dumps(d["query_plan"], ensure_ascii=False),
         d["created_at"]))


def _check_status(status: str) -> None:
    if status not in SESSION_STATUSES:
        raise Forbidden(f"未知 session 状态 {status}", code="INVALID_ARGUMENT")


def get_session(session_id: str) -> dict | None:
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT * FROM recall_sessions WHERE session_id=?",
            (session_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        # 预算事实源 = 成功 round 记录（commit-at-end）；session 行的
        # rounds_used 列不再是权威，读时统一派生覆盖。
        out["rounds_used"] = conn.execute(
            "SELECT COUNT(*) AS c FROM recall_rounds WHERE session_id=?",
            (session_id,)).fetchone()["c"]
        return out


def count_rounds(conn, session_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM recall_rounds WHERE session_id=?",
        (session_id,)).fetchone()["c"]


def require_session(session_id: str) -> dict:
    row = get_session(session_id)
    if row is None:
        raise NotFound("recall session not found", session_id=session_id)
    return row


def expire_if_due(session: dict) -> dict:
    """读时惰性过期（EXPIRED）：TTL 只是运行状态清理，不影响任何正式记忆。"""
    if session["status"] not in ACTIVE_STATUSES:
        return session
    try:
        exp = datetime.fromisoformat(session["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) <= exp:
            return session
    except ValueError:
        pass
    update_status(session["session_id"], session["current_revision"],
                  "EXPIRED")
    session["status"] = "EXPIRED"
    return session


def update_status(session_id: str, expected_revision: int,
                  status: str, extra_fields: dict | None = None) -> dict:
    """CAS 更新 session 状态；revision 不匹配 → RevisionConflict（可诊断）。"""
    _check_status(status)
    extra_fields = extra_fields or {}
    sets = ["status=?", "updated_at=?"]
    params: list = [status, _now()]
    for k in ("rounds_used", "bursts_used", "current_burst",
              "current_revision"):
        if k in extra_fields:
            sets.append(f"{k}=?")
            params.append(extra_fields[k])
    params += [expected_revision, session_id]
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                f"UPDATE recall_sessions SET {', '.join(sets)}"
                " WHERE current_revision=? AND session_id=?", params)
            if cur.rowcount != 1:
                raise Forbidden(
                    "session revision 冲突（expected_revision 过期）；"
                    "请先 status 重读当前状态再重试",
                    code="REVISION_CONFLICT",
                    session_id=session_id,
                    expected_revision=expected_revision)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_session(session_id)


def advance_revision(conn, session_id: str, expected_revision: int,
                     query_plan: dict | None = None,
                     request_ref: str | None = None,
                     change_reason: str = "refine",
                     burst_no: int | None = None,
                     bursts_used: int | None = None) -> int:
    """refine 最终事务内的 revision 前进（CAS，写锁内）。

    返回前进后的 revision；CAS 失败（并发 refine 输家）抛
    REVISION_CONFLICT，调用方整体回滚。
    """
    sets = ["current_revision=?", "updated_at=?"]
    params: list = [expected_revision + 1, _now()]
    if burst_no is not None:
        sets.append("current_burst=?")
        params.append(burst_no)
    if bursts_used is not None:
        sets.append("bursts_used=?")
        params.append(bursts_used)
    params += [expected_revision, session_id]
    cur = conn.execute(
        f"UPDATE recall_sessions SET {', '.join(sets)}"
        " WHERE current_revision=? AND session_id=?", params)
    if cur.rowcount != 1:
        raise Forbidden("session revision 冲突",
                        code="REVISION_CONFLICT",
                        session_id=session_id,
                        expected_revision=expected_revision)
    if query_plan is not None:
        conn.execute(
            "INSERT INTO recall_query_revisions(session_id, revision,"
            " request_ref, query_plan, change_reason, burst_no,"
            " created_at) VALUES(?,?,?,?,?,?,?)",
            (session_id, expected_revision + 1, request_ref,
             json.dumps(query_plan, ensure_ascii=False), change_reason,
             burst_no or 1, _now()))
    return expected_revision + 1


def record_round1_receipt(conn, *, session_id: str, revision: int,
                          plan_hash: str, scope_hash: str,
                          policy_version: str, round_kind: str,
                          methods: dict, coverage: dict,
                          candidate_set_hash: str, judged_count: int,
                          unavailable_count: int, unjudged_count: int,
                          delivery_action: str,
                          completed: bool = False) -> None:
    """S13/WP04：Round1 成功回执——绑定 plan/scope/policy/覆盖与
    judge 统计，供 Round2 门禁核验（不新建独立服务，复用 runtime）。

    全量审计 P1-01：completed 只在 mark_round1_complete=True 的同一
    最终事务里置 1——统计回执的存在不再隐含"本 revision 完整完成"
    （故障/降级轮照写统计回执，但不能作为 Round2 升级依据）。"""
    conn.execute(
        "INSERT OR REPLACE INTO recall_round1_receipts(session_id,"
        " revision, plan_hash, scope_hash, policy_version, round_kind,"
        " methods, coverage, candidate_set_hash, judged_count,"
        " unavailable_count, unjudged_count, delivery_action, completed,"
        " created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, revision, plan_hash, scope_hash, policy_version,
         round_kind, json.dumps(methods, ensure_ascii=False),
         json.dumps(coverage, ensure_ascii=False), candidate_set_hash,
         judged_count, unavailable_count, unjudged_count,
         delivery_action, 1 if completed else 0, _now()))


def read_round1_receipt(conn, session_id: str, revision: int):
    row = conn.execute(
        "SELECT * FROM recall_round1_receipts WHERE session_id=?"
        " AND revision=?", (session_id, revision)).fetchone()
    out = dict(row) if row else None
    if out:
        out["methods"] = json.loads(out["methods"] or "{}")
        out["coverage"] = json.loads(out["coverage"] or "{}")
    return out


def has_raw_round(conn, session_id: str, burst_no: int) -> bool:
    """同 burst 已有 raw 轮 → 不因换 operation_id 无界重跑（S13-5）。"""
    return bool(conn.execute(
        "SELECT 1 FROM recall_rounds WHERE session_id=? AND burst_no=?"
        " AND kind='raw'", (session_id, burst_no)).fetchone())


def issue_raw_continuation(conn, *, session_id: str, revision: int,
                           burst_no: int, next_offset: int) -> str:
    """三轮复审#2：签发/重签 raw 分页游标（单活跃——覆盖旧行）。"""
    token = uuid.uuid4().hex
    conn.execute(
        "INSERT OR REPLACE INTO recall_raw_continuations(session_id,"
        " revision, burst_no, token, next_offset, issued_at)"
        " VALUES(?,?,?,?,?,?)",
        (session_id, revision, burst_no, token, next_offset, _now()))
    return token


def replace_raw_continuation(conn, *, session_id: str, revision: int,
                             burst_no: int, expect_token: str,
                             next_offset: int) -> str | None:
    """自审（2026-10-01）：翻页签发的 CAS——只有仍持有被消费 token
    的请求才能重签（并发双花时恰一赢家）；输家返回 None。

    校验（计算前）与签发（最终事务）之间无锁，commit-at-end 语义：
    竞态输家在此回滚拒 CONTINUATION_INVALID；崩溃在事务前的重试
    不受影响（行未被动过，旧 token 仍有效）。
    """
    token = uuid.uuid4().hex
    cur = conn.execute(
        "UPDATE recall_raw_continuations SET token=?, next_offset=?,"
        " issued_at=? WHERE session_id=? AND revision=? AND burst_no=?"
        " AND token=?",
        (token, next_offset, _now(), session_id, revision, burst_no,
         expect_token))
    return token if cur.rowcount == 1 else None


def clear_raw_continuation_if(conn, *, session_id: str, revision: int,
                              burst_no: int,
                              expect_token: str) -> bool:
    """翻尽清游标（CAS：仅当仍持有被消费 token 时清）。"""
    cur = conn.execute(
        "DELETE FROM recall_raw_continuations WHERE session_id=?"
        " AND revision=? AND burst_no=? AND token=?",
        (session_id, revision, burst_no, expect_token))
    return cur.rowcount == 1


def read_raw_continuation(conn, session_id: str, revision: int,
                          burst_no: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM recall_raw_continuations WHERE session_id=?"
        " AND revision=? AND burst_no=?",
        (session_id, revision, burst_no)).fetchone()
    return dict(row) if row else None


def record_round(conn, session_id: str, burst_no: int,
                 operation_key: str | None = None,
                 kind: str = "memory") -> int:
    """最终事务内登记一条成功轮记录（预算唯一事实源）。

    round_no 在写锁内取 MAX+1；调用方已在此前的预算终检中确认额度。
    """
    round_no = conn.execute(
        "SELECT COALESCE(MAX(round_no), 0) + 1 AS n FROM recall_rounds"
        " WHERE session_id=?", (session_id,)).fetchone()["n"]
    conn.execute(
        "INSERT INTO recall_rounds(session_id, round_no, burst_no,"
        " operation_key, created_at, kind) VALUES(?,?,?,?,?,?)",
        (session_id, round_no, burst_no, operation_key, _now(), kind))
    # session 行的 rounds_used 只是派生展示值，与事实源同事务对齐
    conn.execute(
        "UPDATE recall_sessions SET rounds_used=? WHERE session_id=?",
        (round_no, session_id))
    return round_no


def get_plan(session_id: str, revision: int | None = None) -> dict | None:
    with db.recall_runtime() as conn:
        if revision is None:
            row = conn.execute(
                "SELECT query_plan FROM recall_query_revisions"
                " WHERE session_id=? ORDER BY revision DESC LIMIT 1",
                (session_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT query_plan FROM recall_query_revisions"
                " WHERE session_id=? AND revision=?",
                (session_id, revision)).fetchone()
    return json.loads(row["query_plan"]) if row else None


def upsert_candidates(conn, session_id: str, candidates: list[dict],
                      revision: int) -> None:
    """候选引用入 seen 池（只存引用与版本，不存正文）。

    commit-at-end：在调用方的最终事务内执行（conn 为事务连接）。
    """
    now = _now()
    for c in candidates:
        state = c.get("state", "seen")
        conn.execute(
            "INSERT INTO recall_candidates(session_id, candidate_ref,"
            " resource_ref, channel, representation, content_version,"
            " representation_version, state, score_ref, reject_target,"
            " first_seen_revision, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id, candidate_ref) DO UPDATE SET"
            " state=CASE WHEN recall_candidates.state IN"
            "   ('rejected','accepted') AND excluded.state='seen'"
            " THEN recall_candidates.state ELSE excluded.state END,"
            " score_ref=excluded.score_ref,"
            " reject_target=excluded.reject_target,"
            " updated_at=excluded.updated_at",
            (session_id, c["candidate_ref"], c["resource_ref"],
             c["channel"], c["representation"],
             c.get("content_version"), c.get("representation_version"),
             state, json.dumps(c.get("scores") or {},
                               ensure_ascii=False),
             c.get("reject_target"), revision, now))


def set_candidate_state(session_id: str, candidate_ref: str, state: str,
                        reject_target: str | None = None) -> None:
    with db.recall_runtime() as conn:
        set_candidate_state_tx(conn, session_id, candidate_ref, state,
                               reject_target)


def set_candidate_state_tx(conn, session_id: str, candidate_ref: str,
                           state: str,
                           reject_target: str | None = None) -> None:
    """事务内版：设为型候选状态写入（调用方事务）。"""
    cur = conn.execute(
        "UPDATE recall_candidates SET state=?, reject_target=?,"
        " updated_at=? WHERE session_id=? AND candidate_ref=?",
        (state, reject_target, _now(), session_id, candidate_ref))
    if cur.rowcount != 1:
        raise NotFound("candidate not found in session",
                       candidate_ref=candidate_ref)


def issue_continue_ref(conn, session_id: str, ref: str,
                       for_revision: int) -> None:
    """签发接续引用（RECALL-02，2026-10-04 二批）：与该轮交付同事务。

    签出的 ref 绑定 for_revision（本轮交付的 revision）；客户端下一
    次 refine 用它申领 burst。未签发过的任意字符串不构成有效接续。
    """
    conn.execute(
        "INSERT INTO recall_continue_refs(session_id,"
        " continue_request_ref, from_revision, created_at,"
        " consumed_at) VALUES(?,?,?,?,NULL)",
        (session_id, str(ref), int(for_revision), _now()))


def consume_continue_ref(conn, session_id: str, ref: str,
                         head_revision: int) -> None:
    """消费接续引用（裁定 2026-10-04 + RECALL-02）。

    合同：服务端必须验证 continue_request_ref 与当前 session 最新
    已交付且可继续 revision 的绑定——签发记录不存在（从未签发）、
    已消费、或绑定 revision 落后于当前 head，均判 stale；通过则与
    burst 授予同事务标记消费。同 request_ref 的网络重试走 operation
    幂等重放，不会二次进入本函数；回滚时消费一并撤销。
    """
    from ..errors import StaleOperation
    row = conn.execute(
        "SELECT from_revision, consumed_at FROM recall_continue_refs"
        " WHERE session_id=? AND continue_request_ref=?",
        (session_id, str(ref))).fetchone()
    if row is None:
        raise StaleOperation(
            "continue_request_ref 从未签发：接续引用由服务端随交付"
            "签发（见响应 continuation 字段），不接受任意字符串",
            session_id=session_id, continue_request_ref=str(ref))
    if row["consumed_at"] is not None:
        raise StaleOperation(
            "continue_request_ref 已消费（线性接续）：不得从旧节点"
            " 重复申领 burst", session_id=session_id,
            continue_request_ref=str(ref))
    if int(row["from_revision"]) != int(head_revision):
        raise StaleOperation(
            "continue_request_ref 已过时：绑定 revision"
            f" {row['from_revision']} 不是当前最新已交付 revision"
            f" {head_revision}（head 已前移，请用最新交付签发的引用）",
            session_id=session_id, continue_request_ref=str(ref),
            bound_revision=int(row["from_revision"]),
            head_revision=int(head_revision))
    cur = conn.execute(
        "UPDATE recall_continue_refs SET consumed_at=? WHERE"
        " session_id=? AND continue_request_ref=? AND consumed_at"
        " IS NULL", (_now(), session_id, str(ref)))
    if cur.rowcount != 1:
        raise StaleOperation(
            "continue_request_ref 消费竞争失败（已被并发消费）",
            session_id=session_id, continue_request_ref=str(ref))


def require_session_in_tx(conn, session_id: str) -> dict:
    """写锁内重读 session 当前行（F03：动作最终事务统一复查）。

    动作的允许检查发生在锁外；close 不推进 revision，仅按 revision
    的 CAS 拦不住"锁外读 ACTIVE → 对端提交终态 → 本事务覆盖终态"
    的合法交错。设为型动作在最终写锁内必须以此行重跑状态机与归属。
    """
    row = conn.execute(
        "SELECT * FROM recall_sessions WHERE session_id=?",
        (session_id,)).fetchone()
    if row is None:
        raise NotFound("recall session not found", session_id=session_id)
    return dict(row)


def update_status_tx(conn, session_id: str, expected_revision: int,
                     status: str) -> None:
    """事务内版 CAS 状态写入（close/accept 等最终事务调用）。"""
    _check_status(status)
    cur = conn.execute(
        "UPDATE recall_sessions SET status=?, updated_at=?"
        " WHERE current_revision=? AND session_id=?",
        (status, _now(), expected_revision, session_id))
    if cur.rowcount != 1:
        raise Forbidden(
            "session revision 冲突（expected_revision 过期）；"
            "请先 status 重读当前状态再重试",
            code="REVISION_CONFLICT", session_id=session_id,
            expected_revision=expected_revision)


def list_candidates(session_id: str,
                    states: tuple[str, ...] | None = None,
                    conn=None) -> list[dict]:
    """候选列表；conn 直传时在调用方事务/连接内执行。"""
    if states:
        marks = ",".join("?" * len(states))
        sql = (f"SELECT * FROM recall_candidates WHERE session_id=?"
               f" AND state IN ({marks})")
        params: tuple = (session_id,) + tuple(states)
    else:
        sql = "SELECT * FROM recall_candidates WHERE session_id=?"
        params = (session_id,)
    if conn is not None:
        rows = conn.execute(sql, params).fetchall()
    else:
        with db.recall_runtime() as c:
            rows = c.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["scores"] = json.loads(d.pop("score_ref") or "{}")
        out.append(d)
    return out


def rejected_resource_refs(session_id: str) -> set[str]:
    """当前 session 的排除集合：reject 的候选资源 + 其事件级排除展开。"""
    rows = list_candidates(session_id, states=("rejected",))
    return {r["resource_ref"] for r in rows}


def record_attempt(conn, session_id: str, operation_id: str, revision: int,
                   burst_no: int, kind: str, status: str,
                   budget_snapshot: dict | None = None,
                   provider_versions: dict | None = None,
                   error: str | None = None) -> None:
    """诊断性尝试记录（非预算事实源）；在调用方事务内执行。"""
    now = _now()
    conn.execute(
        "INSERT INTO recall_attempts(session_id, operation_id, revision,"
        " burst_no, kind, status, budget_snapshot, provider_versions,"
        " error, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(session_id, operation_id) DO UPDATE SET"
        " status=excluded.status, budget_snapshot=excluded.budget_snapshot,"
        " provider_versions=excluded.provider_versions,"
        " error=excluded.error, updated_at=excluded.updated_at",
        (session_id, operation_id, revision, burst_no, kind, status,
         json.dumps(budget_snapshot or {}, ensure_ascii=False),
         json.dumps(provider_versions or {}, ensure_ascii=False),
         error, now, now))


def add_receipts(conn, session_id: str, receipts: list[dict],
                 revision: int | None = None) -> None:
    """交付回执写入；在调用方事务内执行。

    revision 记录本回执对应的出站交付轮（裁定 2026-10-04：拒绝
    理由须能证明候选在哪一轮真实出站）。
    """
    now = _now()
    for r in receipts:
        conn.execute(
            "INSERT INTO recall_receipts(session_id, receipt_id,"
            " resource_ref, content_version, representation_version,"
            " permission_version, valid_at, expires_at, created_at,"
            " revision)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(receipt_id) DO UPDATE SET"
            " content_version=excluded.content_version,"
            " representation_version=excluded.representation_version,"
            " valid_at=excluded.valid_at,"
            " revision=excluded.revision",
            (session_id, r["receipt_id"], r["resource_ref"],
             r.get("content_version"), r.get("representation_version"),
             r.get("permission_version"), now, None, now, revision))


def list_receipts(session_id: str) -> list[dict]:
    with db.recall_runtime() as conn:
        rows = conn.execute(
            "SELECT * FROM recall_receipts WHERE session_id=?",
            (session_id,)).fetchall()
    return [dict(r) for r in rows]


def read_operation(principal_id: str, operation_key: str):
    with db.recall_runtime() as conn:
        return conn.execute(
            "SELECT * FROM recall_operation_keys WHERE principal_id=?"
            " AND operation_key=?",
            (principal_id, operation_key)).fetchone()


def replay_operation_row(row, replay_guard):
    """重放已完成 operation：guard 按当前状态重校验后出站（审计 F07）。

    R01（复审 2026-10-07）：出站=裸 packet，重放标记并进顶层字段——与
    通用 transport 幂等回执（capabilities/transport.py）及 deletion/keep/
    corrections 各域惯例一致。不再返回 {"data": ...} 内层信封：那使同一
    能力出现两种嵌套深度，estómago 适配层因此把 FOUND 吞成空候选。
    """
    saved = json.loads(row["result_ref"])
    if replay_guard is not None:
        saved = replay_guard(saved)
    if not isinstance(saved, dict):
        return saved
    out = dict(saved)
    out["idempotent_replay"] = True
    return out


_OPERATION_LOCKS: dict[str, tuple] = {}  # key -> (Lock, waiter_count)
_OPERATION_LOCKS_GUARD = __import__("threading").Lock()


def _operation_lock(operation_key: str):
    """进程内 per-key 锁（性能优化）。有界：无人等待时从 registry
    淘汰，长跑进程不再无界增长（复审 P3）。返回 [lock, waiters]。"""
    import threading
    with _OPERATION_LOCKS_GUARD:
        entry = _OPERATION_LOCKS.get(operation_key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _OPERATION_LOCKS[operation_key] = entry
        entry[1] += 1
    return entry


def _release_operation_lock(operation_key: str, entry) -> None:
    with _OPERATION_LOCKS_GUARD:
        entry[1] -= 1
        if entry[1] <= 0 and _OPERATION_LOCKS.get(operation_key) is entry:
            del _OPERATION_LOCKS[operation_key]


def run_operation(principal_id: str, operation_key: str, builder,
                  payload_hash: str | None = None,
                  replay_guard=None) -> dict:
    """runtime operation 幂等（commit-at-end 模型）。

    - 不再提前落库任何中间态：没有 IN_PROGRESS，没有 failed 记录。
      计算失败/崩溃 → 数据库无本 operation 的任何痕迹，同 key 重试
      从头重新计算。
    - 已完成的 operation（数据库唯一键 principal_id+operation_key）
      直接重放保存结果；重放经 replay_guard 按当前状态重校验
      （审计 F07：session 有效期、候选版本、可见性、当前 phase）。
    - 同 key 异 payload → IDEMPOTENCY_CONFLICT，不回放无关旧结果。
    - 进程内锁只用于避免同进程重复跑昂贵的检索/精排（性能优化，
      非正确性保证）；跨进程并发由最终事务内（BEGIN IMMEDIATE 写锁
      内）的 operation 查重 + 数据库唯一约束裁决，输家回滚自己的
      全部写入并重放赢家的结果。
    """
    row = read_operation(principal_id, operation_key)
    if row is not None:
        if payload_hash is not None and row["payload_hash"] not in (
                None, payload_hash):
            raise IdempotencyConflict(
                "same operation key with different payload",
                operation_key=operation_key)
        if row["result_ref"]:
            return replay_operation_row(row, replay_guard)
    entry = _operation_lock(operation_key)
    try:
        with entry[0]:
            # 锁内双检：前一个同 key 持有者可能刚完成
            row = read_operation(principal_id, operation_key)
            if row is not None:
                if payload_hash is not None \
                        and row["payload_hash"] not in (
                            None, payload_hash):
                    raise IdempotencyConflict(
                        "same operation key with different payload",
                        operation_key=operation_key)
                if row["result_ref"]:
                    return replay_operation_row(row, replay_guard)
            # builder 内部（service 层）完成全部可失败计算，并在最终
            # 事务内写入 session/round/result/operation。跨进程并发时，
            # 最终事务在写锁内查到已有 operation → 抛 OperationRaceLost，
            # 由这里读出赢家结果返回。
            try:
                data = builder()
            except OperationRaceLost as lost:
                return replay_operation_row(lost.row, replay_guard)
            # P1-01：所有 runtime 动作必须在自身最终事务内写入
            # operation 记录（业务副作用与收据原子）。builder 返回而
            # 库中无记录 = 实现遗漏，fail-fast 暴露而不是静默补写
            # 制造崩溃窗口。
            row = read_operation(principal_id, operation_key)
            if row is None:
                raise RuntimeError(
                    "operation handler returned without recording its "
                    "operation row inside its final transaction: "
                    f"{operation_key}")
            # R01（复审 2026-10-07）：返回裸 packet（重放路径见
            # replay_operation_row——顶层 idempotent_replay 标记）。
            # 出站统一单层信封 {ok, data}，由 registry.invoke 包一次。
            return data
    finally:
        _release_operation_lock(operation_key, entry)


def record_operation_row(conn, principal_id: str, operation_key: str,
                         payload_hash: str | None, result: dict) -> None:
    """最终事务内写入完成态 operation（由 service 的 commit 函数调用）。

    若写锁内发现同 key 已有完成行（跨进程并发输家），抛
    OperationRaceLost——调用方整体 ROLLBACK 后由 run_operation 重放
    赢家结果，数据库中只保留一份正式成功结果。
    """
    row = conn.execute(
        "SELECT * FROM recall_operation_keys WHERE principal_id=?"
        " AND operation_key=?",
        (principal_id, operation_key)).fetchone()
    if row is not None:
        if payload_hash is not None and row["payload_hash"] not in (
                None, payload_hash):
            raise IdempotencyConflict(
                "same operation key with different payload",
                operation_key=operation_key)
        if not row["result_ref"]:
            # 旧 NULL 结果行（migration 5 前残留）：本事务接管写入
            conn.execute(
                "UPDATE recall_operation_keys SET payload_hash=?,"
                " result_ref=?, updated_at=?"
                " WHERE principal_id=? AND operation_key=?"
                " AND (result_ref IS NULL OR result_ref = '')",
                (payload_hash, json.dumps(result, ensure_ascii=False),
                 _now(), principal_id, operation_key))
            return
        raise OperationRaceLost(row)
    conn.execute(
        "INSERT INTO recall_operation_keys(principal_id, operation_key,"
        " payload_hash, status, result_ref, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (principal_id, operation_key, payload_hash, "completed",
         json.dumps(result, ensure_ascii=False), _now(), _now()))


def check_operation_conflict(principal_id: str, operation_key: str,
                             payload_hash: str | None) -> None:
    """阶段 A 便宜预检：同 key 异 payload 直接结构化拒绝（读路径）。"""
    if payload_hash is None:
        return
    row = read_operation(principal_id, operation_key)
    if row is not None and row["payload_hash"] not in (None, payload_hash):
        raise IdempotencyConflict(
            "same operation key with different payload",
            operation_key=operation_key)


def purge_expired(limit: int = 200) -> int:
    """有界清理：过期 session 及其子行。TTL 清理不影响正式记忆期限。

    审计 F07：operation 幂等行同样有界清理——超过 session TTL 双倍
    时长的行删除（重放本就必须通过当前状态重校验，清理不改变语义）。

    P2-05（2026-10-05 审计）：docstring 承诺的"及其子行"此前从未兑现
    ——只把状态改成 EXPIRED + 删 operation_keys，rounds/receipts/
    candidates/attempts/revisions 全部无界增长，违背"只存短期状态"
    的库定位。现补齐：超过 TTL 双倍时长的 session（含非终态残留）
    连同子行物理删除，重放路径本就要求经当前状态重校验。
    """
    now = _now()
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT session_id FROM recall_sessions WHERE status IN"
                " ('ACTIVE','AMBIGUOUS','CONFLICT','DEGRADED','BUDGET_EXHAUSTED')"
                " AND expires_at < ? LIMIT ?", (now, limit)).fetchall()
            sids = [r["session_id"] for r in rows]
            for sid in sids:
                conn.execute(
                    "UPDATE recall_sessions SET status='EXPIRED', updated_at=?"
                    " WHERE session_id=?", (now, sid))
            cutoff = (datetime.now(timezone.utc) -
                      timedelta(hours=config.RECALL_SESSION_TTL_HOURS * 2)
                      ).isoformat()
            conn.execute(
                "DELETE FROM recall_operation_keys WHERE created_at < ?",
                (cutoff,))
            # 超期 session 子行按 FK 依赖序（子先父后）整链删除
            stale = conn.execute(
                "SELECT session_id FROM recall_sessions"
                " WHERE expires_at < ?", (cutoff,)).fetchall()
            stale_ids = [r["session_id"] for r in stale]
            for sid in stale_ids:
                for child in ("recall_round1_receipts", "recall_rounds",
                              "recall_query_revisions", "recall_attempts",
                              "recall_receipts", "recall_candidates",
                              # F-J-04（联合审计 2026-10-06）：三张无 FK 子表
                              # 此前漏清——超期 session 留孤儿行无界增长
                              "recall_continue_refs",
                              "recall_raw_continuations",
                              "recall_raw_leases"):
                    conn.execute(
                        f"DELETE FROM {child} WHERE session_id=?",
                        (sid,))
                # MANUAL_HANDOFF_JUDGE_SWITCH_V1（M06）：关闭模式分页资产
                # 随 session 整链清理——先游标（无 session 列，按集合删）
                # 再结果集，无孤儿
                conn.execute(
                    "DELETE FROM recall_page_cursors WHERE"
                    " result_set_id IN (SELECT result_set_id FROM"
                    " recall_page_sets WHERE session_id=?)", (sid,))
                conn.execute(
                    "DELETE FROM recall_page_sets WHERE session_id=?",
                    (sid,))
                conn.execute(
                    "DELETE FROM recall_sessions WHERE session_id=?",
                    (sid,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(sids)


def reset_for_tests() -> None:
    """测试清库（conftest 专用）：清空 runtime 全部运行状态。"""
    tables = ("jev_feature_cache", "jev_rerank_cache",
              "recall_operation_keys", "recall_rounds",
              "recall_round1_receipts", "recall_receipts",
              "recall_attempts", "recall_candidates",
              "recall_query_revisions", "recall_sessions",
              # F-J-04：与 purge_expired 同步（漏清=跨测试残留）
              "recall_continue_refs", "recall_raw_continuations",
              "recall_raw_leases",
              # M06：关闭模式分页资产同源清理
              "recall_page_cursors", "recall_page_sets")
    with db.recall_runtime() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("PRAGMA foreign_keys=ON")
