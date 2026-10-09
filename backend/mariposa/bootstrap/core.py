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
#: RRA-006（2026-10-09 复审）：estomago 桶事件/话语正文分节宽度（码点）
BOOT_MEMITEM_SECTION_CHARS = BOOT_I_SECTION_CHARS
#: RRA-006（二次回访 2026-10-10）：分节**字节**预算——码点宽度只是
#: 初值，控制字符 JSON 转义 6B/字符时按码点分节可把首包顶穿 24576
#:（首桶无条件纳入装页）。分节宽度按"该片序列化字节 ≤ 本预算"折半；
#: 续取游标按已交付码点偏移，链路确定性不变
BOOT_MEMITEM_SECTION_BYTES = 4000

_ENTRY_ALLOWED = {
    "claude_chat": {"claude_chat"},
    "cc": {"cc"},
    # D2 裁定（她 2026-10-06 批准口径 A）：estómago 独立 entry/profile——
    # 不冒充 cc/claude_chat；默认不自动送 mood_text（字段矩阵见 service.get）。
    # estomago_builtin=内置绑定（发币脚本签发的 hold/bootstrap/recall 服务
    # 身份）也是合法 entry——宿主装配层（B 点）与模型工具共用该绑定
    "estomago": {"estomago", "estomago_builtin"},
}


BOOT_RULES_VERSION = "boot_rules_v2.1"

#: RRA-006：memory_days 段单页字节预算（UTF-8；给 plans/i/纪念日等
#: 其余段与信封骨架留约 12KB——整包 ≤24576）
_MEMORY_DAY_PAGE_BUDGET_BYTES = 12000


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
            ("i_revision_memory_relations", "created_at", ""),
            # RRA-005（2026-10-09 复审）：D-6 开窗输出新增依赖 our_words
            # 与关系边——两者写入不触 memories.updated_at，COUNT/MAX
            # 指纹完全不感知（追加话语/建关系后旧 snapshot 仍 unchanged）
            ("memory_our_words", "created_at", ""),
            ("memory_relations", "created_at", "")):
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


def _esc_bytes(s: str) -> int:
    """字符串 JSON 序列化后的 UTF-8 字节数（分节字节口径的度量单位）。"""
    import json as _json
    return len(_json.dumps(s, ensure_ascii=False).encode("utf-8"))


def _byte_width(full: str, *, start: int = 0) -> int:
    """从 start 起的字节有界分节宽度（码点）：初值=节宽上限，按
    `full[start:start+w]` 的序列化字节折半至 ≤BOOT_MEMITEM_SECTION_BYTES；
    最少 1 字符（预算内必装得下，续取链不活锁）。"""
    w = BOOT_MEMITEM_SECTION_CHARS
    while w > 1 and _esc_bytes(full[start:start + w]) \
            > BOOT_MEMITEM_SECTION_BYTES:
        w //= 2
    return max(w, 1)


def _section_body(item: dict, memory_id: str, field: str,
                  full: str) -> None:
    """RRA-006：estomago 桶正文载体分节——首节入 item，超宽给
    truncated/total_chars/next_cursor（bootstrap.next section=
    memory_item 续取；短文整带）。二次回访（2026-10-10）：宽度按
    **序列化字节**折半（码点定宽在控制字符下击穿整包预算）。"""
    width = _byte_width(full)
    if len(full) <= width:
        item[field] = full
        return
    item[field] = full[:width]
    item[f"{field}_truncated"] = True
    item[f"{field}_total_chars"] = len(full)
    item[f"{field}_next"] = {"memory_id": memory_id, "field": field,
                             "offset": width}


def _section_words(rows, start_item: int = 0, start_char: int = 0):
    """RRA-006（回访+二次回访）：our_words 装页——条目数组形状保持，
    条目间断页；**单条超宽按字节折半分片**、条目累计也按字节（码点
    定宽在控制字符下击穿整包预算）。返回 (kept, next_item,
    next_char)：next_item=None=全部装完；否则续取游标位（next_char=
    条内码点偏移）。"""
    kept: list[dict] = []
    used = 0
    i = start_item
    char_off = start_char
    # 越过已耗尽的条目起点（char_off 抵达条尾 → 下一条）
    while i < len(rows) and char_off >= len(rows[i]["text"] or ""):
        i += 1
        char_off = 0
    while i < len(rows):
        text = rows[i]["text"] or ""
        remain = text[char_off:]
        w = _byte_width(remain)
        if kept and used + _esc_bytes(remain[:w]) \
                > BOOT_MEMITEM_SECTION_BYTES:
            return kept, i, char_off
        if len(remain) > w:
            kept.append({"speaker": rows[i]["speaker"],
                         "text": remain[:w],
                         "text_truncated": True,
                         "text_total_chars": len(text)})
            return kept, i, char_off + w
        if kept and used + _esc_bytes(remain) \
                > BOOT_MEMITEM_SECTION_BYTES:
            return kept, i, char_off
        kept.append({"speaker": rows[i]["speaker"], "text": remain})
        used += _esc_bytes(remain)
        i += 1
        char_off = 0
    return kept, None, 0


