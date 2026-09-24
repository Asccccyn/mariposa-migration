"""memory.search：权限过滤 -> 有效投影 -> FTS -> 结果表示（§8.2）。

语义 provider 未配置时关键词照常工作，返回 degraded: semantic_unavailable；
不用随机向量或 mock 相似度冒充语义搜索。
"""
from __future__ import annotations

from .. import config
from ..memory import relations as relations_mod
from . import projection, semantic


def _pool_where(filters: dict) -> tuple[list[str], list]:
    """结构化筛选 → (where 片段, 参数)。跨维度 AND；同维度默认 any（D04）。"""
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
    if dr.get("from") or dr.get("to"):
        # SEARCH-03：按事件发生时间筛（memory_date 或 occurred 区间重叠），
        # 不按 hold/回执/入库时间。occurred 存 ISO 时刻，比较按日期前缀。
        cond = []
        cp: list = []
        if dr.get("from"):
            cond.append("m.memory_date >= ?")
            cp.append(dr["from"])
        if dr.get("to"):
            cond.append("m.memory_date <= ?")
            cp.append(dr["to"])
        base = "(" + " AND ".join(cond) + ")"
        overlap = ("(m.occurred_start IS NOT NULL AND"
                   " substr(m.occurred_start,1,10) <= ?")
        op: list = [dr.get("to", "9999-12-31")]
        if dr.get("from"):
            overlap += (" AND (m.occurred_end IS NULL OR"
                        " substr(m.occurred_end,1,10) >= ?)")
            op.append(dr["from"])
        overlap += ")"
        where.append(f"({base} OR {overlap})")
        params += cp + op
    return where, params


def _hit(row, matched_by: str, matched_fields: list[str]) -> dict:
    return {
        "memory_id": row["memory_id"],
        "matched_by": matched_by,
        "matched_fields": matched_fields,
        "projection_kind": row["projection_kind"] if "projection_kind" in row.keys()
                           else row["compression_state"],
        "memory_version": row["current_version_no"],
        "memory_date": row["memory_date"],
    }


def _recall_keyword(conn, phrase: str, where: list[str], params: list,
                    filters: dict, limit: int, cursor: list | None) -> dict:
    """关键词模式：池内 FTS + BM25（升序=更相关，[T1]）；offset 游标。

    matched_fields 如实标注（D17）：v1 旧桶投影含 why/meaning 层（祖父
    条款），命中未必来自事件正文——用投影自带的 whitelist_body（白名单
    主字段正文）比对，不在其中的标 legacy_projection，不冒充 event_text。
    检索层只读投影表，不回读正文版本表。
    """
    offset = int(cursor[0]) if cursor and len(cursor) == 1 and str(
        cursor[0]).isdigit() else 0
    probe = phrase.strip('"')
    sql = (
        "SELECT m.memory_id, m.memory_date, m.compression_state,"
        " m.current_version_no, rd.projection_kind, bm25(search_fts) AS rank,"
        " rd.whitelist_body"
        " FROM memories m"
        " JOIN retrieval_documents rd ON rd.memory_id = m.memory_id"
        " JOIN search_fts ON search_fts.memory_id = m.memory_id"
        f" WHERE {' AND '.join(where)} AND search_fts MATCH ?"
        " ORDER BY rank, m.memory_id LIMIT ? OFFSET ?")
    rows = conn.execute(sql, params + [phrase, limit + 1, offset]).fetchall()
    hits = []
    for r in rows[:limit]:
        body = r["whitelist_body"]
        if r["compression_state"] == "forgotten_summary":
            matched_by = "summary_keyword"
            fields = (["summary_body"] if body and probe in body
                      else ["forget_tags"])
        elif body is None:
            matched_by = "keyword"
            fields = ["projection"]  # 旧投影行未带成分，不冒充字段命中
        else:
            matched_by = "keyword"
            fields = (["event_text"] if probe in body
                      else ["legacy_projection"])
        hits.append(_hit(r, matched_by, fields))
    next_cursor = [str(offset + limit)] if len(rows) > limit else None
    return {"hits": hits, "query": phrase, "mode": "keyword",
            "filters_applied": _filters_summary(filters),
            "next_cursor": next_cursor, "limit": limit}
    next_cursor = [str(offset + limit)] if len(rows) > limit else None
    return {"hits": hits, "query": phrase, "mode": "keyword",
            "filters_applied": _filters_summary(filters),
            "next_cursor": next_cursor, "limit": limit}


def _recall_browse(conn, where: list[str], params: list, filters: dict,
                   limit: int, cursor: list | None) -> dict:
    """浏览模式：query 为空直接浏览池（稳定键游标 memory_date DESC）。"""
    if cursor and len(cursor) == 2 and cursor[0]:
        where.append("(m.memory_date < ? OR (m.memory_date = ? AND"
                     " m.memory_id < ?))")
        params += [cursor[0], cursor[0], cursor[1] or ""]
    sql = ("SELECT m.memory_id, m.memory_date, m.compression_state,"
           " m.current_version_no FROM memories m"
           f" WHERE {' AND '.join(where)}"
           " ORDER BY m.memory_date DESC, m.memory_id DESC LIMIT ?")
    rows = conn.execute(sql, params + [limit + 1]).fetchall()
    hits = [_hit(r, "filter", []) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = [last["memory_date"], last["memory_id"]]
    return {"hits": hits, "query": "", "mode": "browse",
            "filters_applied": _filters_summary(filters),
            "next_cursor": next_cursor, "limit": limit}


def recall(conn, query: str = "", filters: dict | None = None,
           limit: int = 20, cursor: list | None = None) -> dict:
    """v2 统一召回（spec_v2 §9 / §3.1 白名单）。

    - 结构化筛选先缩小候选池（分类 any/all、心情标签、事件日期范围）；
    - query 为空 = 浏览该池（不强迫全文匹配）；query 非空在池内 BM25 排序；
    - 文本命中只来自当前允许投影：未遗忘=事件正文，遗忘=事后摘要
      （标题/心情文字/我们的话/回忆/原文永不参与——投影构造层保证）；
    - 按 memory_id 去重；分页游标：浏览=[last_date, last_id]，
      关键词=offset；每条带 matched_by / matched_fields / 表示版本。
    """
    filters = filters or {}
    limit = max(1, min(int(limit), 100))
    where, params = _pool_where(filters)
    phrase = projection.compile_query(query or "")
    if phrase:
        return _recall_keyword(conn, phrase, where, params, filters, limit,
                               cursor)
    return _recall_browse(conn, where, params, filters, limit, cursor)


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
        # SEARCH-05：合并按 memory_id 去重——关键词已命中的桶不因关联重复出现
        seen_kw = {h["memory_id"] for h in hits}
        for mid in relations_mod.related_ids(conn, related_of):
            if mid in seen_kw:
                continue
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
    # 语义路径（§8.3：仅有效投影向量参与；provider 未配置显式 degraded）。
    # 空 query 走浏览语义，不做语义匹配（避免空向量产生无依据"伪命中"）
    mode = "keyword"
    if config.SEMANTIC_PROVIDER == "local_bge_zh" and phrase:
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
