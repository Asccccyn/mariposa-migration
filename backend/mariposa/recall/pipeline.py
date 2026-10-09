"""召回检索管线（现行架构，WP-05/D06 死适配层已删）：
- round1_lexical_hits：阶段过滤 scope BM25（S06）——主线 Round 1 候选；
- raw_deep_search：Round 2 原文深搜（gate 由 round2._round2_gate 服务端
  核验——旧 pipeline.round2_gate 死适配层的 judge_status=='complete'
  恒假、伪回执行查询，误接线即 Round2 永拒，2026-10-09 删除排雷）；
- mark_round1_complete_tx：Round1 完成回执（最终事务内）。
判断层出站由 recall/service 的 judge 流程承担；本模块不旁路直出正文。
"""
from __future__ import annotations

from .. import config, db
from ..errors import Forbidden
from ..source import query as source_query
from . import phase_policy as pp
from ..retrieval import projection


# ---------------------------------------------------------------- Round 1

def _scope_pool_ids(conn, where: list[str], params: list,
                    batch: int = 500) -> tuple[list[str], bool]:
    """S19（四轮复审前提前收口）：scope 候选池 keyset 分页取尽。

    旧实现的 `LIMIT 2000` 会让第 2001+ 个作用域桶永远不被检索、
    coverage 却仍签 complete_within_scope（林石见两轮如实申报的
    已知未闭项）。现在分页迭代到取尽；RECALL_POOL_MAX_BUCKETS 是
    防失控安全阀，触顶前先探针确认还有下一桶才标 truncated=True
    ——调用方对 truncated 池不得再签 complete。返回 (ids, truncated)。
    """
    cap = max(1, config.RECALL_POOL_MAX_BUCKETS)
    w = list(where or []) + ["m.memory_id > ?"]
    cond = "WHERE " + " AND ".join(w)
    base = list(params or [])
    ids: list[str] = []
    cursor = ""
    truncated = False
    while True:
        rows = conn.execute(
            f"SELECT m.memory_id FROM memories m {cond}"
            " ORDER BY m.memory_id LIMIT ?",
            base + [cursor, batch]).fetchall()
        if not rows:
            break
        ids.extend(r["memory_id"] for r in rows)
        if len(ids) >= cap:
            del ids[cap:]
            more = conn.execute(
                f"SELECT 1 FROM memories m {cond} LIMIT 1",
                base + [ids[-1]]).fetchone()
            truncated = more is not None
            break
        if len(rows) < batch:
            break
        cursor = ids[-1]
    return ids, truncated


# ---------------------------------------------------------------- Round 2

def mark_round1_complete_tx(conn, session_id: str) -> None:
    """同语义的事务内版本（commit-at-end 最终事务调用）。"""
    # P3（2026-10-05 审计）：datetime('now') 产 SQLite 空格格式，与
    # store.add_receipts 的 ISO+tz 同列混存——字典序比较（purge/租约
    # 扫描依赖）跨格式会错乱；统一 ISO 8601 带时区
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO recall_receipts(receipt_id, session_id,"
        " resource_ref, valid_at, created_at)"
        " VALUES(?,?,?,?,?)",
        (f"rr_{session_id[:12]}_r1", session_id, "round1:complete",
         now, now))


def raw_deep_search(principal, plan: dict, limit: int = 20,
                    offset: int = 0) -> dict:
    """Round 2 raw 深搜：获准 source 层（published=1，human/assistant）。

    候选仍须经同一层 Jev 出站（由调用方装配）；本函数只做检索与证据
    定位。旧 raw_* 层（合成导入）不在 v1.7 深搜范围（见 FIX_REPORT §5）。
    """
    # 复审#2：多词 OR 以已编译 FTS 表达式传入（source_query 不再
    # 二次编译吃掉 OR）；speaker 硬过滤；offset 分页 + has_more。
    # 三轮复审#4：raw Round2 继承同一 query revision 的负向条件
    #（speaker_excluded/source_date_excluded），与 words 稀疏/dense
    # 同一 source_scope；三轮复审#5：excerpt 锚词 = 原始 terms/phrases
    #（用于命中窗口定位），绝不拿 FTS 表达式本身当关键词找正文
    terms = [t for t in (plan.get("lexical_terms") or [])
             if isinstance(t, str) and t.strip()]
    from ..retrieval import projection as _proj
    from ..retrieval import query_plan as _qp
    or_phrases = [_proj.compile_query(t) for t in terms]
    or_phrases = [q for q in or_phrases if q]
    # CB-015（2026-10-02 审计 P1）：exact_phrases 是逐字硬约束——
    # 编译为 FTS 表达式的 AND 子句，而不只是 excerpt 锚词。此前
    # exact-only 查询（terms 为空）fts_expr=None，Raw 退化为全量
    # 浏览并交付无关原文。
    and_phrases = [_proj.compile_query(p)
                   for p in (plan.get("exact_phrases") or [])
                   if isinstance(p, str) and p.strip()]
    and_phrases = [q for q in and_phrases if q]
    _parts: list[str] = []
    if or_phrases:
        _parts.append("(" + " OR ".join(or_phrases) + ")")
    _parts.extend(and_phrases)
    fts_query = " AND ".join(_parts) if _parts else None
    scope = _qp.source_scope(plan)
    anchors = terms + [p for p in (plan.get("exact_phrases") or [])
                       if isinstance(p, str) and p.strip()]
    res = source_query.search(
        None, fts_expr=fts_query or None,
        senders=["human", "assistant"],
        speaker=scope["speaker"],
        date_from=scope["date_from"], date_to=scope["date_to"],
        speakers_excluded=scope["speakers_excluded"],
        date_ranges_excluded=scope["date_ranges_excluded"],
        anchor_terms=anchors or None,
        limit=limit, offset=offset)
    hits = []
    for h in res["hits"]:
        hits.append({
            "resource_ref": f"source_msg:{h['message_id']}",
            "channel": "raw",
            "provider_message_id": h["provider_message_id"],
            "conversation_id": h["provider_conversation_id"],
            "speaker": h["speaker"],
            "excerpt": h["excerpt"],
            # RRA-010：全文随 hit（off 模式候选全文化用；命中窗口仅判断
            # 路径的定位证据）
            "text": h.get("text"),
            "occurred_at": h["created_at"],
            "evidence_kind": "raw_verbatim",
            "content_role": "retrieved_memory",
            "instruction_authority": "none",
        })
    return {"hits": hits, "source": "source_layer",
            "has_more": bool(res.get("has_more")),
            "next_offset": (offset + limit) if res.get("has_more")
            else None,
            "boundary": "Round 2 原文深搜；候选需经同一层 Jev 出站"}