def _pack_memory_rows(conn, rows, profile: str = "claude_chat"):
    """RRA-006（回访 2026-10-09）：memory_days 页按**实际序列化字节**
    装桶——bootstrap.get 首页与 bootstrap.next 续页同一口径（此前续页
    按条数装页无预算，5 桶 31250B 击穿 24576）；装不下的桶顺延下页
    不 pop。返回 (items, overflow_row)：overflow=None=本批全装下。"""
    import json as _json
    items: list[dict] = []
    served = 0
    overflow = None
    for r in rows:
        it = _memory_slim(conn, r["memory_id"], profile=profile)
        b = len(_json.dumps(it, ensure_ascii=False).encode("utf-8"))
        if items and served + b > _MEMORY_DAY_PAGE_BUDGET_BYTES:
            overflow = r
            break
        items.append(it)
        served += b
    return items, overflow


def _memory_slim(conn, memory_id: str,
                  profile: str = "claude_chat") -> dict:
    from ..memory import categories as cats_mod
    """三天桶条目：标题+心情标签+心情文字+分类；estomago profile 按
    D-6（2026-10-09）另附事件正文/我们的话/关系注释摘要（其余 profile
    不展开事件正文——D2/2026-10-06 的最小化语义保持）。"""
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
    # WP-06 6D-2（D-6，她 2026-10-09 裁定）：estomago 开窗近三日桶携带
    # 时间/心情标签+心情文字/事件/我们的话；该桶存在 relation 时附关系
    # 注释（计数+类型标签去重，不带目标桶明细——明细仍走 relations.list，
    # 维持 I 开窗最小化 §8 的"按需显式读取"边界）；无边省略该键
    if mood is not None:
        item["mood_tags"] = [t["tag"] for t in tags]
        item["mood_text"] = mood["mood_text"]
    else:
        item["mood_tags"] = []
        item["mood_text"] = None  # 心情空白 ≠ 不重要（R05）
    if profile == "estomago":
        # RRA-006：事件/话语正文按码点分节（首节+truncated+续取
        # cursor——完整内容经 bootstrap.next section=memory_item 续取，
        # 不静默裁剪；短文整带无 cursor）
        vv = conn.execute(
            "SELECT event_text, hold_text FROM memory_versions"
            " WHERE memory_id=? AND version_no=?",
            (memory_id, m["current_version_no"])).fetchone()
        if vv is not None:
            _body = vv["event_text"] or vv["hold_text"]
            if _body:
                _section_body(item, memory_id, "event_text", _body)
        _words = conn.execute(
            "SELECT w.speaker, w.text FROM memory_our_words w"
            " JOIN memories mm ON mm.memory_id=w.memory_id"
            " WHERE w.memory_id=? AND mm.visibility='active'",
            (memory_id,)).fetchall()
        if _words:
            # RRA-006（回访 2026-10-09）：话语按**条目**分节（保持数组
            # 形状——消费端 w.speaker/w.text 不变）；单条超宽按码点分节
            # （续取 section=memory_item 返回剩余条目/条内剩余字符）
            _kept, _nxt_i, _nxt_c = _section_words(_words)
            item["our_words"] = _kept
            if _nxt_i is not None:
                item["our_words_truncated"] = True
                item["our_words_total_items"] = len(_words)
                item["our_words_next"] = {
                    "memory_id": memory_id, "field": "our_words",
                    "item_offset": _nxt_i, "char_offset": _nxt_c}
        _edges = conn.execute(
            "SELECT relation_type FROM memory_relations"
            " WHERE from_memory=? OR to_memory=?",
            (memory_id, memory_id)).fetchall()
        if _edges:
            item["relations"] = {
                "count": len(_edges),
                "types": sorted({e["relation_type"] for e in _edges}),
                "note": "存在关联记忆（明细走 relations.list 按需读取）",
            }
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
