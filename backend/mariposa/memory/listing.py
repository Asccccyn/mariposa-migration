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


def by_date(date_str: str) -> dict:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT memory_id FROM memories WHERE memory_date=? AND"
            " visibility='active' ORDER BY created_at", (date_str,)).fetchall()
        items = [memory.get(conn, r["memory_id"]) for r in rows]
    return {"date": date_str, "items": items, "matched_by": "date"}


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
        if not conn.execute(
                "SELECT 1 FROM memories WHERE memory_id=?",
                (memory_id,)).fetchone():
            raise NotFound("memory not found", memory_id=memory_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
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


def by_emotion(tag: str, whose: str | None = None) -> dict:
    """RA-010：按心情标签查（现行 memory_mood_tags 表）。"""
    q = ("SELECT t.memory_id, m.compression_state,"
         " m.current_version_no FROM memory_mood_tags t"
         " JOIN memories m ON m.memory_id=t.memory_id"
         " WHERE t.tag=? AND m.visibility='active'")
    params: list = [tag]
    if whose:
        if whose not in ("jiaming", "qiaosheng"):
            raise Forbidden("whose must be jiaming or qiaosheng")
        q += " AND t.whose IS NOT NULL AND t.whose != ''"
    with db.formal() as conn:
        rows = conn.execute(q, params).fetchall()
    return {"tag": tag, "items": [dict(r) for r in rows],
            "matched_by": "mood_tag"}


# ---------------- meaning ----------------





def _rebuild_full_projection(conn, memory_id: str) -> None:
    """full 投影重建统一入口的薄委托（审计 F04：正文来源不再由本层决定）。"""
    memory.rebuild_full_projection(conn, memory_id)
