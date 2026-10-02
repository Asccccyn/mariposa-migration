"""运维能力：outbox 消费、活动查询、提醒到期（§16.1/§19.2/§17.3）。"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import db
from ..errors import Forbidden


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def outbox_drain(limit: int = 100) -> dict:
    """至少一次投递 + 幂等消费的第一版消费者：逐条标记 processed。

    异步下游（embedding/日历缓存/工作区回填）接入点在此注册；
    当前无注册消费者时仅做确认性标记并返回统计。
    """
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT event_id, event_type FROM events_outbox WHERE processed=0"
            " ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        for r in rows:
            conn.execute("UPDATE events_outbox SET processed=1 WHERE event_id=?",
                         (r["event_id"],))
    return {"drained": len(rows),
            "types": sorted({r["event_type"] for r in rows})}


def outbox_status() -> dict:
    with db.formal() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS pending FROM events_outbox WHERE processed=0"
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) AS t FROM events_outbox").fetchone()["t"]
    return {"pending": row["pending"], "total": total}


def activity_list(limit: int = 50, event_type: str | None = None) -> list[dict]:
    """审计查询（管理接口；不参与记忆召回，§19.3）。"""
    with db.formal() as conn:
        if event_type:
            rows = conn.execute(
                "SELECT event_id, event_type, occurred_at, actor_principal,"
                " entry_source, resource_id, resource_version FROM audit_events"
                " WHERE event_type=? ORDER BY occurred_at DESC LIMIT ?",
                (event_type, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT event_id, event_type, occurred_at, actor_principal,"
                " entry_source, resource_id, resource_version FROM audit_events"
                " ORDER BY occurred_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]





def idempotency_reconcile(principal_id: str, record_principal: str,
                          capability: str, idempotency_key: str,
                          stale_seconds: int = 60) -> dict:
    """崩溃窗口对账（§13.2）：把疑似中途崩溃的 running 幂等记录显式标记 failed。

    只有 failed 之后同 key 重试才能重新占位执行。调用方必须先核实业务结果
    （副作用可能已发生）；本工具只清除占位，不伪造结果。
    """
    from ..errors import NotFound as _NF
    from datetime import datetime as _dt
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE principal_id=? AND"
            " capability=? AND idempotency_key=?",
            (record_principal, capability, idempotency_key)).fetchone()
        if row is None:
            raise _NF("idempotency record not found",
                      principal=record_principal, capability=capability,
                      key=idempotency_key)
        if row["status"] != "running":
            return {"reconciled": False, "status": row["status"],
                    "note": "record is not running; nothing to reconcile"}
        created = _dt.fromisoformat(row["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age <= stale_seconds:
            return {"reconciled": False, "status": "running",
                    "age_seconds": int(age),
                    "note": "record still fresh; concurrent execution may be"
                            " in flight; refuse to reconcile"}
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE idempotency_records SET status='failed', result_ref=?"
                " WHERE principal_id=? AND capability=? AND idempotency_key=?"
                " AND status='running'",
                (None, record_principal, capability, idempotency_key))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"reconciled": True, "status": "failed",
            "age_seconds": int(age),
            "reconciled_by": principal_id,
            "note": "占位已清除；副作用是否已发生须由调用方核实业务状态，"
                    "确认后同 key 重试将重新执行"}


def jobs_status() -> dict:
    """维护任务状态总览：outbox 待处理、导入任务。

    CB-051（2026-10-02 审计 P2）：Workspace Tasks/Lease 已退役——
    不再查询已删除的 workspace_task_leases/work_items（此前该状态页
    在 fresh schema 上直接 no such table 崩溃）。"""
    with db.formal() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM events_outbox WHERE processed=0"
        ).fetchone()["c"]
        imports = conn.execute(
            "SELECT status, COUNT(*) AS c FROM import_jobs GROUP BY status"
        ).fetchall()
    return {"outbox_pending": pending,
            "import_jobs": {r["status"]: r["c"] for r in imports}}


def idempotency_reconcile(principal_id: str, record_principal: str,
                          capability: str, idempotency_key: str,
                          stale_seconds: int = 60) -> dict:
    """崩溃窗口对账（§13.2）：把疑似中途崩溃的 running 幂等记录显式标记 failed。

    只有 failed 之后同 key 重试才能重新占位执行。调用方必须先核实业务结果
    （副作用可能已发生）；本工具只清除占位，不伪造结果。
    """
    from ..errors import NotFound as _NF
    from datetime import datetime as _dt
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE principal_id=? AND"
            " capability=? AND idempotency_key=?",
            (record_principal, capability, idempotency_key)).fetchone()
        if row is None:
            raise _NF("idempotency record not found",
                      principal=record_principal, capability=capability,
                      key=idempotency_key)
        if row["status"] != "running":
            return {"reconciled": False, "status": row["status"],
                    "note": "record is not running; nothing to reconcile"}
        created = _dt.fromisoformat(row["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age <= stale_seconds:
            return {"reconciled": False, "status": "running",
                    "age_seconds": int(age),
                    "note": "record still fresh; concurrent execution may be"
                            " in flight; refuse to reconcile"}
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE idempotency_records SET status='failed', result_ref=?"
                " WHERE principal_id=? AND capability=? AND idempotency_key=?"
                " AND status='running'",
                (None, record_principal, capability, idempotency_key))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"reconciled": True, "status": "failed",
            "age_seconds": int(age),
            "reconciled_by": principal_id,
            "note": "占位已清除；副作用是否已发生须由调用方核实业务状态，"
                    "确认后同 key 重试将重新执行"}
