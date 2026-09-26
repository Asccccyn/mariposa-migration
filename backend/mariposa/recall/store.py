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
from ..errors import Forbidden, NotFound

from .models import ACTIVE_STATUSES, SESSION_STATUSES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    return f"rs_{uuid.uuid4().hex[:12]}"


def _check_status(status: str) -> None:
    if status not in SESSION_STATUSES:
        raise Forbidden(f"未知 session 状态 {status}", code="INVALID_ARGUMENT")


def create_session(principal_id: str, conversation_scope: str,
                   query_plan: dict) -> dict:
    sid = new_session_id()
    now = _now()
    expires = (datetime.now(timezone.utc) +
               timedelta(hours=config.RECALL_SESSION_TTL_HOURS)).isoformat()
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO recall_sessions(session_id, principal_id,"
                " conversation_scope, status, current_revision, current_burst,"
                " rounds_used, bursts_used, policy_version, created_at,"
                " updated_at, expires_at)"
                " VALUES(?,?,?,?,1,1,0,1,?,?,?,?)",
                (sid, principal_id, conversation_scope or "", "ACTIVE",
                 config.RECALL_POLICY_VERSION, now, now, expires))
            conn.execute(
                "INSERT INTO recall_query_revisions(session_id, revision,"
                " request_ref, query_plan, change_reason, burst_no, created_at)"
                " VALUES(?,1,?,?, 'start', 1, ?)",
                (sid, query_plan.get("request_ref"),
                 json.dumps(query_plan, ensure_ascii=False), now))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return require_session(sid)


def get_session(session_id: str) -> dict | None:
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT * FROM recall_sessions WHERE session_id=?",
            (session_id,)).fetchone()
    return dict(row) if row else None


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


def bump_revision(session_id: str, expected_revision: int,
                  query_plan: dict | None = None,
                  request_ref: str | None = None,
                  change_reason: str = "refine",
                  burst_no: int | None = None,
                  bursts_used: int | None = None) -> dict:
    """refine 类动作：revision+1 并落新查询计划。"""
    session = require_session(session_id)
    fields = {"current_revision": expected_revision + 1}
    if burst_no is not None:
        fields["current_burst"] = burst_no
    sets = ["current_revision=?", "updated_at=?"]
    params: list = [expected_revision + 1, _now()]
    if burst_no is not None:
        sets.append("current_burst=?")
        params.append(burst_no)
    if bursts_used is not None:
        sets.append("bursts_used=?")
        params.append(bursts_used)
    params += [expected_revision, session_id]
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
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
                     json.dumps(query_plan, ensure_ascii=False),
                     change_reason, burst_no or session["current_burst"],
                     _now()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_session(session_id)


def add_revision_record(session_id: str, revision: int,
                        query_plan: dict, request_ref: str | None,
                        change_reason: str, burst_no: int) -> None:
    with db.recall_runtime() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO recall_query_revisions(session_id,"
            " revision, request_ref, query_plan, change_reason, burst_no,"
            " created_at) VALUES(?,?,?,?,?,?,?)",
            (session_id, revision, request_ref,
             json.dumps(query_plan, ensure_ascii=False), change_reason,
             burst_no, _now()))


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


def upsert_candidates(session_id: str, candidates: list[dict],
                      revision: int) -> None:
    """候选引用入 seen 池（只存引用与版本，不存正文）。"""
    now = _now()
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for c in candidates:
                state = c.get("state", "seen")
                conn.execute(
                    "INSERT INTO recall_candidates(session_id, candidate_ref,"
                    " resource_ref, channel, representation, content_version,"
                    " representation_version, state, score_ref, reject_target,"
                    " first_seen_revision, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(session_id, candidate_ref) DO UPDATE SET"
                    " state=excluded.state, score_ref=excluded.score_ref,"
                    " reject_target=excluded.reject_target,"
                    " updated_at=excluded.updated_at",
                    (session_id, c["candidate_ref"], c["resource_ref"],
                     c["channel"], c["representation"],
                     c.get("content_version"), c.get("representation_version"),
                     state, json.dumps(c.get("scores") or {},
                                       ensure_ascii=False),
                     c.get("reject_target"), revision, now))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def set_candidate_state(session_id: str, candidate_ref: str, state: str,
                        reject_target: str | None = None) -> None:
    with db.recall_runtime() as conn:
        cur = conn.execute(
            "UPDATE recall_candidates SET state=?, reject_target=?,"
            " updated_at=? WHERE session_id=? AND candidate_ref=?",
            (state, reject_target, _now(), session_id, candidate_ref))
        if cur.rowcount != 1:
            raise NotFound("candidate not found in session",
                           candidate_ref=candidate_ref)


