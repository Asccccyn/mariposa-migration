"""memory.search：权限过滤 -> 有效投影 -> FTS -> 结果表示（§8.2）。

语义 provider 未配置时关键词照常工作，返回 degraded: semantic_unavailable；
不用随机向量或 mock 相似度冒充语义搜索。
"""
from __future__ import annotations

from .. import config
from ..memory import relations as relations_mod
from . import projection, semantic


def recall(conn, query: str = "", filters: dict | None = None,
           limit: int = 20, cursor: list | None = None) -> dict:
    """v2 统一召回（spec_v2 §9 / §3.1 白名单）。

    - 结构化筛选先缩小候选池（分类 any/all、心情标签、事件日期范围）；
    - query 为空 = 浏览该池（不强迫全文匹配）；query 非空在池内 BM25 排序
      （bm25 值越小越相关，[T1]）；
    - 文本命中只来自当前允许投影：未遗忘=事件正文，遗忘=事后摘要
      （标题/心情文字/我们的话/回忆/原文永不参与——投影构造层保证）；
    - 按 memory_id 去重；分页游标：浏览=[last_date, last_id]，
      关键词=offset；每条带 matched_by / matched_fields / 表示版本。
    """
    filters = filters or {}
    limit = max(1, min(int(limit), 100))
    where = ["m.visibility='active'"]
    params: list = []

    cats = [c for c in (filters.get("categories") or []) if c]
    if cats:
        marks = ",".join("?" * len(cats))
        if filters.get("category_match", "any") == "all":
            where.append(
                f"m.memory_id IN (SELECT memory_id FROM memory_categories"
                f" WHERE category IN ({marks})"
                f" GROUP BY memory_id HAVING COUNT(DISTINCT category)=?)")
            params += cats + [len(set(cats))]
        else:
            where.append(
                f"EXISTS(SELECT 1 FROM memory_categories c WHERE"
                f" c.memory_id=m.memory_id AND c.category IN ({marks}))")
            params += cats

    tags = [t for t in (filters.get("mood_tags") or []) if t]
    if tags:
        marks = ",".join("?" * len(tags))
        if filters.get("mood_match", "any") == "all":
            where.append(
                f"m.memory_id IN (SELECT memory_id FROM memory_mood_tags"
                f" WHERE tag IN ({marks})"
                f" GROUP BY memory_id HAVING COUNT(DISTINCT tag)=?)")
            params += tags + [len(set(tags))]
        else:
            where.append(
                f"EXISTS(SELECT 1 FROM memory_mood_tags t WHERE"
                f" t.memory_id=m.memory_id AND t.tag IN ({marks}))")
            params += tags

    dr = filters.get("event_date") or {}
    if dr.get("from"):
        where.append("m.memory_date >= ?")
        params.append(dr["from"])
    if dr.get("to"):
        where.append("m.memory_date <= ?")
        params.append(dr["to"])

    phrase = projection.compile_query(query or "")
    hits: list[dict] = []
    next_cursor = None

    if phrase:
        # 关键词模式：池内 FTS + BM25（升序=更相关）；offset 游标
        offset = int(cursor[0]) if cursor and len(cursor) == 1 and str(
            cursor[0]).isdigit() else 0
        sql = (
            "SELECT m.memory_id, m.memory_date, m.compression_state,"
            " m.current_version_no, rd.projection_kind, bm25(search_fts) AS rank"
            " FROM memories m"
            " JOIN retrieval_documents rd ON rd.memory_id = m.memory_id"
            " JOIN search_fts ON search_fts.memory_id = m.memory_id"
            f" WHERE {' AND '.join(where)} AND search_fts MATCH ?"
            " ORDER BY rank, m.memory_id LIMIT ? OFFSET ?")
        rows = conn.execute(sql, params + [phrase, limit + 1, offset]).fetchall()
        for r in rows[:limit]:
            hits.append({
                "memory_id": r["memory_id"],
                "matched_by": ("summary_keyword"
                               if r["compression_state"] == "forgotten_summary"
                               else "keyword"),
                "matched_fields": (["summary_body"]
                                   if r["compression_state"] == "forgotten_summary"
                                   else ["event_text"]),
                "projection_kind": r["projection_kind"],
                "memory_version": r["current_version_no"],
                "memory_date": r["memory_date"],
            })
        if len(rows) > limit:
            next_cursor = [str(offset + limit)]
        return {"hits": hits, "query": query, "mode": "keyword",
                "filters_applied": _filters_summary(filters),
                "next_cursor": next_cursor, "limit": limit}

    # 浏览模式：稳定键游标 (memory_date DESC, memory_id DESC)
    extra = ""
    if cursor and len(cursor) == 2 and cursor[0]:
        where.append("(m.memory_date < ? OR (m.memory_date = ? AND"
                     " m.memory_id < ?))")
        params += [cursor[0], cursor[0], cursor[1] or ""]
    sql = ("SELECT m.memory_id, m.memory_date, m.compression_state,"
           " m.current_version_no FROM memories m"
           f" WHERE {' AND '.join(where)}"
           " ORDER BY m.memory_date DESC, m.memory_id DESC LIMIT ?")
    rows = conn.execute(sql, params + [limit + 1]).fetchall()
    for r in rows[:limit]:
        hits.append({
            "memory_id": r["memory_id"],
            "matched_by": "filter",
            "matched_fields": [],
            "projection_kind": r["compression_state"],
            "memory_version": r["current_version_no"],
            "memory_date": r["memory_date"],
        })
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = [last["memory_date"], last["memory_id"]]
    return {"hits": hits, "query": "", "mode": "browse",
            "filters_applied": _filters_summary(filters),
            "next_cursor": next_cursor, "limit": limit}


def _filters_summary(filters: dict) -> dict:
    out = {}
    if filters.get("categories"):
        out["categories"] = list(filters["categories"])
        out["category_match"] = filters.get("category_match", "any")
    if filters.get("mood_tags"):
        out["mood_tags"] = list(filters["mood_tags"])
        out["mood_match"] = filters.get("mood_match", "any")
    if filters.get("event_date"):
        out["event_date"] = filters["event_date"]
    return out


def search(conn, query: str, limit: int = 20,
           related_of: str | None = None) -> dict:
    """related_of：按关联找到的桶（matched_by=relation），不依赖文本匹配。"""
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
        # 语义是补充召回：关键词为主路径，语义命中取 top-5（同质语料防泛化）
        sem = semantic.semantic_search(conn, query, min(5, limit))
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