# ------------------------------------------------ 主线适配层（A04 接线）

def round1_lexical_hits(conn, plan: dict, rejected: set[str],
                        coverage: dict) -> list[dict]:
    """阶段过滤词法检索（S06 scope-stage-bm25-v1，WP03 重写）。

    - 检索面：scope（结构过滤+排除）内、每桶当前阶段允许字段的
      分字段文档（field_search_docs.text_norm，不含禁检来源）；
    - 排序分：本模块内计算的真 BM25（对数 IDF/df/avgdl 只来自该
      授权集合；k1=1.5、b=0.75），field_fts 不再提供排序分，
      也不做逐桶 N+1 MATCH；
    - 每 memory 取其命中文档的最大分（不累加"分类多/话语多"选票），
      matched_fields 如实标注。
    """
    from ..retrieval import query_plan as qp
    from ..retrieval import field_projection as fp_mod
    from ..retrieval import scoped_bm25
    base_where, base_params, _, _ = qp.AllowedScope.for_plan(plan)
    where = list(base_where)
    params = list(base_params)
    if rejected:
        ids = [r[len("memory:"):] for r in rejected
               if r.startswith("memory:")]
        if ids:
            marks = ",".join("?" * len(ids))
            where.append(f"m.memory_id NOT IN ({marks})")
            params += ids
    terms, phrases = qp.plan_token_groups(plan)
    if not (terms or phrases):
        return []  # 浏览模式由主线处理

    pool_ids, pool_truncated = _scope_pool_ids(conn, where, params)

    from datetime import datetime as _dt, timezone as _tz
    _now = _dt.now(_tz.utc)
    stats = {"WIDE": 0, "MID": 0, "CORE": 0, "gap": 0}
    allowed_by_mid: dict[str, set] = {}
    # P1-05（2026-10-05 审计）：批量装载阶段事实——此前逐桶 phase_of
    # 每桶新开一条 formal 连接跑 4 条查询，池上限 2 万桶即 2 万次
    # 连接开关；facts_for_many 单连接三次查询等价替代
    facts = pp.facts_for_many(conn, pool_ids)
    for mid in pool_ids:
        if f"memory:{mid}" in rejected:
            continue
        f = facts.get(mid)
        if f is None:
            stats["gap"] += 1
            continue
        try:
            phase = pp.phase_from_facts(f, now=_now)
        except pp.DataGap:
            # P1-03（2026-10-05 审计）：v1 存量桶（held_at 缺失）此前
            # 被整桶跳过且 coverage 仍签 complete_within_scope——假完整
            # 回执给 Round2 背书。与 retrieval/search._stage_fields 同
            # 语义：保守按最小允许集纳入扫描（不猜宽松阶段放大命中
            # 面），既不再漏桶也不再虚签完整
            stats["CORE"] += 1
            kinds = fp_mod.stage_filter_kinds(pp.CORE_FIELDS)
            if kinds:
                allowed_by_mid[mid] = set(kinds)
            continue
        except (pp.PolicyError, KeyError):
            stats["gap"] += 1
            continue
        stats[phase.stage] += 1
        kinds = fp_mod.stage_filter_kinds(pp.eligible_fields(phase))
        if kinds:
            allowed_by_mid[mid] = set(kinds)

    # P1-03 补充（2026-10-05）：coverage event 维持 S19 裁定语义——
    # complete 与否只由"池是否取尽"决定（truncated 不签 complete）；
    # 逐桶不可分类（CATEGORY_REQUIRED 等 PolicyError）是池内非参与
    # 成员，计入 stage_filter.gap 元数据、不构成不完整。DataGap 桶
    # 已在上文按 CORE 兜底纳入扫描，不再落入 gap
    def _coverage_event() -> str:
        return ("partial_pool_truncated" if pool_truncated
                else "complete_within_scope")

    if not allowed_by_mid:
        # S19：truncated 池不签 complete（说得过头 = 假"完整搜过"）
        coverage["event"] = _coverage_event()
        coverage["stage_filter"] = stats
        coverage["event_pool"] = {"scanned": len(pool_ids),
                                  "truncated": pool_truncated}
        return []

    # 批量取 scope 内全部分字段文档，内存按每桶允许字段过滤后评分
    mids = list(allowed_by_mid)
    docs: list[dict] = []
    for i in range(0, len(mids), 500):
        chunk = mids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
                "SELECT memory_id, field_kind, text_norm FROM"
                f" field_search_docs WHERE memory_id IN ({marks})",
                chunk).fetchall():
            allowed = allowed_by_mid.get(row["memory_id"])
            if not allowed or row["field_kind"] not in allowed:
                continue
            docs.append({"owner": row["memory_id"],
                         "field": row["field_kind"],
                         "tokens": scoped_bm25._doc_tokens(
                             row["text_norm"] or "")})

    flat_terms = [g for g in terms]
    scored = scoped_bm25.score_documents(docs, flat_terms, phrases)
    # S19：全量迭代 + 安全阀诚实化——metadata（event_pool）按三轮#1
    # 白名单语义不参与 family 完整性判断
    coverage["event"] = _coverage_event()
    coverage["stage_filter"] = stats
    coverage["lexical_scorer"] = scoped_bm25.SCORER_VERSION
    coverage["event_pool"] = {"scanned": len(pool_ids),
                              "truncated": pool_truncated}

    hits: list[dict] = []
    if not scored:
        return hits
    ids = [e["owner"] for e in scored]
    meta: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for m in conn.execute(
                "SELECT m.memory_id, m.memory_date, m.compression_state,"
                " m.current_version_no, rd.projection_kind,"
                " rd.whitelist_body, v.original_title FROM memories m"
                " JOIN retrieval_documents rd ON rd.memory_id=m.memory_id"
                " LEFT JOIN memory_versions v ON v.memory_id=m.memory_id"
                "   AND v.version_no=m.current_version_no"
                f" WHERE m.memory_id IN ({marks})", chunk).fetchall():
            meta[m["memory_id"]] = dict(m)
    for e in scored:
        m = meta.get(e["owner"])
        if m is None:
            continue
        matched = dict(e.get("excerpt_by_field") or {})
        # our_words 字段命中：定位具体话语原文（复审#1——拼接字段
        # 的命中窗不能冒充原话；word_id 可反查）
        if "our_words" in matched:
            loc = _locate_word_text(conn, e["owner"], terms, phrases)
            if loc:
                matched["our_words"] = loc["text"]
                matched["our_words_word_id"] = loc["word_id"]
        card = {
            "resource_ref": f"memory:{e['owner']}",
            "candidate_ref": f"memory:{e['owner']}",
            "memory_id": e["owner"], "channel": "event",
            "representation": m["projection_kind"],
            "content_version": str(m["current_version_no"]),
            "representation_version": str(m["current_version_no"]),
            "projection_version": config.PROJECTION_REVISION,
            "memory_date": m["memory_date"],
            "matched_by": (["summary_keyword"]
                           if m["compression_state"] == "forgotten_summary"
                           else ["keyword"]),
            "matched_fields": e["fields"],
            "bm25_score": e["score"],
            "_matched": matched,
            "_row": m,
        }
        hits.append(card)
    return hits


