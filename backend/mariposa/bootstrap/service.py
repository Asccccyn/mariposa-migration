"""两入口开窗（spec_v2 R20 / §11 / V2-BOOT）。

v2 默认包 = 最近三个自然日桶的 标题+心情标签+心情文字 + I + 提前 0..3
自然日进入临近的计划/纪念日全文；进行中/逾期未完成计划单列不消失。
普通事件不自动全文展开；**不默认附带旧版 30 条原文**（显式取源走 raw
工具并另行确认）。开窗不产生 view confirm，不续期。

- Claude Chat / CC 两个 profile 同一基础包（entry_source 校验不变）；
- 最近三天 = 事件日期的今天与前两天（自然日，非滚动 72 小时）；
- 临近按发生日期与今天之差 0..3 日（含恰好 3 日，按日期不按小时）；
- snapshot 指纹覆盖：记忆当前表示、计划、I、纪念日发生项；变化即失效。
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from .. import config, db
from ..errors import Forbidden, SnapshotStale
from ..memory import categories as cats_mod
from ..memory import service as memory
from ..plans import service as plans
from ..raw import service as raw  # noqa: F401 （显式取源仍走 raw 工具）

BOOT_MEMORY_DAYS = 3
BOOT_DAY_WINDOW_MODE = "calendar_days"  # 今天+前两天（自然日），非最近72小时
BOOT_UPCOMING_DAYS = 3  # 临近按日期差 0..3 日（含边界；S4/D03）
BOOT_SOFT_TOKEN_BUDGET = 16000
BOOT_SECTION_LIMIT = 50  # 每段上限；超出走 cursor，不静默截断

_ENTRY_ALLOWED = {
    "claude_chat": {"claude_chat"},
    "cc": {"cc"},
}


def _state_hash(conn) -> str:
    """开窗依据资源的状态指纹：任一变化使旧 snapshot 失效（§12.2）。

    v2 覆盖：记忆（含表示版本）、计划、I 正本、纪念日发生项；
    raw 不再属于开窗包，不参与指纹。
    """
    import hashlib
    parts = []
    for table, time_col, extra in (
            ("memories", "updated_at", ", MAX(representation_state) AS e"),
            ("plans", "updated_at", ""),
            ("i_documents", "updated_at", ""),
            ("anniversary_occurrences", "occurrence_date", "")):
        row = conn.execute(
            f"SELECT COUNT(*) AS c, MAX({time_col}) AS m {extra} FROM {table}"
        ).fetchone()
        parts.append(f"{table}:{row['c']}:{row['m']}")
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _memory_slim(conn, memory_id: str) -> dict:
    """三天桶条目：标题+心情标签+心情文字+分类；不默认展开事件正文。"""
    m = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                     (memory_id,)).fetchone()
    v = conn.execute(
        "SELECT original_title FROM memory_versions WHERE memory_id=?"
        " AND version_no=?", (memory_id, m["current_version_no"])).fetchone()
    mood = conn.execute(
        "SELECT mood_text FROM memory_moods WHERE memory_id=?",
        (memory_id,)).fetchone()
    tags = conn.execute(
        "SELECT tag FROM memory_mood_tags WHERE memory_id=? ORDER BY tag",
        (memory_id,)).fetchall()
    from ..memory import retention as ret_mod
    item = {
        "memory_id": memory_id,
        "memory_date": m["memory_date"],
        "original_title": v["original_title"] if v else None,
        "representation": m["compression_state"],
        "representation_version": m["representation_state"],
        "categories": cats_mod.list_of(conn, memory_id),
    }
    if mood is not None:
        item["mood_tags"] = [t["tag"] for t in tags]
        item["mood_text"] = mood["mood_text"]
    else:
        item["mood_tags"] = []
        item["mood_text"] = None  # 心情空白 ≠ 不重要（R05）
    # 补录标记（D03）：hold 日期晚于事件日期 → 新收录，不冒充刚发生
    if m["held_at"]:
        held_day = ret_mod.local_date(m["held_at"],
                                      config.RELATIONSHIP_TIMEZONE)
        try:
            ev_day = m["memory_date"][:10]
            item["late_entry"] = held_day.isoformat() > ev_day
        except (TypeError, ValueError):
            item["late_entry"] = None
    return item


def _anniversaries_upcoming(today, days: int) -> list[dict]:
    horizon = (today + timedelta(days=days)).isoformat()
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT d.definition_id, d.title, o.occurrence_date,"
            " d.memory_id FROM anniversary_occurrences o"
            " JOIN anniversary_definitions d ON d.definition_id=o.definition_id"
            " WHERE o.occurrence_date BETWEEN ? AND ?"
            " ORDER BY o.occurrence_date",
            (today.isoformat(), horizon)).fetchall()
    return [dict(r) for r in rows]


def _i_section() -> dict:
    from ..identity_i import service as i_svc
    doc = i_svc.get()
    return {"content": doc["content"], "version": doc["version"],
            "source": "i_documents" if doc["content"] is not None else None,
            "note": None if doc["content"] is not None else
                    "I 尚未落笔（无旧Self自动映射，V2-I-03）"}


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
            # 开窗去重：同 session 同策略状态未变 -> 薄响应，不重复灌包
            return {"snapshot_id": loaded_snapshot_id, "unchanged": True,
                    "profile": profile,
                    "note": "底层资源未变化；继续用已加载内容，不重发开窗包"}

    tz = ZoneInfo(config.RELATIONSHIP_TIMEZONE)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).astimezone(tz).date()
    three_days = [(today - timedelta(days=i)).isoformat() for i in range(BOOT_MEMORY_DAYS)]

    with db.formal() as conn:
        mem_rows = conn.execute(
            "SELECT memory_id, memory_date FROM memories WHERE visibility='active'"
            " AND memory_date IN (?,?,?) ORDER BY memory_date DESC, memory_id"
            " LIMIT ?", tuple(three_days) + (BOOT_SECTION_LIMIT,)).fetchall()
        memory_items = [_memory_slim(conn, r["memory_id"]) for r in mem_rows]
        mem_total = conn.execute(
            "SELECT COUNT(*) AS c FROM memories WHERE visibility='active'"
            " AND memory_date IN (?,?,?)", tuple(three_days)).fetchone()["c"]

    plan_items = plans.bootstrap_plans(today, BOOT_UPCOMING_DAYS)
    active_plans = [p for p in plan_items if p["state"] in plans.OPEN_STATES]
    upcoming_plans = [p for p in plan_items if p["state"] == "planned"]
    plan_page = plan_items[:BOOT_SECTION_LIMIT]
    plan_cursor = {"plans_offset": BOOT_SECTION_LIMIT} \
        if len(plan_items) > BOOT_SECTION_LIMIT else {"plans_offset": None}

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
        "time": {"local_date": today.isoformat(),
                 "timezone": config.RELATIONSHIP_TIMEZONE},
        "memory_days": {
            "dates": three_days, "mode": BOOT_DAY_WINDOW_MODE,
            "items": memory_items, "count": len(memory_items),
            "total_in_window": mem_total,
            "section_limit": BOOT_SECTION_LIMIT,
            "next_cursor": mem_cursor,
            "fields": ["original_title", "mood_tags", "mood_text",
                       "categories"],
            "note": "三天桶只出标题+心情；事件正文须明确打开该桶",
        },
        "i": _i_section(),
        "plans": {
            "items": plan_page, "count": len(plan_page),
            "total": len(plan_items),
            "active_count": len(active_plans),
            "upcoming_count": len(upcoming_plans),
            "upcoming_days": BOOT_UPCOMING_DAYS,
            "mode": "date_diff_0_to_3",
            "section_limit": BOOT_SECTION_LIMIT,
            "next_cursor": plan_cursor,
            "note": "进行中/需执行/逾期未完成单列不消失；plan 查看不续期",
        },
        "anniversaries": {
            "items": _anniversaries_upcoming(today, BOOT_UPCOMING_DAYS),
            "upcoming_days": BOOT_UPCOMING_DAYS,
        },
        "policy": {
            "boot_memory_days": BOOT_MEMORY_DAYS,
            "plan_upcoming_days": BOOT_UPCOMING_DAYS,
            "soft_token_budget": BOOT_SOFT_TOKEN_BUDGET,
            "raw_in_default_package": False,
        },
        "cursor": {"next": None},
        "coverage": {"raw": "not_in_default_package",
                     "note": "取原文用 raw.messages.list / raw.read 显式查询"},
    }

    _estimate_budget(result)
    with db.formal() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bootstrap_snapshots(snapshot_id, state_hash,"
            " profile, created_at) VALUES(?,?,?,datetime('now'))",
            (result["snapshot_id"], current_state, profile))
    return result


def next_page(principal_id: str, entry_source: str, snapshot_id: str,
              cursor: dict, section: str = "plans") -> dict:
    """续取开窗分页（memory_days / plans）。snapshot 变化即 SNAPSHOT_STALE。

    v2 起 raw 不再是开窗 section（V2-BOOT-03）：显式取源走 raw 工具。
    """
    if principal_id != "jiaming":
        raise Forbidden("bootstrap is for jiaming entries", principal=principal_id)
    if section not in ("memory_days", "plans"):
        raise Forbidden(f"unknown section: {section}（v2 开窗无 raw 段）")
    with db.formal() as conn:
        snap = conn.execute(
            "SELECT * FROM bootstrap_snapshots WHERE snapshot_id=?",
            (snapshot_id,)).fetchone()
        if snap is None:
            raise SnapshotStale("snapshot unknown; re-fetch bootstrap")
        if snap["state_hash"] != _state_hash(conn):
            raise SnapshotStale("underlying resources changed; re-fetch",
                                snapshot_id=snapshot_id)

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
            items = [_memory_slim(conn, r["memory_id"]) for r in rows]
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE visibility='active'"
                " AND memory_date IN (?,?,?)", tuple(three_days)).fetchone()["c"]
        nxt = None
        if len(items) >= BOOT_SECTION_LIMIT:  # 不足一页 = 到底
            last = rows[-1]
            nxt = {"memory_before_date": last["memory_date"],
                   "memory_last_id": last["memory_id"]}
        return {"snapshot_id": snapshot_id, "section": "memory_days",
                "items": items, "count": len(items),
                "total_in_window": total, "next_cursor": nxt}

    # plans：offset 游标
    offset = int((cursor or {}).get("plans_offset") or 0)
    all_plans = plans.bootstrap_plans(today, BOOT_UPCOMING_DAYS)
    page = all_plans[offset:offset + BOOT_SECTION_LIMIT]
    nxt = offset + BOOT_SECTION_LIMIT if offset + BOOT_SECTION_LIMIT < len(all_plans) else None
    return {"snapshot_id": snapshot_id, "section": "plans",
            "items": page, "count": len(page), "total": len(all_plans),
            "next_cursor": {"plans_offset": nxt}}


def _estimate_budget(result: dict) -> None:
    """粗估（chars/1.5）；超预算显式提示，不静默截断约定资料。"""
    chars = 0
    for m in result.get("memory_days", {}).get("items", []):
        chars += len(m.get("mood_text") or "")
        chars += len(m.get("original_title") or "")
    chars += len((result.get("i") or {}).get("content") or "")
    for p in result.get("plans", {}).get("items", []):
        chars += len(p.get("title") or "") + len(p.get("content") or "")
    est = int(chars / 1.5)
    result["estimated_tokens"] = est
    if est > BOOT_SOFT_TOKEN_BUDGET:
        result["warnings"] = [{
            "code": "SOFT_BUDGET_EXCEEDED",
            "message": f"估算 {est} tokens 超过软预算 {BOOT_SOFT_TOKEN_BUDGET}；"
                       "分节分页续取，不静默截断",
        }]
