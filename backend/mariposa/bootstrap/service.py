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
        loaded_snapshot_id: str | None = None) -> dict:
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
            "SELECT memory_id FROM memories WHERE visibility='active'"
            " AND memory_date IN (?,?,?) ORDER BY memory_date DESC",
            tuple(three_days)).fetchall()
        memory_items = [memory.get(conn, r["memory_id"]) for r in mem_rows]
    plan_items = plans.bootstrap_plans(today, PLAN_UPCOMING_DAYS)

    result: dict = {
        "snapshot_id": f"snap_{uuid.uuid4().hex[:12]}",
        "state_hash": current_state,
        "profile": profile,
        "time": {"local_date": today.isoformat(), "timezone": config.RELATIONSHIP_TIMEZONE},
        "memory_days": {
            "dates": three_days, "mode": BOOT_DAY_WINDOW_MODE,
            "items": memory_items, "count": len(memory_items),
        },
        "plans": {"items": plan_items, "count": len(plan_items),
                  "upcoming_days": PLAN_UPCOMING_DAYS},
        "policy": {
            "boot_memory_days": BOOT_MEMORY_DAYS,
            "plan_upcoming_days": PLAN_UPCOMING_DAYS,
            "soft_token_budget": BOOT_SOFT_TOKEN_BUDGET,
        },
        "coverage": {"raw": "not_applicable"},
    }

    if profile == "claude_chat":
        raw_msgs = raw.list_recent(raw.BOOT_RAW_MESSAGES)
        result["raw"] = {
            "count": len(raw_msgs), "requested": raw.BOOT_RAW_MESSAGES,
            "counts_messages_not_turns": True,
            "messages": raw_msgs,
        }
        if len(raw_msgs) < raw.BOOT_RAW_MESSAGES:
            result["coverage"] = {
                "raw": f"only {len(raw_msgs)} messages available; not padded"}

    _estimate_budget(result)
    with db.formal() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bootstrap_snapshots(snapshot_id, state_hash,"
            " created_at) VALUES(?,?,datetime('now'))",
            (result["snapshot_id"], current_state))
    return result


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
