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
    """在调用方事务内写 audit_events + events_outbox。返回 event_id。"""
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
    conn.execute(
        "INSERT INTO events_outbox(event_id, event_type, created_at, payload)"
        " VALUES(?,?,?,?)",
        (event_id, event_type, occurred, json.dumps(
            {"event_type": event_type, "resource_id": resource_id,
             "resource_version": resource_version, **(payload or {})},
            ensure_ascii=False,
        )),
    )
    return event_id


def record_isolated(event_type: str, actor_principal: str,
                    resource_id: str | None = None,
                    payload: dict | None = None) -> str:
    """独立事务的审计写入（GATE-06，2026-10-04 复审 P2）。

    供门禁等没有外层事务的模块使用：audit_events 与 events_outbox
    两表 INSERT 在同一 BEGIN/COMMIT 内——半途失败整体回滚，不再留
    "已提交但无法发布"的孤立事件（db.formal 默认 autocommit，两个
    INSERT 裸跑会各自提交）。
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
