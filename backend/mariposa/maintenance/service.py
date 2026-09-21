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


def reminders_fire_due(now: str | None = None) -> dict:
    """到期提醒惰性结算：标记 fired（幂等）。无通知通道时不发任何外部副作用。

    常驻 scheduler = 后续交付（AUTO_WAKEUP_ENABLED=false）；
    本入口供手动/未来 scheduler 调用。
    """
    now = now or _now()
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT id, title FROM reminders WHERE status='scheduled'"
            " AND remind_at<=?", (now,)).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE reminders SET status='fired', updated_at=? WHERE id=?"
                " AND status='scheduled'", (now, r["id"]))
    return {"fired": [dict(r) for r in rows]}


def reconcile_workspace() -> dict:
    """按正式库终局决议对账工作区状态（§6.4.6）。

    approved 的 resolution 若工作区未回填 -> 回填 approved_and_applied；
    rejected/withdrawn 同理。崩溃/中断后调用，不重复应用正文。
    """
    from .. import db as _db
    fixed = 0
    with _db.formal() as fconn:
        resolutions = fconn.execute(
            "SELECT proposal_id, decision FROM proposal_resolutions").fetchall()
    res_map = {r["proposal_id"]: r["decision"] for r in resolutions}
    with _db.workspace() as wconn:
        for pid, decision in res_map.items():
            item = wconn.execute(
                "SELECT state FROM work_items WHERE item_id=?", (pid,)).fetchone()
            if item is None:
                continue
            target = {"approved": "approved_and_applied",
                      "rejected": "rejected",
                      "withdrawn": "withdrawn"}[decision]
            if item["state"] != target:
                wconn.execute(
                    "UPDATE work_items SET state=?, updated_at=? WHERE item_id=?",
                    (target, _now(), pid))
                fixed += 1
    return {"checked": len(res_map), "fixed": fixed}
