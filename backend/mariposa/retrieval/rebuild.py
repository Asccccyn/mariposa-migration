"""检索索引重建（§8.5：重建不得让遗忘桶旧词复活）。"""
from __future__ import annotations

from .. import audit, db
from ..retrieval import projection
from . import search as retrieval_search


def rebuild_index(actor: str = "system") -> dict:
    """按当前版本重建全部有效投影与 FTS。

    不变式：forgotten 桶只从 compressed_summary 生成投影；
    hidden/archived 无投影。重建后旧正文词不可搜（有测试钉住）。
    """
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT memory_id, current_version_no, compression_state,"
            " visibility FROM memories").fetchall()
        rebuilt = {"full": 0, "forgotten_summary": 0, "removed": 0}
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM search_fts")
            conn.execute("DELETE FROM retrieval_documents")
            for r in rows:
                if r["visibility"] != "active":
                    rebuilt["removed"] += 1
                    continue
                v = conn.execute(
                    "SELECT * FROM memory_versions WHERE memory_id=? AND"
                    " version_no=?", (r["memory_id"], r["current_version_no"])
                ).fetchone()
                if r["compression_state"] == "forgotten_summary":
                    projection.upsert(
                        conn, r["memory_id"], r["current_version_no"],
                        "forgotten_summary",
                        projection.build_forgotten(v["compressed_summary"] or ""))
                    rebuilt["forgotten_summary"] += 1
                else:
                    layers = [x["content"] for x in conn.execute(
                        "SELECT content FROM memory_meanings WHERE memory_id=?"
                        " AND layer_no<1000 ORDER BY layer_no", (r["memory_id"],))]
                    projection.upsert(
                        conn, r["memory_id"], r["current_version_no"], "full",
                        projection.build_full(
                            "\n".join([v["hold_text"] or ""] + layers),
                            v["why_remember"]))
                    rebuilt["full"] += 1
            audit.record(conn, "retrieval.index.rebuilt", actor,
                         payload=rebuilt)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"rebuilt": rebuilt}
