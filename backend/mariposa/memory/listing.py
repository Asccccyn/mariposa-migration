"""记忆列表/结构化检索 + meaning 层（§10.5/§17.3）。

meaning 只允许周家明写；追加层次，替换留底；纳入 full 投影；
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

def meanings_append(principal_id: str, memory_id: str, content: str) -> dict:
    """只允许周家明写；追加层（不覆盖）；重建 full 投影纳入新层。"""
    if principal_id != "jiaming":
        raise Forbidden("only jiaming writes meanings", principal=principal_id)
    if not content or not str(content).strip():
        raise Forbidden("meaning content required")
    with db.formal() as conn:
        m, v = _rep(conn, memory_id)
        if m["compression_state"] == "forgotten_summary":
            # 遗忘桶可以追加 meaning，但其内容不进默认文本检索（§10.5）
            rebuild_projection = False
        else:
            rebuild_projection = True
        next_layer = (conn.execute(
            "SELECT COALESCE(MAX(layer_no),0)+1 AS n FROM memory_meanings"
            " WHERE memory_id=?", (memory_id,)).fetchone()["n"])
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO memory_meanings(memory_id, layer_no, content,"
                " written_by, created_at) VALUES(?,?,?,?,?)",
                (memory_id, next_layer, str(content), principal_id, _now()))
            if rebuild_projection:
                _rebuild_full_projection(conn, memory_id)
            audit.record(conn, "memory.meaning.appended", principal_id,
                         resource_id=memory_id,
                         payload={"layer_no": next_layer})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "layer_no": next_layer}


def meanings_replace(principal_id: str, memory_id: str,
                     new_layers: list[str]) -> dict:
    """替换要保存旧层：旧层移入 archive 层号段（>=1000），新层从 1 重排。"""
    if principal_id != "jiaming":
        raise Forbidden("only jiaming replaces meanings")
    if not isinstance(new_layers, list) or not new_layers:
        raise Forbidden("new_layers must be a non-empty list")
    with db.formal() as conn:
        m, v = _rep(conn, memory_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            olds = conn.execute(
                "SELECT layer_no, content, written_by, created_at FROM"
                " memory_meanings WHERE memory_id=? AND layer_no<1000"
                " ORDER BY layer_no", (memory_id,)).fetchall()
            archive_base = 1000 + (conn.execute(
                "SELECT COALESCE(MAX(layer_no),0) AS m FROM memory_meanings"
                " WHERE memory_id=?", (memory_id,)).fetchone()["m"])
            for i, old in enumerate(olds):
                conn.execute(
                    "INSERT INTO memory_meanings(memory_id, layer_no, content,"
                    " written_by, created_at) VALUES(?,?,?,?,?)",
                    (memory_id, archive_base + i, old["content"],
                     old["written_by"], old["created_at"]))
                conn.execute(
                    "DELETE FROM memory_meanings WHERE memory_id=? AND"
                    " layer_no=?", (memory_id, old["layer_no"]))
            for i, text in enumerate(new_layers, start=1):
                conn.execute(
                    "INSERT INTO memory_meanings(memory_id, layer_no, content,"
                    " written_by, created_at) VALUES(?,?,?,?,?)",
                    (memory_id, i, str(text), principal_id, _now()))
            if m["compression_state"] == "full":
                _rebuild_full_projection(conn, memory_id)
            audit.record(conn, "memory.meaning.replaced", principal_id,
                         resource_id=memory_id,
                         payload={"archived": len(olds),
                                  "new_layers": len(new_layers)})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "new_layers": len(new_layers),
            "archived_old": len(olds)}


def meanings_list(memory_id: str) -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT layer_no, content, written_by, created_at FROM"
            " memory_meanings WHERE memory_id=? AND layer_no<1000"
            " ORDER BY layer_no", (memory_id,)).fetchall()
    return [dict(r) for r in rows]


def _rebuild_full_projection(conn, memory_id: str) -> None:
    """full 投影 = hold_text + why_remember + 当前有效 meaning 各层。"""
    m = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                     (memory_id,)).fetchone()
    v = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id=? AND version_no=?",
        (memory_id, m["current_version_no"])).fetchone()
    layers = [r["content"] for r in conn.execute(
        "SELECT content FROM memory_meanings WHERE memory_id=? AND layer_no<1000"
        " ORDER BY layer_no", (memory_id,))]
    search_text = projection.build_full(
        "\n".join([v["hold_text"] or ""] + layers), v["why_remember"])
    projection.upsert(conn, memory_id, m["current_version_no"], "full",
                      search_text,
                      whitelist_body=projection.normalize_search_text(
                          v["hold_text"] or ""))
