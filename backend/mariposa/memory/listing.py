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
                  cursor_date: str | None = None) -> dict:
    """倒序列表；正文按当前表示（遗忘桶只给摘要）。"""
    q = ("SELECT memory_id FROM memories WHERE visibility='active'"
         + (" AND compression_state=? " if state else " ")
         + "AND memory_date IS NOT NULL AND (? IS NULL OR memory_date < ?)"
           " ORDER BY memory_date DESC LIMIT ?")
    with db.formal() as conn:
        rows = conn.execute(q, tuple(
            ([state] if state else []) +
            [cursor_date, cursor_date, limit])).fetchall()
        items = [memory.get(conn, r["memory_id"]) for r in rows]
    return {"items": items, "count": len(items)}


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


# ---------------- meaning ----------------





def _rebuild_full_projection(conn, memory_id: str) -> None:
    """full 投影重建统一入口的薄委托（审计 F04：正文来源不再由本层决定）。"""
    memory.rebuild_full_projection(conn, memory_id)
