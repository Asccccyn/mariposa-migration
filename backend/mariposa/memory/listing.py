"""记忆列表/结构化检索 + meaning 层（§10.5/§17.3）。

meaning 只允许周家明写；追加层次，替换留底；禁检来源（不进任何检索投影）；
遗忘桶的 meaning 旧内容不作为默认文本检索依据（§10.5）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound
from ..retrieval import projection
from . import service as memory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rep(conn, memory_id: str) -> dict:
    m = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                     (memory_id,)).fetchone()
    if m is None:
        raise NotFound("memory not found", memory_id=memory_id)
    v = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id=? AND version_no=?",
        (memory_id, m["current_version_no"])).fetchone()
    return m, v


def list_memories(state: str | None = None, limit: int = 50,
                  cursor_date: str | None = None,
                  cursor_id: str | None = None) -> dict:
    """倒序列表；正文按当前表示（遗忘桶只给摘要）。

    CB-049（2026-10-02 审计 P2）：(memory_date, memory_id) 复合
    keyset——同日多桶可完整续页（此前纯日期排他过滤，同日超出页大小
    的剩余桶从该游标永久不可达）。无日期桶不在此列表（口径显式，
    走 by_tag/检索）。
    """
    q = ("SELECT memory_id, memory_date FROM memories WHERE"
         " visibility='active'"
         + (" AND compression_state=? " if state else " ")
         + "AND memory_date IS NOT NULL AND (? IS NULL OR"
           " (memory_date < ? OR (memory_date = ? AND memory_id < ?)))"
           " ORDER BY memory_date DESC, memory_id DESC LIMIT ?")
    with db.formal() as conn:
        rows = conn.execute(q, tuple(
            ([state] if state else []) +
            [cursor_date, cursor_date, cursor_date, cursor_id, limit])
        ).fetchall()
        items = [memory.get(conn, r["memory_id"]) for r in rows]
    next_cursor = None
    if len(rows) >= limit and rows:
        last = rows[-1]
        next_cursor = {"memory_date": last["memory_date"],
                       "memory_id": last["memory_id"]}
    return {"items": items, "count": len(items),
            "next_cursor": next_cursor}


def by_date(date_str: str, limit: int = 200) -> dict:
    """按事件发生日期直达（memory_date=真实发生日期，非写入日期）。

    2026-10-05 江乔生裁定：直达入口标题优先——先给标题卡片清单
    （正文按需经 memory.get 取），单日默认全量、上限 200 条结构化
    截断（has_more 如实标注，不冒充完整）。
    """
    with db.formal() as conn:
        cards = _title_cards(
            conn, "m.memory_date=?", [date_str],
            order=" ORDER BY m.memory_date, m.memory_id", limit=limit)
    return {"date": date_str, "items": cards["items"],
            "has_more": cards["has_more"], "matched_by": "date"}


#: 直达卡片固定取数列（标题优先：标题 + 日期 + 分类 + 心情标签）
_CARD_COLS = ("m.memory_id, m.memory_date, m.compression_state,"
              " v.original_title, v.event_text, v.hold_text")


def _title_cards(conn, where: str, params: list, *, order: str,
                 limit: int) -> dict:
    """标题卡片清单（共同实现）：缺标题的 v1 旧行以正文前 12 字代替。"""
    rows = conn.execute(
        f"SELECT {_CARD_COLS} FROM memories m"
        " LEFT JOIN memory_versions v ON v.memory_id=m.memory_id"
        " AND v.version_no=m.current_version_no"
        f" WHERE m.visibility='active' AND ({where}){order}"
        " LIMIT ?", (*params, limit + 1)).fetchall()
    has_more = len(rows) > limit
    items = []
    for r in rows[:limit]:
        cats = [c["category"] for c in conn.execute(
            "SELECT category FROM memory_categories WHERE memory_id=?"
            " ORDER BY category", (r["memory_id"],))]
        moods = [t["tag"] for t in conn.execute(
            "SELECT tag FROM memory_mood_tags WHERE memory_id=?"
            " ORDER BY tag", (r["memory_id"],))]
        title = r["original_title"] or (
            (r["event_text"] or r["hold_text"] or "")[:12] or "(无标题)")
        items.append({"memory_id": r["memory_id"],
                      "original_title": title,
                      "memory_date": r["memory_date"],
                      "categories": cats, "mood_tags": moods,
                      "forgotten": r["compression_state"]
                      == "forgotten_summary"})
    return {"items": items, "has_more": has_more}


def by_category(category: str, limit: int = 50,
                cursor_date: str | None = None,
                cursor_id: str | None = None) -> dict:
    """按分类直达（2026-10-05 江乔生裁定）：标题优先 + (日期,id)
    keyset 续页——周家明/网页想看"日常的全部"一条直达，不进召回。"""
    from . import categories as categories_mod
    cats = categories_mod.validate([category])
    where = ("m.memory_id IN (SELECT memory_id FROM memory_categories"
             " WHERE category=?)")
    params: list = [cats[0]]
    order = " ORDER BY m.memory_date DESC, m.memory_id DESC"
    if cursor_date and cursor_id:
        # keyset：同 (date,id) 复合游标，同日多桶不丢（CB-049 同口径）
        where += (" AND (m.memory_date < ? OR (m.memory_date = ?"
                  " AND m.memory_id < ?))")
        params += [cursor_date, cursor_date, cursor_id]
    with db.formal() as conn:
        cards = _title_cards(conn, where, params, order=order, limit=limit)
    out = {"category": cats[0], "items": cards["items"],
           "has_more": cards["has_more"]}
    if cards["items"]:
        last = cards["items"][-1]
        out["next_cursor"] = {"memory_date": last["memory_date"],
                              "memory_id": last["memory_id"]}
    return out


def by_tag(namespace: str, tag: str, whose: str | None = None) -> dict:
    q = ("SELECT t.memory_id, t.whose FROM memory_tags t JOIN memories m"
         " ON m.memory_id = t.memory_id WHERE t.namespace=? AND t.tag=?"
         " AND m.visibility='active'")
    params: list = [namespace, tag]
    if whose:
        if whose not in ("jiaming", "qiaosheng"):
            raise Forbidden("whose must be jiaming or qiaosheng")
        q += " AND t.whose=?"
        params.append(whose)
    with db.formal() as conn:
        rows = conn.execute(q, params).fetchall()
        items = [dict(memory.get(conn, r["memory_id"]),
                      tag_whose=r["whose"]) for r in rows]
    return {"namespace": namespace, "tag": tag, "items": items,
            "matched_by": "tag"}


def tags_add(principal_id: str, memory_id: str, tags: list[str]) -> dict:
    """RA-010（2026-10-02 复审 P2）：现行 tags 写入——此前 handler 引用
    不存在的 content 模块（NameError）。namespace 固定 free/whose 按
    主体；幂等（OR IGNORE）。"""
    if not tags:
        raise Forbidden("tags must be a non-empty list")
    whose = "jiaming" if principal_id == "jiaming" else "qiaosheng"
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # P1-4：存在性检查在写锁内
            if not conn.execute(
                    "SELECT 1 FROM memories WHERE memory_id=?",
                    (memory_id,)).fetchone():
                raise NotFound("memory not found", memory_id=memory_id)
            for t in tags:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_tags(memory_id,"
                    " namespace, tag, whose, confidence, created_by)"
                    " VALUES(?,?,?,?,?,?)",
                    (memory_id, "free", str(t), whose, "human",
                     principal_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "added": len(tags)}


def by_emotion(tag: str, whose: str | None = None, limit: int = 50,
               cursor_date: str | None = None,
               cursor_id: str | None = None) -> dict:
    """按心情子分类直达（2026-10-05 裁定）：tag 为空=全量（不筛
    心情的全部有效桶）；给了=该子分类下。标题优先 + keyset 续页。

    RA-010 原语义（tag 必填按标签查）保留为给 tag 的路径。"""
    if whose and whose not in ("jiaming", "qiaosheng"):
        raise Forbidden("whose must be jiaming or qiaosheng")
    if tag:
        where = ("m.memory_id IN (SELECT t.memory_id FROM memory_mood_tags"
                 " t WHERE t.tag=?"
                 + (" AND EXISTS(SELECT 1 FROM memory_moods mm WHERE"
                    " mm.memory_id=t.memory_id AND mm.author=?)" if whose
                    else "") + ")")
        params: list = [tag] + ([whose] if whose else [])
    else:
        where = "1=1"
        params = []
    order = " ORDER BY m.memory_date DESC, m.memory_id DESC"
    if cursor_date and cursor_id:
        where += (" AND (m.memory_date < ? OR (m.memory_date = ?"
                  " AND m.memory_id < ?))")
        params += [cursor_date, cursor_date, cursor_id]
    with db.formal() as conn:
        cards = _title_cards(conn, where, params, order=order, limit=limit)
    out = {"mood_tag": tag or None, "items": cards["items"],
           "has_more": cards["has_more"]}
    if cards["items"]:
        last = cards["items"][-1]
        out["next_cursor"] = {"memory_date": last["memory_date"],
                              "memory_id": last["memory_id"]}
    return out


# ---------------- meaning ----------------





def _rebuild_full_projection(conn, memory_id: str) -> None:
    """full 投影重建统一入口的薄委托（审计 F04：正文来源不再由本层决定）。"""
    memory.rebuild_full_projection(conn, memory_id)
