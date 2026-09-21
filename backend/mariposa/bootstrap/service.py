"""两入口开窗（§12）。

- Claude Chat：30 条真实原文 + 近三天正式桶（遗忘桶=批准摘要）+ 进行中/临近计划
- CC：三天桶 + 同样计划；原文不自动读，按需调 raw 工具
- 服务端按绑定 entry_source 校验 profile；worker 不得传 profile 冒充
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from .. import config, db
from ..errors import Forbidden, SnapshotStale
from ..memory import service as memory
from ..plans import service as plans
from ..raw import service as raw

BOOT_MEMORY_DAYS = 3
BOOT_DAY_WINDOW_MODE = "calendar_days"  # 今天+前两天（自然日），非最近72小时
PLAN_UPCOMING_DAYS = 7
BOOT_SOFT_TOKEN_BUDGET = 16000
BOOT_SECTION_LIMIT = 50  # 每段上限；超出走 cursor，不静默截断

_ENTRY_ALLOWED = {
    "claude_chat": {"claude_chat"},
    "cc": {"cc"},
}


def _state_hash(conn) -> str:
    """开窗依据资源的状态指纹：任一变化使旧 snapshot 失效（§12.2）。"""
    import hashlib
    parts = []
    for table, time_col in (("memories", "updated_at"), ("plans", "updated_at"),
                            ("raw_messages", "occurred_at")):
        row = conn.execute(
            f"SELECT COUNT(*) AS c, MAX({time_col}) AS m FROM {table}").fetchone()
        parts.append(f"{table}:{row['c']}:{row['m']}")
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def get(principal_id: str, entry_source: str, profile: str,
        loaded_snapshot_id: str | None = None,
        cursor: dict | None = None) -> dict:
    if principal_id != "jiaming":
        raise Forbidden("bootstrap is for jiaming entries", principal=principal_id)
    if profile not in _ENTRY_ALLOWED:
        raise Forbidden(f"unknown profile: {profile}")
    if entry_source not in _ENTRY_ALLOWED[profile]:
        raise Forbidden(
            "entry_source does not match bootstrap profile",
            entry_source=entry_source, profile=profile)

    with db.formal() as conn:
        current_state = _state_hash(conn)
        if loaded_snapshot_id:
            snap = conn.execute(
                "SELECT state_hash FROM bootstrap_snapshots WHERE snapshot_id=?",
                (loaded_snapshot_id,)).fetchone()
            if snap is None:
                raise SnapshotStale("snapshot unknown; re-fetch bootstrap")
            if snap["state_hash"] != current_state:
                raise SnapshotStale(
                    "underlying resources changed since snapshot; re-fetch",
                    snapshot_id=loaded_snapshot_id)

    tz = ZoneInfo(config.RELATIONSHIP_TIMEZONE)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).astimezone(tz).date()
    three_days = [(today - timedelta(days=i)).isoformat() for i in range(BOOT_MEMORY_DAYS)]

    with db.formal() as conn:
        mem_rows = conn.execute(
            "SELECT memory_id, memory_date FROM memories WHERE visibility='active'"
            " AND memory_date IN (?,?,?) ORDER BY memory_date DESC, memory_id"
            " LIMIT ?", tuple(three_days) + (BOOT_SECTION_LIMIT,)).fetchall()
        memory_items = [memory.get(conn, r["memory_id"]) for r in mem_rows]
        mem_total = conn.execute(
            "SELECT COUNT(*) AS c FROM memories WHERE visibility='active'"
            " AND memory_date IN (?,?,?)", tuple(three_days)).fetchone()["c"]
    plan_items = plans.bootstrap_plans(today, PLAN_UPCOMING_DAYS)
    plan_page = plan_items[:BOOT_SECTION_LIMIT]
    plan_cursor = {"plans_offset": BOOT_SECTION_LIMIT} \
        if len(plan_items) > BOOT_SECTION_LIMIT else {"plans_offset": None}

    # 三天桶分页 cursor：数量上限显式可见，不静默截断（§12.2）
    if mem_total > len(memory_items):
        last = mem_rows[-1]
        mem_cursor = {"memory_before_date": last["memory_date"],
                      "memory_last_id": last["memory_id"],
                      "remaining": mem_total - len(memory_items)}
    else:
        mem_cursor = None

    result: dict = {
        "snapshot_id": f"snap_{uuid.uuid4().hex[:12]}",
        "state_hash": current_state,
        "profile": profile,
        "time": {"local_date": today.isoformat(), "timezone": config.RELATIONSHIP_TIMEZONE},
        "memory_days": {
            "dates": three_days, "mode": BOOT_DAY_WINDOW_MODE,
            "items": memory_items, "count": len(memory_items),
            "total_in_window": mem_total,
            "section_limit": BOOT_SECTION_LIMIT,
            "next_cursor": mem_cursor,
        },
        "plans": {"items": plan_page, "count": len(plan_page),
                  "total": len(plan_items),
                  "upcoming_days": PLAN_UPCOMING_DAYS,
                  "section_limit": BOOT_SECTION_LIMIT,
                  "next_cursor": plan_cursor},
        "policy": {
            "boot_memory_days": BOOT_MEMORY_DAYS,
            "plan_upcoming_days": PLAN_UPCOMING_DAYS,
            "soft_token_budget": BOOT_SOFT_TOKEN_BUDGET,
        },
        "coverage": {"raw": "not_applicable"},
    }

    if profile == "claude_chat":
        raw_msgs = raw.list_recent(raw.BOOT_RAW_MESSAGES,
                                   before=(cursor or {}).get("raw_before"))
        result["raw"] = {
            "count": len(raw_msgs), "requested": raw.BOOT_RAW_MESSAGES,
            "counts_messages_not_turns": True,
            "messages": raw_msgs,
        }
        # 分页 cursor：还有更早消息时给 next（§12.2 不静默截断）
        with db.formal() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM raw_messages").fetchone()["c"]
        if raw_msgs and total > raw.BOOT_RAW_MESSAGES:
            oldest = min(m["occurred_at"] for m in raw_msgs)
            remaining = total - len(raw_msgs)
            result["cursor"] = {
                "next": {"raw_before": oldest},
                "remaining_raw": remaining,
                "note": "用 bootstrap.next 续取；不静默截断约定资料",
            }
        else:
            result["cursor"] = {"next": None}
        if len(raw_msgs) < raw.BOOT_RAW_MESSAGES:
            result["coverage"] = {
                "raw": f"only {len(raw_msgs)} messages available; not padded"}
    else:
        result["cursor"] = {"next": None}

    _estimate_budget(result)
    with db.formal() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bootstrap_snapshots(snapshot_id, state_hash,"
            " profile, created_at) VALUES(?,?,?,datetime('now'))",
            (result["snapshot_id"], current_state, profile))
    return result


def next_page(principal_id: str, entry_source: str, snapshot_id: str,
              cursor: dict, section: str = "raw") -> dict:
    """续取开窗分页（raw / memory_days / plans）；snapshot 变化即 SNAPSHOT_STALE。"""
    if principal_id != "jiaming":
        raise Forbidden("bootstrap is for jiaming entries", principal=principal_id)
    if section not in ("raw", "memory_days", "plans"):
        raise Forbidden(f"unknown section: {section}")
    with db.formal() as conn:
        snap = conn.execute(
            "SELECT * FROM bootstrap_snapshots WHERE snapshot_id=?",
            (snapshot_id,)).fetchone()
        if snap is None:
            raise SnapshotStale("snapshot unknown; re-fetch bootstrap")
        if snap["state_hash"] != _state_hash(conn):
            raise SnapshotStale("underlying resources changed; re-fetch bootstrap",
                                snapshot_id=snapshot_id)

    if section == "raw":
        if snap["profile"] == "cc":
            raise Forbidden("cc profile has no raw section to paginate")
        if not cursor or not cursor.get("raw_before"):
            raise Forbidden("cursor.raw_before required")
        msgs = raw.list_recent(raw.BOOT_RAW_MESSAGES, before=cursor["raw_before"])
        out: dict = {"snapshot_id": snapshot_id, "section": "raw", "raw": {
            "count": len(msgs), "messages": msgs,
            "counts_messages_not_turns": True}}
        if len(msgs) < raw.BOOT_RAW_MESSAGES:
            out["cursor"] = {"next": None}
        else:
            oldest = min(m["occurred_at"] for m in msgs)
            out["cursor"] = {"next": {"raw_before": oldest}}
        return out

    tz = ZoneInfo(config.RELATIONSHIP_TIMEZONE)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).astimezone(tz).date()
    three_days = [(today - timedelta(days=i)).isoformat()
                  for i in range(BOOT_MEMORY_DAYS)]

    if section == "memory_days":
        before_date = (cursor or {}).get("memory_before_date")
        last_id = (cursor or {}).get("memory_last_id")
        if not before_date:
            raise Forbidden("cursor.memory_before_date required")
        with db.formal() as conn:
            rows = conn.execute(
                "SELECT memory_id, memory_date FROM memories WHERE"
                " visibility='active' AND memory_date IN (?,?,?)"
                " AND (memory_date < ? OR (memory_date = ? AND memory_id > ?))"
                " ORDER BY memory_date DESC, memory_id LIMIT ?",
                tuple(three_days) + (before_date, before_date, last_id or "",
                                     BOOT_SECTION_LIMIT)).fetchall()
            items = [memory.get(conn, r["memory_id"]) for r in rows]
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE visibility='active'"
                " AND memory_date IN (?,?,?)", tuple(three_days)).fetchone()["c"]
        nxt = None
        if rows and total > len(items):
            # 粗略剩余估计：窗口总数 - 本页起点之前的项不可知，用下一 cursor 表达
            last = rows[-1]
            nxt = {"memory_before_date": last["memory_date"],
                   "memory_last_id": last["memory_id"]}
        return {"snapshot_id": snapshot_id, "section": "memory_days",
                "items": items, "count": len(items),
                "total_in_window": total, "next_cursor": nxt}

    # plans：offset 游标
    offset = int((cursor or {}).get("plans_offset") or 0)
    all_plans = plans.bootstrap_plans(today, PLAN_UPCOMING_DAYS)
    page = all_plans[offset:offset + BOOT_SECTION_LIMIT]
    nxt = offset + BOOT_SECTION_LIMIT if offset + BOOT_SECTION_LIMIT < len(all_plans) else None
    return {"snapshot_id": snapshot_id, "section": "plans",
            "items": page, "count": len(page), "total": len(all_plans),
            "next_cursor": {"plans_offset": nxt}}


def _estimate_budget(result: dict) -> None:
    """粗估（chars/1.5）；超预算显式提示，不静默截断约定资料。"""
    chars = 0
    for m in result.get("memory_days", {}).get("items", []):
        chars += len(m.get("text") or "")
    for p in result.get("plans", {}).get("items", []):
        chars += len(p.get("title") or "") + len(p.get("content") or "")
    for m in result.get("raw", {}).get("messages", []):
        chars += len(m.get("body") or "")
    est = int(chars / 1.5)
    result["estimated_tokens"] = est
    if est > BOOT_SOFT_TOKEN_BUDGET:
        result["warnings"] = [{
            "code": "SOFT_BUDGET_EXCEEDED",
            "message": f"估算 {est} tokens 超过软预算 {BOOT_SOFT_TOKEN_BUDGET}；"
                       "本版不静默截断，分页机制为后续交付",
        }]
