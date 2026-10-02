"""检索索引重建（§8.5：重建不得让遗忘桶旧词复活）。"""
from __future__ import annotations

from .. import audit, db
from ..retrieval import projection
from . import search as retrieval_search


def rebuild_index(actor: str = "system") -> dict:
    """按当前版本重建全部有效投影与 FTS。

    不变式：forgotten 桶只从 compressed_summary 生成投影；
    hidden 无投影。重建后旧正文词不可搜（有测试钉住）。
    审计 F04：full 投影经 memory.rebuild_full_projection 统一构造
    （event_text 优先，v1 旧数据回退 hold_text）——v2 正文不再在
    全库重建时从检索消失。
    审计 F13：返回对象在所有步骤前定义；部分提交后不再引用未定义名。
    """
    from ..memory import service as memory_service
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
                        projection.build_forgotten(v["compressed_summary"] or ""),
                        whitelist_body=projection.normalize_search_text(
                            v["compressed_summary"] or ""))
                    rebuilt["forgotten_summary"] += 1
                else:
                    memory_service.rebuild_full_projection(
                        conn, r["memory_id"])
                    rebuilt["full"] += 1
            audit.record(conn, "retrieval.index.rebuilt", actor,
                         payload=rebuilt)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    out = {"rebuilt": rebuilt}
    # v1.7 F5：派生索引全家统一重建入口（分字段投影 + words 索引）
    from . import field_projection as _fp
    from . import words as _words
    from .. import db as _db
    field_stat = _fp.rebuild_all()
    with _db.formal() as conn:
        words_n = _words.rebuild_words_index(conn)
    out["field_projection"] = field_stat
    out["words_index"] = {"rebuilt": words_n}
    # v1.7：source 检索投影一并重建（published 正文）
    from ..source import binding as src_binding
    out["source_projection"] = src_binding.reindex_search_docs()
    return out
