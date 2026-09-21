"""提醒（§16.1）：时区、计划时间、状态；到期结算走 maintenance.reminders_fire_due。

AUTO_WAKEUP_ENABLED=false（§21 默认）：本模块不产生任何自动唤醒副作用；
scheduler 常驻为后续交付。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(principal_id: str, title: str, remind_at: str, note: str | None = None,
           timezone_name: str | None = None) -> dict:
    if not title or not str(title).strip():
        raise Forbidden("reminder title required")
    try:
        datetime.fromisoformat(str(remind_at).replace("Z", "+00:00"))
    except ValueError:
        raise Forbidden("remind_at must be ISO-8601")
    rid = f"rem_{uuid.uuid4().hex[:10]}"
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO reminders(id, principal, title, note, remind_at,"
                " timezone, status, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,'scheduled',?,?)",
                (rid, principal_id, title, note, remind_at, timezone_name, now, now))
            audit.record(conn, "reminder.created", principal_id, resource_id=rid,
                         payload={"remind_at": remind_at})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"reminder_id": rid, "status": "scheduled"}


def list_reminders(states: list[str] | None = None) -> list[dict]:
    with db.formal() as conn:
        if states:
            marks = ",".join("?" * len(states))
            rows = conn.execute(
                f"SELECT * FROM reminders WHERE status IN ({marks})"
                " ORDER BY remind_at", tuple(states)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reminders ORDER BY remind_at").fetchall()
    return [dict(r) for r in rows]


def cancel(principal_id: str, reminder_id: str) -> dict:
    with db.formal() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id=?",
                           (reminder_id,)).fetchone()
        if row is None:
            raise NotFound("reminder not found", reminder_id=reminder_id)
        if row["status"] != "scheduled":
            raise Forbidden("only scheduled reminders can be cancelled",
                            status=row["status"])
        conn.execute(
            "UPDATE reminders SET status='cancelled', updated_at=? WHERE id=?",
            (_now(), reminder_id))
    return {"reminder_id": reminder_id, "status": "cancelled"}
