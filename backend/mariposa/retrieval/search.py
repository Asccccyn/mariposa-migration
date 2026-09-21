"""memory.search：权限过滤 -> 有效投影 -> FTS -> 结果表示（§8.2）。

语义 provider 未配置时关键词照常工作，返回 degraded: semantic_unavailable；
不用随机向量或 mock 相似度冒充语义搜索。
"""
from __future__ import annotations

from .. import config
from ..memory import relations as relations_mod
from . import projection, semantic


def search(conn, query: str, limit: int = 20,
           related_of: str | None = None) -> dict:
    """related_of：按关联找到的桶（matched_by=relation），不依赖文本匹配。"""
    hits: list[dict] = []
    phrase = projection.compile_query(query)
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
    if related_of:
        for mid in relations_mod.related_ids(conn, related_of):
            state = conn.execute(
                "SELECT compression_state, visibility, current_version_no"
                " FROM memories WHERE memory_id=?", (mid,)).fetchone()
            if state is None or state["visibility"] != "active":
                continue
            hits.append({
                "memory_id": mid,
                "matched_by": "relation",
                "projection_kind": state["compression_state"],
                "memory_version": state["current_version_no"],
            })
    # 语义路径（§8.3：仅有效投影向量参与；provider 未配置显式 degraded）
    mode = "keyword"
    if config.SEMANTIC_PROVIDER == "local_bge_zh":
        sem = semantic.semantic_search(conn, query, limit)
        mode = "hybrid"
    else:
        sem = []
    # 语义命中去重（关键词已命中的桶保留 keyword 标注优先）
    seen = {h["memory_id"] for h in hits}
    for sh in sem:
        if sh["memory_id"] not in seen:
            hits.append(sh)

    result: dict = {"hits": hits, "query": query, "mode": mode}
    if config.SEMANTIC_PROVIDER == "local_bge_zh":
        result["semantic"] = "local_bge_zh"
    else:
        result["semantic"] = "unavailable"
        result["degraded"] = "semantic_unavailable"
    return result
