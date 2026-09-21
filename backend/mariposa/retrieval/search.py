"""memory.search：权限过滤 -> 有效投影 -> FTS -> 结果表示（§8.2）。

语义 provider 未配置时关键词照常工作，返回 degraded: semantic_unavailable；
不用随机向量或 mock 相似度冒充语义搜索。
"""
from __future__ import annotations

from .. import config
from . import projection


def search(conn, query: str, limit: int = 20) -> dict:
    phrase = projection.compile_query(query)
    hits: list[dict] = []
    if phrase:
        rows = conn.execute(
            "SELECT f.memory_id, rd.projection_kind, rd.memory_version_no,"
            " m.compression_state, m.visibility, bm25(search_fts) AS rank"
            " FROM search_fts f"
            " JOIN retrieval_documents rd ON rd.memory_id = f.memory_id"
            " JOIN memories m ON m.memory_id = f.memory_id"
            " WHERE search_fts MATCH ? AND m.visibility='active'"
            " ORDER BY rank LIMIT ?",
            (phrase, limit),
        ).fetchall()
        for r in rows:
            hits.append(
                {
                    "memory_id": r["memory_id"],
                    "matched_by": (
                        "summary_keyword"
                        if r["compression_state"] == "forgotten_summary"
                        else "keyword"
                    ),
                    "projection_kind": r["projection_kind"],
                    "memory_version": r["memory_version_no"],
                }
            )
    result: dict = {"hits": hits, "query": query}
    if config.SEMANTIC_PROVIDER:
        result["semantic"] = "unimplemented"
    else:
        result["semantic"] = "unavailable"
        result["degraded"] = "semantic_unavailable"
    return result
