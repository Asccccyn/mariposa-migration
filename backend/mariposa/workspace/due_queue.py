"""持久到期队列（spec_v2 §7.3）：租约、失败重试、dead-letter。

到期的真源是 retention 行（记忆）与 plans 终结锚点（计划）；本队列是
调度派生物：按业务自然日检查 due_date，只创建候选任务，不直接修改正式
表示。重算/重启执行时替换对应待处理行；扫描消费用确定排序，不饥饿。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import db
from ..memory import retention as retention_mod

MAX_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(conn, target_kind: str, target_id: str, due_date: str) -> None:
    """入队（幂等：同目标同 due_date 只一行）。必须在正式库事务内。"""
    if not due_date:
        return
    conn.execute(
        "INSERT OR IGNORE INTO forgetting_due_queue(item_id, target_kind,"
        " target_id, due_date, status, created_at, updated_at)"
        " VALUES(?,?,?,?, 'pending', ?, ?)",
        (f"due_{uuid.uuid4().hex[:14]}", target_kind, target_id, due_date,
         _now(), _now()))


def replace_pending(conn, target_kind: str, target_id: str,
                    due_date: str | None) -> None:
    """期限重算/重启执行：清掉旧待处理行，按新 due_date 重建。"""
    conn.execute(
        "DELETE FROM forgetting_due_queue WHERE target_kind=? AND target_id=?"
        " AND status IN ('pending','failed')",
        (target_kind, target_id))
    enqueue(conn, target_kind, target_id, due_date or "")


def due_items(today: str | None = None, limit: int = 50) -> list[dict]:
    """到期候选（business_today >= due_date），确定排序不饥饿。"""
    t = today or retention_mod.business_today().isoformat()
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT * FROM forgetting_due_queue"
            " WHERE status IN ('pending','failed') AND due_date <= ?"
            " ORDER BY due_date, target_id LIMIT ?",
            (t, limit)).fetchall()
    return [dict(r) for r in rows]


def lease(item_id: str, lease_seconds: int = 600) -> dict | None:
    """认领租约（pending/失败重试 → leased）。"""
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE forgetting_due_queue SET status='leased', lease_id=?,"
                " lease_expires_at=?, updated_at=? WHERE item_id=? AND"
                " status IN ('pending','failed')",
                (f"lease_{uuid.uuid4().hex[:10]}", now, now, item_id))
            conn.execute("COMMIT")
            claimed = cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return _get(item_id) if claimed else None


def _get(item_id: str) -> dict | None:
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM forgetting_due_queue WHERE item_id=?",
            (item_id,)).fetchone()
    return dict(row) if row else None


def complete(item_id: str) -> None:
    with db.formal() as conn:
        conn.execute(
            "UPDATE forgetting_due_queue SET status='done', lease_id=NULL,"
            " updated_at=? WHERE item_id=?", (_now(), item_id))


def fail(item_id: str, error: str) -> dict:
    """失败重试：超过 MAX_ATTEMPTS 进 dead-letter，不再自动重试。"""
    from ..errors import NotFound
    with db.formal() as conn:
        row = conn.execute(
            "SELECT attempts FROM forgetting_due_queue WHERE item_id=?",
            (item_id,)).fetchone()
        if row is None:
            raise NotFound("due queue item not found", item_id=item_id)
        attempts = row["attempts"] + 1
        status = "dead" if attempts >= MAX_ATTEMPTS else "failed"
        conn.execute(
            "UPDATE forgetting_due_queue SET status=?, attempts=?,"
            " last_error=?, lease_id=NULL, updated_at=? WHERE item_id=?",
            (status, attempts, error[:500], _now(), item_id))
    return {"item_id": item_id, "attempts": attempts, "status": status}


def reclaim_expired_leases() -> int:
    """崩溃租约回收：过期 leased → pending（可重试）。"""
    now = _now()
    with db.formal() as conn:
        cur = conn.execute(
            "UPDATE forgetting_due_queue SET status='pending', lease_id=NULL,"
            " updated_at=? WHERE status='leased' AND lease_expires_at < ?",
            (now, now))
        return cur.rowcount


def stats() -> dict:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM forgetting_due_queue"
            " GROUP BY status").fetchall()
    return {r["status"]: r["c"] for r in rows}
