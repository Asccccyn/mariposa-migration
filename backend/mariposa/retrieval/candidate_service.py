"""候选生成共享层（EST_MARIPOSA_AUTO_SURFACING_V1 · WP2 提取）。

2026-10-19 前这三段逻辑是 recall/service 的私有函数；自动浮现需要
复用**交付截断之前**的候选生成接缝（本包 README 五条红线之二），
逐字搬移到本模块，recall/service 以同名导入回接（service 模块级
名字保留——测试对 svc._event_candidates 打补丁仍生效）。

行为等价性约束：主动 recall 主线调用结果必须与提取前一致（既有
recall 测试全集即黄金用例）；本模块不引入 surface 专用行为。
"""
from __future__ import annotations

from .. import config
from . import evidence as evidence_mod
from . import query_plan as qp
from . import semantic


def event_candidates(conn, plan: dict, rejected: set[str],
                     coverage: dict, degraded: list[str]) -> list[dict]:
    """event 通道：结构化过滤前置的作用域内词法检索 + scoped 向量。

    词频统计只来自过滤后的池（HYBRID-06：加入无权语料不改变本 scope
    可见排名/计数，不只是结果最后被过滤）。dense 用同一 where 前置到
    评分与 Top-K 之前（HYBRID-01）；provider 未配置显式 unavailable，
    不伪造语义分（HYBRID-07）。
    """
    base_where, base_params, _, _ = qp.AllowedScope.for_plan(plan)
    lex_where = list(base_where)
    lex_params = list(base_params)
    if rejected:
        ids = [r[len("memory:"):] for r in rejected
               if r.startswith("memory:")]
        if ids:
            marks = ",".join("?" * len(ids))
            lex_where.append(f"m.memory_id NOT IN ({marks})")
            lex_params += ids

    terms, phrases = qp.plan_token_groups(plan)
    lexical_rows: list = []
    if terms or phrases:
        # v1.7 主线接线（A04）：阶段过滤 + 分字段索引替代整桶投影检索
        from ..recall import pipeline as _pl
        stage_hits = _pl.round1_lexical_hits(conn, plan, rejected, coverage)
        return stage_hits + dense_tail(conn, plan, terms, phrases,
                                       base_where, base_params, rejected,
                                       coverage, degraded)
    else:
        # 空 query = 浏览该池（HYBRID-09）：不构造随机/空语义向量
        lexical_rows = conn.execute(
            "SELECT m.memory_id, m.memory_date, m.compression_state,"
            " m.current_version_no, rd.projection_kind, rd.search_text,"
            " rd.whitelist_body FROM memories m"
            " JOIN retrieval_documents rd ON rd.memory_id = m.memory_id"
            f" WHERE {' AND '.join(lex_where)}"
            " ORDER BY m.memory_date DESC, m.memory_id DESC LIMIT ?",
            lex_params + [config.RECALL_LEXICAL_K]).fetchall()
        # 全量审计 P1-01/ROUND2-03：browse 只看 latest-K 窗口，不是
        # 完整 scope——不得签 complete（否则可冒充"完整搜过没有候选"
        # 给 Round2 背书）
        coverage["event"] = "partial_topk_window"

    lexical_hits = []
    for r in lexical_rows:
        ref = f"memory:{r['memory_id']}"
        if ref in rejected:
            continue
        lexical_hits.append({
            "resource_ref": ref, "candidate_ref": ref,
            "memory_id": r["memory_id"], "channel": "event",
            "representation": r["projection_kind"],
            "content_version": str(r["current_version_no"]),
            "representation_version": str(r["current_version_no"]),
            "projection_version": config.PROJECTION_REVISION,
            "memory_date": r["memory_date"],
            "matched_by": (["summary_keyword"]
                           if r["compression_state"] == "forgotten_summary"
                           else ["keyword"]),
            "matched_fields": ["event_text"],
            "_row": dict(r),
        })

    dense = dense_tail(conn, plan, terms, phrases, base_where,
                       base_params, rejected, coverage, degraded)
    return lexical_hits + dense


