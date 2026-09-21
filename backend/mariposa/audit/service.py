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
