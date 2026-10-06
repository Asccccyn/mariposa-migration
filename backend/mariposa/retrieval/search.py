"""memory.search：权限过滤 -> 有效投影 -> FTS -> 结果表示（§8.2）。

语义 provider 未配置时关键词照常工作，返回 degraded: semantic_unavailable；
不用随机向量或 mock 相似度冒充语义搜索。
"""
from __future__ import annotations

from .. import config
from ..errors import Forbidden
from ..memory import relations as relations_mod
from . import projection, semantic



def _allowed_field_kinds(conn, memory_ids: list[str]) -> dict[str, set[str]]:
    """逐桶现算阶段，返回允许参与命中的 field kinds（2026-09-30 裁定：
    memory.search/recall 底层进入 v1.7 字段矩阵；延迟 import 避免
    retrieval→recall 顶层循环依赖）。"""
    from ..recall import phase_policy
    from . import field_projection
    out: dict[str, set[str]] = {}
    # P1-05（2026-10-05 审计）：批量装载事实——此前逐桶 phase_of 每桶
    # 新开一条 formal 连接，与 recall 主线同病
    facts = phase_policy.facts_for_many(conn, memory_ids)
    for mid in memory_ids:
        f = facts.get(mid)
        if f is None:
            continue
        try:
            fields = phase_policy.eligible_fields(
                phase_policy.phase_from_facts(f))
        except phase_policy.DataGap:
            # v1 存量桶（held_at 缺失）无阶段事实：保守按最小允许集
            # （仅事件正文）处理，不用猜测的宽松阶段放大命中面
            fields = phase_policy.CORE_FIELDS
        out[mid] = set(field_projection.stage_filter_kinds(fields))
    return out


def _stage_scoped_hits(conn, query: str, where: list[str],
                       params: list) -> list[tuple[str, list[str]]]:
    """全量审计 P2-01：兼容入口复用 Runtime 的 scoped BM25——
    scope 池 → 逐桶当前阶段允许字段 → 分字段文档打分，阶段过滤在
    打分/截断**之前**生效。此前先 FTS rank LIMIT 再过滤，CORE 桶的
    不允许 title 命中会占满取数窗，把后面的合法 event_text 挤出
    候选（false negative）。返回 [(memory_id, [命中字段...])] 按
    分数降序。"""
    from ..recall import pipeline as pl
    from . import query_plan as qp
    from . import scoped_bm25
    terms, phrases = qp.plan_token_groups({"original_request": query})
    if not (terms or phrases):
        return [], False
    # CB-043（2026-10-02 审计 P2）：池安全阀截断状态贯穿返回——此前
    # 丢弃后零命中与"池未查尽"无法区分（false negative + 不诚实覆盖）
    pool_ids, pool_trunc = pl._scope_pool_ids(conn, where, params)
    pool_ids = [m for m in pool_ids if m]
    if not pool_ids:
        return [], pool_trunc
    allowed = _allowed_field_kinds(conn, pool_ids)
    docs: list[dict] = []
    for i in range(0, len(pool_ids), 500):
        chunk = [m for m in pool_ids[i:i + 500] if allowed.get(m)]
        if not chunk:
            continue
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
                "SELECT memory_id, field_kind, text_norm FROM"
                f" field_search_docs WHERE memory_id IN ({marks})",
                chunk).fetchall():
            kinds = allowed.get(row["memory_id"])
            if not kinds or row["field_kind"] not in kinds:
                continue
            docs.append({"owner": row["memory_id"],
                         "field": row["field_kind"],
                         "tokens": scoped_bm25._doc_tokens(
                             row["text_norm"] or "")})
    scored = scoped_bm25.score_documents(docs, terms, phrases)
    return ([(e["owner"], list(e["fields"])) for e in scored],
            pool_trunc)


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
        # 2026-10-05 冻结裁定（当晚修订）：mood_tags 只按大类过滤
        # （与 by_emotion 同语义）；子心情在 mood_note，不做筛选键
        from ..memory.service import MOOD_CATEGORIES
        bad = [t for t in tags if t not in MOOD_CATEGORIES]
        if bad:
            raise Forbidden(
                "mood_tags 只能是大类（固定 8 个）："
                + "、".join(MOOD_CATEGORIES),
                code="MOOD_CATEGORY_REQUIRED", got=bad)
        tags = list(dict.fromkeys(tags))
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


