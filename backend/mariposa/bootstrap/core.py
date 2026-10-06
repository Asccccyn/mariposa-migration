"""Bootstrap 共享核心（2026-10-04 依赖边界批）。

常量、状态指纹、三日窗、瘦身行——供 service 与 pages 单向引用，
打破 pages→service 的静态环（独立 import pages 曾失败）。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import biztime as ret_mod
from .. import config
from .. import db


BOOT_MEMORY_DAYS = 3
BOOT_DAY_WINDOW_MODE = "calendar_days"  # 今天+前两天（自然日），非最近72小时
BOOT_UPCOMING_DAYS = 3  # 临近按日期差 0..3 日（含边界；S4/D03）
BOOT_SOFT_TOKEN_BUDGET = 16000
BOOT_SECTION_LIMIT = 50  # 每段上限；超出走 cursor，不静默截断
# F16（2026-10-03 审计 P2）：默认包内 I 正文分节长度——单条长 I
# 全文返回会击穿软预算且承诺的分节续取并不存在
BOOT_I_SECTION_CHARS = 2000
# 裁定（2026-10-04）：Plan 允许长正文、存在真实预算风险——与 I 同款
# 分节/续取（mood 不是长内容载体，不做）
BOOT_PLAN_SECTION_CHARS = BOOT_I_SECTION_CHARS

_ENTRY_ALLOWED = {
    "claude_chat": {"claude_chat"},
    "cc": {"cc"},
    # D2 裁定（她 2026-10-06 批准口径 A）：estómago 独立 entry/profile——
    # 不冒充 cc/claude_chat；默认不自动送 mood_text（字段矩阵见 service.get）
    "estomago": {"estomago"},
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
            ("memory_categories", "created_at", ""),
            ("i_revision_memory_relations", "created_at", "")):
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


def _three_day_window(tz) -> tuple:
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc).astimezone(tz).date()
    return today, [(today - timedelta(days=i)).isoformat()
                   for i in range(BOOT_MEMORY_DAYS)]


def _memory_slim(conn, memory_id: str,
                  profile: str = "claude_chat") -> dict:
    from ..memory import categories as cats_mod
    """三天桶条目：标题+心情标签+心情文字+分类；不默认展开事件正文。

    D2 裁定（2026-10-06）：estomago profile 默认**不带**心情自由文字
    （mood_text）——标签/标题/分类照给；cc/claude_chat 行为不变。"""
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
        if profile != "estomago":
            item["mood_text"] = mood["mood_text"]
    else:
        item["mood_tags"] = []
        if profile != "estomago":
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
