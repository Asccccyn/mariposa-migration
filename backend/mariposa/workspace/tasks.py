"""工作区任务租约（§7.4）：list/claim/release + 授权材料 inspect。

任务=当前可认领的工作项；租约持久化（模型会话结束不丢），
过期可被重领。inspect 只返回授权任务的材料，不默认全库。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from .. import db
from ..errors import Forbidden, NotFound

_LEASE_MINUTES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def tasks_list() -> list[dict]:
    with db.workspace() as wconn:
        rows = wconn.execute(
            "SELECT item_id, item_type, state, current_revision, updated_at"
            " FROM work_items WHERE state IN ('draft','submitted','deferred')"
            " ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def task_claim(principal_id: str, task_key: str) -> dict:
    """认领任务：未过期有效租约存在则拒绝（FIFO 不抢占）。"""
    now = _now()
    expires = now + timedelta(minutes=_LEASE_MINUTES)
    lease_id = f"lease_{uuid.uuid4().hex[:10]}"
    with db.workspace() as wconn:
        active = wconn.execute(
            "SELECT * FROM workspace_task_leases WHERE task_key=? AND"
            " released=0 AND expires_at>?",
            (task_key, now.isoformat())).fetchone()
        if active:
            raise Forbidden("task already leased",
                            code="LEASE_HELD", lease_id=active["lease_id"],
                            expires_at=active["expires_at"])
        wconn.execute(
            "INSERT INTO workspace_task_leases(lease_id, task_key, claimed_by,"
            " claimed_at, expires_at) VALUES(?,?,?,?,?)",
            (lease_id, task_key, principal_id, now.isoformat(),
             expires.isoformat()))
    return {"lease_id": lease_id, "task_key": task_key,
            "expires_at": expires.isoformat()}


def task_release(principal_id: str, lease_id: str) -> dict:
    with db.workspace() as wconn:
        row = wconn.execute(
            "SELECT * FROM workspace_task_leases WHERE lease_id=?",
            (lease_id,)).fetchone()
        if row is None or row["released"]:
            raise NotFound("lease not found", lease_id=lease_id)
        if row["claimed_by"] != principal_id:
            raise Forbidden("only the claimer may release")
        wconn.execute(
            "UPDATE workspace_task_leases SET released=1 WHERE lease_id=?",
            (lease_id,))
    return {"lease_id": lease_id, "released": True}


def memory_inspect(principal_id: str, memory_id: str) -> dict:
    """授权任务材料读取：返回桶当前表示、版本链元数据、开放提案。"""
    from ..memory import service as memory
    with db.formal() as conn:
        rep = memory.get(conn, memory_id)
        versions = conn.execute(
            "SELECT version_no, representation, origin_kind, created_at"
            " FROM memory_versions WHERE memory_id=? ORDER BY version_no",
            (memory_id,)).fetchall()
    with db.workspace() as wconn:
        proposals = wconn.execute(
            "SELECT item_id, state, updated_at FROM work_items WHERE"
            " target_memory_id=? AND state IN ('draft','submitted','deferred')",
            (memory_id,)).fetchall()
    return {"memory": rep,
            "versions": [dict(v) for v in versions],
            "open_proposals": [dict(p) for p in proposals]}
