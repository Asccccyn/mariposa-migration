"""审计与 outbox：与正式写入同一事务产生（§19）。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .. import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(
    conn,
    event_type: str,
    actor_principal: str,
    resource_id: str | None = None,
    resource_version: int | None = None,
    payload: dict | None = None,
    initiated_by: str | None = None,
    executor_kind: str | None = None,
    entry_source: str | None = None,
    correlation_id: str | None = None,
) -> str:
    """在调用方事务内写 audit_events（单一正本）。返回 event_id。

    WP-05（D05a）：events_outbox 停写——无注册消费者期间双写只是
    重复账本；历史行保留只读（maintenance.outbox.drain/status）。
    """
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    occurred = _now()
    safe_payload = json.dumps(payload or {}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO audit_events(event_id, event_type, occurred_at, actor_principal,"
        " initiated_by, executor_kind, entry_source, resource_id, resource_version,"
        " payload, correlation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id, event_type, occurred, actor_principal, initiated_by,
            executor_kind, entry_source, resource_id, resource_version,
            safe_payload, correlation_id,
        ),
    )
    return event_id


def record_isolated(event_type: str, actor_principal: str,
                    resource_id: str | None = None,
                    payload: dict | None = None) -> str:
    """独立事务的审计写入（GATE-06，2026-10-04 复审 P2）。

    供门禁等没有外层事务的模块使用：audit_events INSERT 在显式
    BEGIN/COMMIT 内（db.formal 默认 autocommit，裸跑 INSERT 的事务性
    不可靠）。WP-05（D05a）后 events_outbox 已停写。
    """
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            event_id = record(conn, event_type, actor_principal,
                              resource_id=resource_id, payload=payload)
            conn.execute("COMMIT")
            return event_id
        except Exception:
            conn.execute("ROLLBACK")
            raise
