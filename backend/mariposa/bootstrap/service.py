"""两入口开窗（spec_v2 R20 / §11 / V2-BOOT）。

v2 默认包 = 最近三个自然日桶的 标题+心情标签+心情文字 + 当前 I + 提前 0..3
自然日进入临近的计划/纪念日全文；进行中/逾期未完成计划单列不消失。
普通事件不自动全文展开；**不默认附带旧版 30 条原文**（显式取源走 raw
工具并另行确认）。开窗不产生 view confirm，不续期。

I 只注入当前 revision；若某条有历史，只带 has_history/history_count 指针，
旧 I 正文必须显式 i.item.history 才能读取。

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

BOOT_MEMORY_DAYS = 3
BOOT_DAY_WINDOW_MODE = "calendar_days"  # 今天+前两天（自然日），非最近72小时
BOOT_UPCOMING_DAYS = 3  # 临近按日期差 0..3 日（含边界；S4/D03）
BOOT_SOFT_TOKEN_BUDGET = 16000
BOOT_SECTION_LIMIT = 50  # 每段上限；超出走 cursor，不静默截断

_ENTRY_ALLOWED = {
    "claude_chat": {"claude_chat"},
    "cc": {"cc"},
}


BOOT_RULES_VERSION = "boot_rules_v2.1"


def _state_hash(conn) -> str:
    """开窗依据资源的状态指纹：任一变化使旧 snapshot 失效（§12.2）。

    v2.1 修正成分缺口：业务日期/时区、规则版本与分页常量、记忆表示
    版本、纪念日"定义"（改名不改 occurrences 行也要失效）。业务日期入
    指纹 = 跨自然日旧快照必然 SNAPSHOT_STALE，不会继续吐前一天窗口。
    """
    import hashlib
    from .. import biztime as ret_mod
    today = ret_mod.business_today().isoformat()
    parts = [
        f"rules:{BOOT_RULES_VERSION}:{BOOT_MEMORY_DAYS}:{BOOT_UPCOMING_DAYS}"
        f":{BOOT_SECTION_LIMIT}",
        f"tz:{config.RELATIONSHIP_TIMEZONE}",
        f"business_date:{today}",
    ]
    for table, time_col, extra in (
            ("memories", "updated_at", ", MAX(representation_state) AS e"),
            ("plans", "updated_at", ""),
            ("i_documents", "updated_at", ""),
            ("anniversary_definitions", "created_at", ""),
            ("anniversary_occurrences", "occurrence_date", ""),
            # CB-046（2026-10-02 审计 P2）：开窗实际展示的心情与分类
            # 进入指纹——COUNT+MAX(updated_at) 漏掉"改标签不改时间戳"
            # 的行级变化，mood.write/categories.replace 后旧快照仍
            # unchanged（审计反例）。mood_tags 无时间列，聚合 tag 集
            # 合本体（排序拼接防顺序漂移）
            ("memory_moods", "captured_at", ""),
            ("memory_categories", "created_at", "")):
        row = conn.execute(
            f"SELECT COUNT(*) AS c, MAX({time_col}) AS m {extra} FROM {table}"
        ).fetchone()
        part = f"{table}:{row['c']}:{row['m']}"
        if "e" in row.keys():
            part += f":{row['e']}"  # 记忆表示版本（表示变化必失效）
        parts.append(part)
    # CB-046：内容级成分——心情文本/标签集合与分类集合整体入指纹
    # （单项增删改即失效，不依赖时间戳是否前移）
    mood_rows = conn.execute(
        "SELECT memory_id, mood_text, author, evidence_state,"
        " captured_at FROM memory_moods ORDER BY memory_id"
    ).fetchall()
    mood_content = "|".join(
        f"{r['memory_id']}:{r['mood_text'] or ''}:{r['author']}:"
        f"{r['evidence_state']}" for r in mood_rows)
    tags = "|".join(sorted(r["memory_id"] + ":" + r["tag"] for r in
                           conn.execute(
                               "SELECT memory_id, tag FROM"
                               " memory_mood_tags")))
    cats = "|".join(sorted(r["memory_id"] + ":" + r["category"] for r in
                           conn.execute(
                               "SELECT memory_id, category FROM"
                               " memory_categories")))
    parts.append(f"mood_rows:{mood_content}")
    parts.append(f"mood_tags:{tags}")
    parts.append(f"categories:{cats}")
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
    from .. import biztime as ret_mod
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


def _anniversaries_upcoming(today, days: int, conn=None) -> list[dict]:
    horizon = (today + timedelta(days=days)).isoformat()
    if conn is not None:
        return _anniv_rows(conn, horizon, today, days)
    with db.formal() as c:
        return _anniv_rows(c, horizon, today, days)


def _anniv_rows(conn, horizon, today, days: int) -> list[dict]:
    if True:
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
            "items": doc.get("items", []),
            "history_policy": "旧 revision 默认不注入；has_history=true 时可按需调用 i.item.history",
            "source": "i_documents" if doc["content"] is not None else None,
            "note": None if doc["content"] is not None else
                    "I 尚未落笔（无旧Self自动映射，V2-I-03）"}


def _memory_section(conn, three_days: list[str]) -> dict:
    """三天桶段：标题+心情标签+心情文字+分类；不默认展开事件正文。"""
    mem_rows = conn.execute(
        "SELECT memory_id, memory_date FROM memories WHERE visibility='active'"
        " AND memory_date IN (?,?,?) ORDER BY memory_date DESC, memory_id"
        " LIMIT ?", tuple(three_days) + (BOOT_SECTION_LIMIT,)).fetchall()
    items = [_memory_slim(conn, r["memory_id"]) for r in mem_rows]
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE visibility='active'"
        " AND memory_date IN (?,?,?)", tuple(three_days)).fetchone()["c"]
    if total > len(items):
        last = mem_rows[-1]
        next_cursor = {"memory_before_date": last["memory_date"],
                       "memory_last_id": last["memory_id"],
                       "remaining": total - len(items)}
    else:
        next_cursor = None
    return {"items": items, "count": len(items), "total_in_window": total,
            "next_cursor": next_cursor}


def _three_day_window(tz) -> tuple:
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc).astimezone(tz).date()
    return today, [(today - timedelta(days=i)).isoformat()
                   for i in range(BOOT_MEMORY_DAYS)]


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

    # CB-044 延伸：state hash 与各段读取共享一个读事务——指纹与
    # 装配内容同快照，避免"hash 计算后又变化"的错配 unchanged
    with db.formal() as conn:
        conn.execute("BEGIN")
        try:
            current_state = _state_hash(conn)
            if loaded_snapshot_id:
                snap = conn.execute(
                    "SELECT state_hash FROM bootstrap_snapshots WHERE"
                    " snapshot_id=?",
                    (loaded_snapshot_id,)).fetchone()
                if snap is None:
                    raise SnapshotStale(
                        "snapshot unknown; re-fetch bootstrap")
                if snap["state_hash"] != current_state:
                    raise SnapshotStale(
                        "underlying resources changed since snapshot;"
                        " re-fetch", snapshot_id=loaded_snapshot_id)
                # 开窗去重：同 session 同策略状态未变 -> 薄响应，不重复灌包
                return {"snapshot_id": loaded_snapshot_id,
                        "unchanged": True, "profile": profile,
                        "note": "底层资源未变化；继续用已加载内容，"
                                "不重发开窗包"}

            tz = ZoneInfo(config.RELATIONSHIP_TIMEZONE)
            today, three_days = _three_day_window(tz)
            md = _memory_section(conn, three_days)
            # RA-006（2026-10-02 复审 P2）：I/Plan/纪念日与 state hash
            # 同一读事务装配——此前 COMMIT 在这些读取之前，包内容与其
            # 指纹可来自不同快照（审计反例 bootstrap_mixed_snapshot）
            from ..identity_i import service as _i_svc
            i_doc = _i_svc.get(conn=conn)
            plan_items = plans.bootstrap_plans(
                today, BOOT_UPCOMING_DAYS, conn=conn)
            anniv = _anniversaries_upcoming(today, BOOT_UPCOMING_DAYS,
                                            conn=conn)
        finally:
            conn.execute("COMMIT")

    active_plans = [p for p in plan_items if p["state"] in plans.OPEN_STATES]
    upcoming_plans = [p for p in plan_items if p["state"] == "planned"]
    plan_page = plan_items[:BOOT_SECTION_LIMIT]
    plan_cursor = {"plans_offset": BOOT_SECTION_LIMIT} \
        if len(plan_items) > BOOT_SECTION_LIMIT else {"plans_offset": None}

    result: dict = {
        "snapshot_id": f"snap_{uuid.uuid4().hex[:12]}",
        "state_hash": current_state,
        "profile": profile,
        "time": {"local_date": today.isoformat(),
                 "timezone": config.RELATIONSHIP_TIMEZONE},
        "memory_days": {
            "dates": three_days, "mode": BOOT_DAY_WINDOW_MODE,
            **md,
            "section_limit": BOOT_SECTION_LIMIT,
            "fields": ["original_title", "mood_tags", "mood_text",
                       "categories"],
            "note": "三天桶只出标题+心情；事件正文须明确打开该桶",
        },
        "i": {"content": i_doc["content"], "version": i_doc["version"],
              "items": i_doc.get("items", []),
              "history_policy": "旧 revision 默认不注入；has_history="
                               "true 时可按需调用 i.item.history",
              "source": ("i_documents"
                         if i_doc["content"] is not None else None),
              "note": None if i_doc["content"] is not None else
                      "I 尚未落笔（无旧Self自动映射，V2-I-03）"},
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
            "items": anniv,
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
                     "note": "取原文用 source.message.get / source.search 显式查询"},
    }

    _estimate_budget(result)
    with db.formal() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bootstrap_snapshots(snapshot_id, state_hash,"
            " profile, created_at, business_date) VALUES(?,?,?,datetime('now'),?)",
            (result["snapshot_id"], current_state, profile,
             result["time"]["local_date"]))
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
    snap_day = snap["business_date"] if "business_date" in snap.keys() else None
    if snap_day and snap_day != today.isoformat():
        # BOOT-05/07：跨业务日不得续页——旧快照的窗口是前一天的三个自然日
        raise SnapshotStale(
            "crossed business day; re-fetch bootstrap for the new window",
            snapshot_id=snapshot_id, snapshot_day=snap_day,
            current_day=today.isoformat())
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
