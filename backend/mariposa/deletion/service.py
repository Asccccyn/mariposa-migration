"""删除申请（memory-only）：审批制删除，非直接物理删除。

继承的旧规格（main2.5 v2.17.11 源码核验，0921 Phase 5a）：
- 删除 = 提交删除申请（confirm 语义）：reason 必填；同资源 pending_exists(409)；
  DAILY_LIMIT=10/天、LIFETIME_LIMIT=5/资源；withdraw 撤回；decide(approve/reject)
  由 AI 侧审批，可带 ai_reason 与 expected_resource_id 乐观校验；目标不活跃 ->
  superseded
- approve + action=delete 才物理删除（审批+限额+审计门槛；旧"测试桶豁免直删"
  通道不迁移，mariposa 不暴露无审批物理删除）
- archive -> memory.visibility=archived（对齐旧 bucket_mgr.archive）

2026-10-01：信件功能拆出 mariposa（独立项目另行开发），本模块自
letters/service.py 迁出并收窄为仅服务 memory（letters/letter_versions 表
已随 schema v24 移除）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import audit, config, db
from ..errors import AlreadyDecided, DeleteBlocked, Forbidden, NotFound
from ..retrieval import projection

DAILY_LIMIT = 10
LIFETIME_LIMIT = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def deletion_submit(principal_id: str, resource_id: str, reason: str,
                    action: str = "delete") -> dict:
    """人类提交删除申请。reason 必填；限额与旧系统一致。"""
    if action not in ("archive", "delete"):
        raise Forbidden("action must be archive or delete")
    reason = str(reason or "").strip()
    if not reason:
        raise Forbidden("deletion reason is required", code="reason_required")
    with db.formal() as conn:
        target = conn.execute("SELECT memory_id FROM memories WHERE memory_id=?",
                              (resource_id,)).fetchone()
        if target is None:
            raise NotFound("target not found", resource_id=resource_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            related = conn.execute(
                "SELECT status FROM deletion_requests WHERE resource_id=?",
                (resource_id,)).fetchall()
            if any(r["status"] == "pending" for r in related):
                raise Forbidden("a deletion request is already pending",
                                code="pending_exists")
            if len(related) >= LIFETIME_LIMIT:
                raise Forbidden("resource lifetime deletion request limit reached",
                                code="lifetime_limit")
            local_date = _now().astimezone(ZoneInfo(config.RELATIONSHIP_TIMEZONE)) \
                .date().isoformat()
            today_count = conn.execute(
                "SELECT COUNT(*) AS c FROM deletion_requests WHERE local_date=?",
                (local_date,)).fetchone()["c"]
            if today_count >= DAILY_LIMIT:
                raise Forbidden("daily deletion request limit reached",
                                code="daily_limit")
            rid = f"del_{uuid.uuid4().hex[:10]}"
            conn.execute(
                "INSERT INTO deletion_requests(id, resource_id, resource_kind, action,"
                " human_reason, ai_reason, status, submitted_by, submitted_at, local_date)"
                " VALUES(?,?, 'memory', ?,?,'', 'pending', ?,?,?)",
                (rid, resource_id, action, reason, principal_id,
                 _now().isoformat(), local_date))
            audit.record(conn, "deletion.requested", principal_id, resource_id=rid,
                         payload={"target": resource_id, "action": action})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"request_id": rid, "status": "pending", "action": action}


def deletion_withdraw(principal_id: str, resource_id: str) -> dict:
    """撤回该资源最近一条 pending。"""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM deletion_requests WHERE resource_id=? AND"
                " status='pending' ORDER BY submitted_at DESC LIMIT 1",
                (resource_id,)).fetchone()
            if row is None:
                raise NotFound("pending deletion request not found",
                               resource_id=resource_id)
            conn.execute(
                "UPDATE deletion_requests SET status='withdrawn', decided_at=?,"
                " decided_by=? WHERE id=?",
                (_now().isoformat(), principal_id, row["id"]))
            audit.record(conn, "deletion.withdrawn", principal_id,
                         resource_id=row["id"])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"request_id": row["id"], "status": "withdrawn"}


def _target_active(conn, resource_id: str) -> bool:
    r = conn.execute("SELECT visibility FROM memories WHERE memory_id=?",
                     (resource_id,)).fetchone()
    return bool(r) and r["visibility"] == "active"


def deletion_decide(principal_id: str, request_id: str, decision: str,
                    ai_reason: str = "", expected_resource_id: str = "") -> dict:
    """AI 侧（周家明）审批；approve 执行 archive/delete。

    审计 F38：状态迁移在写事务内以 pending 为条件做 CAS——并发的
    approve/reject 只有一个能落定，另一个得到结构化 ALREADY_DECIDED；
    状态更新与删除副作用同一事务提交，数据库状态与实际副作用一致。
    """
    if principal_id != "jiaming":
        raise Forbidden("only jiaming decides deletion requests",
                        principal=principal_id)
    if decision not in ("approve", "reject"):
        raise Forbidden("decision must be approve or reject")
    with db.formal() as conn:
        row = conn.execute("SELECT * FROM deletion_requests WHERE id=?",
                           (request_id,)).fetchone()
        if row is None or row["status"] != "pending":
            raise NotFound("pending deletion request not found", request_id=request_id)
        if expected_resource_id and expected_resource_id != row["resource_id"]:
            raise Forbidden("deletion request does not match resource_id",
                            code="bucket_mismatch")
        if not _target_active(conn, row["resource_id"]):
            supersede_conflict = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE deletion_requests SET status='superseded',"
                    " decided_at=?, decided_by=? WHERE id=?"
                    " AND status='pending'",
                    (_now().isoformat(), principal_id, request_id))
                if cur.rowcount != 1:
                    # N12：CAS 输家——不在 try 内 raise（外层 except 的
                    # ROLLBACK 会因事务已结束再炸，掩盖真实错误）
                    supersede_conflict = AlreadyDecided(
                        "deletion request already decided by a concurrent "
                        "decision", request_id=request_id)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            if supersede_conflict is not None:
                raise supersede_conflict
            raise Forbidden("deletion request target is no longer active",
                            code="superseded")
        conn.execute("BEGIN IMMEDIATE")
        try:
            status = "approved" if decision == "approve" else "rejected"
            # CAS：只有仍处于 pending 的申请能被本次决定落定
            cur = conn.execute(
                "UPDATE deletion_requests SET status=?, ai_reason=?,"
                " decided_at=?, decided_by=? WHERE id=? AND status='pending'",
                (status, str(ai_reason or "").strip(), _now().isoformat(),
                 principal_id, request_id))
            if cur.rowcount != 1:
                raise AlreadyDecided(
                    "deletion request already decided by a concurrent "
                    "decision", request_id=request_id)
            if decision == "approve":
                _execute(conn, row, principal_id)
            audit.record(conn, f"deletion.{status}", principal_id,
                         resource_id=request_id,
                         payload={"target": row["resource_id"],
                                  "action": row["action"]})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"request_id": request_id, "decision": decision,
            "resource_id": row["resource_id"], "status": status}


def _blocking_references(conn, rid: str) -> dict[str, int]:
    """阻止物理删除的正式跨域引用（审计 F34）。

    I revision 关系与 Source 绑定是跨域历史引用：物理删除 memory 会
    破坏其完整性。返回 {引用类型: 行数}；空 dict = 无引用可删。
    """
    out: dict[str, int] = {}
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM i_revision_memory_relations"
        " WHERE memory_id=?", (rid,)).fetchone()["c"]
    if n:
        out["i_revision_relations"] = n
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM memory_source_bindings"
        " WHERE memory_id=?", (rid,)).fetchone()["c"]
    if n:
        out["source_bindings"] = n
    return out


def _execute(conn, row, actor: str) -> None:
    """approve 后执行：archive -> visibility=archived；delete -> 物理删除+清派生。"""
    rid = row["resource_id"]
    if row["action"] == "archive":
        conn.execute("UPDATE memories SET visibility='archived',"
                     " updated_at=? WHERE memory_id=?",
                     (_now().isoformat(), rid))
        projection.remove(conn, rid)  # 归档即无默认投影
        return
    # delete：物理删除（有审批+限额+审计门槛；继承旧 HumanDeleteExecutor 语义）
    # 审计 F34：存在 I revision 关系 / Source 绑用的记忆不做物理删除，
    # 结构化拒绝（引用完整性优先），调用方可改走 archive。
    refs = _blocking_references(conn, rid)
    if refs:
        raise DeleteBlocked(
            "memory 被正式跨域引用，不允许物理删除；请先解除引用或"
            "改用 archive", memory_id=rid, references=refs)
    # v2 分层子表全清（B08：v1 清单不含分类/心情/话语/回忆/keep 行，
    # v2 桶会 FK 失败）
    for table in ("memory_raw_refs", "memory_categories", "memory_tags",
                  "memory_moods", "memory_mood_tags", "memory_our_words",
                  "memory_recollections", "memory_view_receipts",
                  "memory_reengagements", "memory_meanings",
                  "memory_keeps", "field_search_docs", "field_fts",
                  "search_fts", "retrieval_documents", "memory_versions"):
        conn.execute(f"DELETE FROM {table} WHERE memory_id=?", (rid,))
    conn.execute("DELETE FROM memory_relations WHERE from_memory=?"
                 " OR to_memory=?", (rid, rid))
    conn.execute("DELETE FROM plan_memory_links WHERE memory_id=?", (rid,))
    conn.execute("DELETE FROM memories WHERE memory_id=?", (rid,))


def deletion_list(status: str | None = None) -> list[dict]:
    with db.formal() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM deletion_requests WHERE status=?"
                " ORDER BY submitted_at DESC", (status,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM deletion_requests ORDER BY submitted_at DESC").fetchall()
    return [dict(r) for r in rows]