def list_candidates(session_id: str,
                    states: tuple[str, ...] | None = None) -> list[dict]:
    with db.recall_runtime() as conn:
        if states:
            marks = ",".join("?" * len(states))
            rows = conn.execute(
                f"SELECT * FROM recall_candidates WHERE session_id=?"
                f" AND state IN ({marks})",
                (session_id,) + states).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM recall_candidates WHERE session_id=?",
                (session_id,)).fetchall()
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


def record_attempt(session_id: str, operation_id: str, revision: int,
                   burst_no: int, kind: str, status: str,
                   budget_snapshot: dict | None = None,
                   provider_versions: dict | None = None,
                   error: str | None = None) -> None:
    now = _now()
    with db.recall_runtime() as conn:
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


def add_receipts(session_id: str, receipts: list[dict]) -> None:
    now = _now()
    with db.recall_runtime() as conn:
        for r in receipts:
            conn.execute(
                "INSERT INTO recall_receipts(session_id, receipt_id,"
                " resource_ref, content_version, representation_version,"
                " permission_version, valid_at, expires_at, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(receipt_id) DO UPDATE SET"
                " content_version=excluded.content_version,"
                " representation_version=excluded.representation_version,"
                " valid_at=excluded.valid_at",
                (session_id, r["receipt_id"], r["resource_ref"],
                 r.get("content_version"), r.get("representation_version"),
                 r.get("permission_version"), now, None, now))


def list_receipts(session_id: str) -> list[dict]:
    with db.recall_runtime() as conn:
        rows = conn.execute(
            "SELECT * FROM recall_receipts WHERE session_id=?",
            (session_id,)).fetchall()
    return [dict(r) for r in rows]


def claim_operation(principal_id: str, operation_key: str,
                    result_builder) -> dict:
    """runtime 幂等（§9.4）：同 key 只执行一次副作用；重放只回结果引用。

    与 formal 库的 idempotency_records 分离——召回结果不长期缓存到
    正式库；session 过期后本表随清理失效。
    """
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT result_ref FROM recall_operation_keys"
            " WHERE principal_id=? AND operation_key=?",
            (principal_id, operation_key)).fetchone()
        if row is not None and row["result_ref"]:
            return {"idempotent_replay": True,
                    "data": json.loads(row["result_ref"])}
    data = result_builder()
    with db.recall_runtime() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO recall_operation_keys(principal_id,"
            " operation_key, result_ref, created_at) VALUES(?,?,?,?)",
            (principal_id, operation_key,
             json.dumps(data, ensure_ascii=False), _now()))
    return {"data": data}


def purge_expired(limit: int = 200) -> int:
    """有界清理：过期 session 及其子行。TTL 清理不影响正式记忆期限。"""
    now = _now()
    with db.recall_runtime() as conn:
        rows = conn.execute(
            "SELECT session_id FROM recall_sessions WHERE status IN"
            " ('ACTIVE','AMBIGUOUS','CONFLICT','DEGRADED','BUDGET_EXHAUSTED')"
            " AND expires_at < ? LIMIT ?", (now, limit)).fetchall()
        sids = [r["session_id"] for r in rows]
        for sid in sids:
            conn.execute(
                "UPDATE recall_sessions SET status='EXPIRED', updated_at=?"
                " WHERE session_id=?", (now, sid))
        return len(sids)


def reset_for_tests() -> None:
    """测试清库（conftest 专用）：清空 runtime 全部运行状态。"""
    tables = ("recall_operation_keys", "recall_receipts", "recall_attempts",
              "recall_candidates", "recall_query_revisions", "recall_sessions")
    with db.recall_runtime() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("PRAGMA foreign_keys=ON")