def dense_tail(conn, plan, terms, phrases, base_where, base_params,
               rejected, coverage, degraded) -> list[dict]:
    """dense 路独立函数（v1.7 接线后与阶段词法路并列）。

    S04/WP03：dense 不以词法命中为前提——纯 semantic_query 也可运行
    （query-only 语义检索）。"""
    dense_hits: list[dict] = []
    if plan.get("semantic_query"):
        from ..retrieval.semantic import LOCAL_PROVIDERS
        if config.SEMANTIC_PROVIDER in LOCAL_PROVIDERS:
            sem_raw = semantic.semantic_search(
                conn, plan["semantic_query"], config.RECALL_DENSE_K,
                extra_where=base_where, extra_params=base_params)
            pending = 0
            sem = []
            for entry in sem_raw:
                if isinstance(entry, dict) and "__pending_vectors__" in entry:
                    pending = entry["__pending_vectors__"]
                    continue
                sem.append(entry)
            coverage["dense_event"] = (
                "partial_vectors_pending" if pending
                else "complete_within_scope")
            if pending:
                coverage["dense_pending_vectors"] = pending
            for s in sem:
                ref = f"memory:{s['memory_id']}"
                if ref in rejected:
                    continue
                # P1-02：dense 卡携带真实版本与 v1.7 字段身份——
                # matched_fields=event_text（三阶段全部允许），
                # replay guard / receipt 重校验不再误杀 dense 卡
                dense_hits.append({
                    "resource_ref": ref, "candidate_ref": ref,
                    "memory_id": s["memory_id"], "channel": "event",
                    "representation": s.get("projection_kind", "full"),
                    "content_version": s.get("content_version"),
                    "representation_version": s.get("content_version"),
                    "projection_version": config.PROJECTION_REVISION,
                    "matched_by": ["semantic"],
                    "matched_fields": ["event_text"],
                    "score": s.get("score"),
                    # 事件正文证据由 attach_event_evidence 按此生成
                    "_row": {"whitelist_body": s.get("whitelist_body"),
                             "compression_state": "full"},
                })
            # 闭环复审 P2-8：pending 时不覆盖回 complete（上方的
            # partial_vectors_pending 必须存活到出口）
            coverage["dense_event"] = coverage.get(
                "dense_event", "complete_within_scope")
        else:
            # provider 未配置：显式 unavailable，不因空结果谎报完整（HYBRID-07）
            coverage["dense_event"] = "unavailable"
            degraded.append("semantic_unavailable")
    else:
        # S04：browse/词法回退场景（无显式 semantic_query）不构造
        # 向量也不报降级——not_requested 的判定依据是"是否显式请求
        # 语义"，不是词法回退是否产生了 phrases
        coverage["dense_event"] = (
            "not_requested" if not plan.get("semantic_query")
            else "unavailable")
        if (config.SEMANTIC_PROVIDER != "local_bge_zh"
                and plan.get("semantic_query")):
            degraded.append("semantic_unavailable")
    return dense_hits


def attach_event_evidence(conn, candidates: list[dict]) -> None:
    """event 候选证据：authored_event / approved_summary + structured_fact。

    authored_event 证明正式记录，不冒充 raw 原话（EVID-01）；遗忘桶只给
    approved_summary（§7.1）。
    """
    for c in candidates:
        if c.get("channel") != "event" or c.get("evidence"):
            continue
        # r2 S09：_row 保留在卡上（Jev 投影需要 title/event 主体）；
        # 出站前由 _finalize_cards 白名单剔除，不进最终 packet
        row = c.get("_row")
        mid = c["memory_id"]
        if row is None:
            row = {"whitelist_body": None,
                   "compression_state": "full"}
        body = row.get("whitelist_body") or ""
        snippet, truncated = evidence_mod.excerpt(body)
        if c["representation"] == "forgotten_summary":
            # v1.7：无压缩。遗留 forgotten_summary 表示（离线迁移前）不再
            # 从已退役的摘要表取数；标证据缺口，不冒充事件正文。
            ev = [evidence_mod.make_evidence(
                "approved_summary", "summary_body", "",
                f"memory:{mid}", source_version=c["content_version"],
                structured_value={"legacy_content_gap": True,
                                  "note": "旧摘要表示待一次性离线迁移恢复"})]
        else:
            ev = [evidence_mod.make_evidence(
                "authored_event", "event_text", snippet, f"memory:{mid}",
                source_version=c["content_version"], truncated=truncated)]
        cats = [r["category"] for r in conn.execute(
            "SELECT category FROM memory_categories WHERE memory_id=?",
            (mid,)).fetchall()]
        if cats:
            ev.append(evidence_mod.make_evidence(
                "structured_fact", "categories", "", f"memory:{mid}",
                structured_value={"categories": cats}))
        c["evidence"] = ev