def _recall_keyword(conn, query: str, where: list[str], params: list,
                    filters: dict, limit: int, cursor: list | None) -> dict:
    """关键词模式：池内 scoped BM25（全量审计 P2-01：阶段字段过滤在
    打分/截断**之前**，CORE 桶的不允许命中不再挤掉合法 event_text）；
    offset 游标；matched_fields 如实标注真实命中字段。
    """
    offset = int(cursor[0]) if cursor and len(cursor) == 1 and str(
        cursor[0]).isdigit() else 0
    ordered, pool_trunc = _stage_scoped_hits(conn, query, where, params)
    page = ordered[offset:offset + limit]
    hits = []
    for mid, eff in page:
        r = conn.execute(
            "SELECT memory_id, memory_date, compression_state,"
            " current_version_no FROM memories WHERE memory_id=?",
            (mid,)).fetchone()
        matched_by = ("summary_keyword"
                      if r["compression_state"] == "forgotten_summary"
                      else "keyword")
        hits.append(_hit(r, matched_by, eff))
    total_kept = len(ordered)
    next_cursor = ([str(offset + limit)]
                   if total_kept > offset + limit else None)
    # CB-043：池安全阀触顶——本页零命中不代表完整查尽，truncated
    # 如实外露且不再签"终结"游标语义
    out = {"hits": hits, "query": query, "mode": "keyword",
           "filters_applied": _filters_summary(filters),
           "next_cursor": next_cursor, "limit": limit}
    if pool_trunc:
        out["coverage"] = "partial_pool_cap"
        out["pool_truncated"] = True
    return out


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
    - 文本命中来自当前阶段允许的分字段投影（v1.7 字段矩阵：
      WIDE 含 title/our_words，MID 含 title，CORE 仅 event_text；
      why/meaning/mood_note/回忆/原文永不参与——投影构造层保证）；
    - 按 memory_id 去重；分页游标：浏览=[last_date, last_id]，
      关键词=offset；每条带 matched_by / matched_fields / 表示版本。
    """
    filters = filters or {}
    limit = max(1, min(int(limit), 100))
    where, params = _pool_where(filters)
    if (query or "").strip():
        return _recall_keyword(conn, query, where, params, filters, limit,
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
        # 2026-09-30 裁定 + 全量审计 P2-01：接口兼容（名称/参数/返回
        # 结构），底座=Runtime scoped BM25——阶段字段过滤在 Top-K
        # 之前；why/meaning 等禁检来源不参与
        ordered, pool_trunc = _stage_scoped_hits(
            conn, query, ["m.visibility='active'"], [])
        for mid, eff in ordered[:limit]:
            m = conn.execute(
                "SELECT compression_state, current_version_no FROM memories"
                " WHERE memory_id=?", (mid,)).fetchone()
            hits.append({
                "memory_id": mid,
                "matched_by": ("summary_keyword"
                               if m["compression_state"] == "forgotten_summary"
                               else "keyword"),
                "matched_fields": eff,
                "projection_kind": m["compression_state"],
                "memory_version": m["current_version_no"],
            })
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
    semantic_error: str | None = None
    from .semantic import LOCAL_PROVIDERS
    if config.SEMANTIC_PROVIDER in LOCAL_PROVIDERS and phrase:
        # 语义是补充召回：关键词为主路径，语义命中取 top-5（同质语料防泛化）
        # 裁定 2026-10-05（语义修正说明 §二.5）：provider 配置但模型
        # 加载/推理失败时**明确降级**——关键词主路径照常、degraded 如实
        # 标注、stderr 留痕；不偷偷 fallback 也不让补充路径 500 掉主路径
        try:
            sem = semantic.semantic_search(conn, query, min(5, limit))
            mode = "hybrid"
        except Exception as e:  # noqa: BLE001
            import sys
            sys.stderr.write(
                f"[semantic] provider error, keyword path continues: "
                f"{e!r}\n")
            sem = []
            semantic_error = "semantic_provider_error"
    else:
        sem = []
    # 语义命中去重（关键词已命中的桶保留 keyword 标注优先）；
    # pending 哨兵不是候选，剥离
    seen = {h["memory_id"] for h in hits}
    for sh in sem:
        if isinstance(sh, dict) and "__pending_vectors__" in sh:
            continue
        if sh["memory_id"] not in seen:
            # CB-017（2026-10-02 审计 P1）：兼容检索入口不得附送未判断
            # 的 dense 正文——whitelist_body 只进 Recall 的 judge 管线
            #（S10 出站硬门），此处仅返回未交付候选元数据（要正文走
            # memory.open / Recall 正规链路）
            hits.append({k: v for k, v in sh.items()
                         if k != "whitelist_body"})

    result: dict = {"hits": hits, "query": query, "mode": mode}
    # RA-017（2026-10-02 复审 P2）：池安全阀截断状态贯穿兼容入口
    if phrase and pool_trunc:
        result["coverage"] = "partial_pool_cap"
        result["pool_truncated"] = True
    if config.SEMANTIC_PROVIDER in LOCAL_PROVIDERS:
        result["semantic"] = config.SEMANTIC_PROVIDER
    else:
        result["semantic"] = "unavailable"
        result["degraded"] = "semantic_unavailable"
    if semantic_error:
        result["semantic"] = "error"
        result["degraded"] = semantic_error
    return result