def _locate_word_text(conn, memory_id: str, term_groups,
                      phrases=None) -> dict | None:
    """三轮复审#3：在该桶 our_words 逐句上复用候选打分的同一查询
    语义（term 组内连续、组间 OR；phrases AND）定位真实命中话语。

    token 交叠定位会把"小猫今天很乖"当成"小路灯"的命中（组内任一
    token 命中即取首句），不允许；打分与 round1 候选完全同源，最高分
    话语即 Jev 看到的 match evidence。返回 {"word_id", "text"}。
    """
    from ..retrieval import scoped_bm25
    rows = conn.execute(
        "SELECT word_id, text FROM memory_our_words WHERE memory_id=?"
        " ORDER BY ordinal", (memory_id,)).fetchall()
    if not rows:
        return None
    docs = [{"owner": r["word_id"], "field": "our_words",
             "tokens": scoped_bm25._doc_tokens(
                 projection.normalize_search_text(r["text"]))}
            for r in rows]
    scored = scoped_bm25.score_documents(
        docs, [g for g in (term_groups or [])],
        [p for p in (phrases or [])])
    if not scored:
        return None
    by_id = {r["word_id"]: r["text"] for r in rows}
    return {"word_id": scored[0]["owner"],
            "text": by_id.get(scored[0]["owner"])}

