"""共享 Recall Session 统一编排（v1.3 §3—§14 / v1.4 §3、§9）。

Chat（MCP）与 CC（estómago）等权走同一入口：session 正本只在 Mariposa
runtime 库；检索 = 查询校验 → 授权范围内 BM25+向量粗召回 → 去重/RRF →
可选 Jev 精排 → 代码门控 → 0—3 条证据包；纠正（reject/refine/navigate）
只改本 session 临时状态，不修改正式记忆。

检索内容全链路 instruction_authority=none（§10）：候选正文中的指令
只是历史数据，不提升权限、不触发工具、不改变 scope。
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .. import config, db
from ..errors import Forbidden, NotFound
from ..memory import service as _mem_svc
canonical_hash = _mem_svc.canonical_hash
from ..retrieval import evidence as evidence_mod
from ..retrieval import query_plan as qp
from ..retrieval import semantic, selection, words as words_mod
from ..retrieval import fusion
from ..retrieval.judges import base as judge_base

from . import budget, state_machine, store
# 依赖边界批：共享原语自 shared 单向引入（重绑定本名——测试对
# svc.<name> 打补丁仍作用于 service 内调用路径）
from .shared import (_enforce_output_budget, _finalize_cards, _now_iso,
                     _op_id, _query_fp, _require_enabled,
                     _revalidate_session_in_tx, _with_round_preview,
                     require_owned_session)
from .models import validate_query_plan

_SEARCH_STATUSES = ("FOUND", "AMBIGUOUS", "CONFLICT", "NO_MATCH_OBSERVED",
                    "DEGRADED", "BUDGET_EXHAUSTED", "STALE_RETRY_REQUIRED",
                    "UNAVAILABLE")








def _virtual(session: dict, expected_revision: int, burst_no: int,
             bursts_used: int | None) -> dict:
    """refine 计算阶段的"虚拟前进后"session 视图（不落库）。"""
    v = dict(session)
    v["current_revision"] = expected_revision + 1
    v["current_burst"] = burst_no
    if bursts_used is not None:
        v["bursts_used"] = bursts_used
    return v


# ---------- 检索执行 ----------

def _event_candidates(conn, plan: dict, rejected: set[str],
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
        from . import pipeline as _pl
        stage_hits = _pl.round1_lexical_hits(conn, plan, rejected, coverage)
        return stage_hits + _dense_tail(conn, plan, terms, phrases,
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

    dense = _dense_tail(conn, plan, terms, phrases, base_where,
                        base_params, rejected, coverage, degraded)
    return lexical_hits + dense


def _dense_tail(conn, plan, terms, phrases, base_where, base_params,
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
                    # 事件正文证据由 _attach_event_evidence 按此生成
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


def _attach_event_evidence(conn, candidates: list[dict]) -> None:
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


def _words_evidence_insufficient(plan: dict, words_hits: list[dict]) -> bool:
    """words 证据不足判定（v1.4 §7.1）：空/未满足证据等级/来源失效。"""
    if plan.get("evidence_requirement") != "verbatim_required":
        return not words_hits
    if not words_hits:
        return True
    return not any(evidence_mod.meets_requirement(
        h.get("evidence") or [], "verbatim_required") for h in words_hits)


def _words_dense_fuse(conn, plan, words_hits, coverage):
    """words 通道 dense 融合（1005B 重构：从 _run_round_compute
    逐字拆出，行为零变更——各轮裁定注释随行保留。"""
    # S08/WP05：words 专项 dense（独立向量空间，独立阈值）
    if not plan.get("semantic_query"):
        return words_hits  # 无语义查询：稀疏结果原样（原 if 包裹语义）
    from ..retrieval import words_semantic as wsem
    if plan.get("semantic_query"):
        # 三轮复审#4：dense 侧过滤与稀疏/raw 同一 source_scope
        # （正/负 speaker + 正/负日期一套语义，不再各写一份）
        from ..retrieval import query_plan as _qp
        _wwhere, _wparams = _qp.source_scope_sql(
            _qp.source_scope(plan), "w.speaker", "m.memory_date")
        wdense_res = wsem.words_semantic_search(
            conn, plan["semantic_query"],
            limit=config.RECALL_LEXICAL_K,
            extra_where=_wwhere, extra_params=_wparams)
        # 全量审计 P1-04：结构化状态区分"正常搜完零命中"
        #（complete）/"向量未就绪"（partial）/"未配置"
        #（unavailable）——零命中不再误报 unavailable
        wdense = wdense_res["hits"]
        wpending = wdense_res["pending_vectors"]
        if not wdense_res["provider_active"]:
            coverage["words_dense"] = "unavailable"
        elif wpending:
            coverage["words_dense"] = "partial_vectors_pending"
            coverage["words_dense_pending_vectors"] = wpending
        else:
            coverage["words_dense"] = "complete_within_scope"
        if wdense:
            # 同通道 RRF：BM25 与 dense 各自成序后融合
            lex_ranked = fusion.family_rank(words_hits)
            dense_cards = []
            for h in wdense:
                # 闭环复审 P1-4：资源身份统一——dense 与
                # 稀疏 words 同用 our_word:<id>/channel=words
                #（S01：our_word 是对象，一份证据一个身份）
                # 复审#5：纯 dense word 补正式证据身份
                #（当前 memory version + word 分级证据）
                _mv = conn.execute(
                    "SELECT current_version_no FROM"
                    " memories WHERE memory_id=?",
                    (h["memory_id"],)).fetchone()
                _mv_s = (str(_mv["current_version_no"])
                         if _mv else None)
                # CB-039：dense 与 sparse 共用同一证据
                # 构造——等级取决于当前 provenance（来源
                # 当前事实/撤销换代），不取决于命中通道
                from ..retrieval.words import _word_evidence
                _wrow = {
                    "word_id": h["word_id"],
                    "expression_kind": h.get(
                        "expression_kind"),
                    "source_ref": h.get("source_ref"),
                    "source_binding_version": h.get(
                        "source_binding_version", 0),
                    "text": h.get("text"),
                    "current_version_no": _mv_s,
                }
                dense_cards.append({
                    "resource_ref":
                        f"our_word:{h['word_id']}",
                    "candidate_ref":
                        f"our_word:{h['word_id']}",
                    "word_id": h["word_id"],
                    "memory_id": h["memory_id"],
                    "channel": "words",
                    "representation": "full",
                    "content_version": _mv_s,
                    "representation_version": _mv_s,
                    "projection_version":
                        config.PROJECTION_REVISION,
                    "speaker": h["speaker"],
                    "expression_kind": h["expression_kind"],
                    "excerpt": h["text"],
                    "matched_by": ["semantic"],
                    "matched_fields": ["our_words"],
                    "speaker": h.get("speaker"),
                    "expression_kind":
                        h.get("expression_kind"),
                    "excerpt": h.get("text"),
                    "evidence": _word_evidence(_wrow, conn) or [
                        evidence_mod.make_evidence(
                        "word_unverified", "our_words", h.get("text")
                        or "", f"our_word:{h['word_id']}",
                        source_version=_mv_s)],
                })
            dense_ranked = fusion.family_rank(dense_cards)
            words_hits = fusion.rrf_fuse({
                "lexical": lex_ranked,
                "dense": dense_ranked})
        # P1-04：零命中（provider 正常）已在上方签
        # complete_within_scope，不落 unavailable
    return words_hits


def _judge_pass(session, plan, event_fused, words_hits, raw_pre,
                coverage, degraded, policy=None):
    """判断层精排+基数对账+unjudged 计数（1005B 拆出；2026-10-08
    MANUAL_HANDOFF_JUDGE_SWITCH_V1 政策化）。返回 (judge_result,
    judge_candidates, judge_items, unjudged)。

    - 政策 off（明确人类关闭）：不构造/不调用任何 provider、不读其
      分数缓存（零判断调用），coverage.judge=bypassed_by_user；
    - 政策 on：构造**所选** provider（get_provider_by_name）——Jev 与
      Codex 等实现同接口同材料，不串联接力；
    - 未配置（缺行/无 provider/未知名）：保留旧 S10 阻断语义
      （not_configured，正文不直出）——不是关闭。
    """
    from . import judge_policy
    policy = policy or judge_policy.effective()
    sid = session["session_id"]
    if policy["mode"] == "off":
        # 关闭模式：候选全集走冻结分页（_run_round_compute 组装），
        # 这里必须零 provider 构造/调用（J01：构造器/缓存/网络/进程
        # 计数为零）
        coverage["judge"] = "bypassed_by_user"
        return None, [], [], 0
    # on / unconfigured：走判断门（unconfigured 用 DisabledJudge 表达
    # not_configured，交付被 S10 硬门拦下）
    provider = judge_base.get_provider_by_name(
        policy.get("provider") if policy["mode"] == "on" else None)
    # Jev 精排（可替换；只评价被交付的候选）。
    # S10/WP01：words 与 raw-fallback 候选同样过一层判断——未判断
    # 候选不得出站（完整 mixed 各 20/cap40 与 RRF 合序送判归
    # WP02/WP04/WP05 精化）
    judge_result = None
    judge_items: list = []   # 对账后的可用判断项（P1-02 复审）
    judge_candidates = (event_fused + words_hits + raw_pre)[
        :config.RECALL_JUDGE_CANDIDATE_CAP]
    if isinstance(provider, judge_base.DisabledJudge):
        coverage["judge"] = "not_configured"
    else:
        ctx = {"session_id": sid, "revision": session["current_revision"],
               "policy_version": config.RECALL_POLICY_VERSION,
               "judge_policy_revision": policy.get("revision")}
        judge_result = provider.judge(plan, judge_candidates, ctx)
        coverage["judge"] = judge_result.provider_status
        coverage["judge_cache"] = {
            "hits": judge_result.cache_hits,
            "misses": judge_result.cache_misses,
            "requests": judge_result.request_count,
        }
        if judge_result.degraded_reason:
            degraded.append(f"judge_{judge_result.degraded_reason}")
        # 全量审计 P1-02 复审：provider 返回与送判集合严格对账——
        # 重复 ref / 陌生 ref / 漏返回都不许静默通过（可替换 provider
        # 送 N 回 1 曾被当成"全部判完"）。基数违例 → 整轮 judge 作废
        #（coverage=unavailable，gate judge_no_fault 拒 Round2）
        sent_refs = [c.get("candidate_ref") or c["resource_ref"]
                     for c in judge_candidates]
        item_refs = [i.candidate_ref for i in judge_result.items]
        if (len(set(item_refs)) != len(item_refs)
                or set(item_refs) - set(sent_refs)):
            coverage["judge"] = "unavailable"
            degraded.append("judge_cardinality_violation")
        else:
            judge_items = judge_result.items
            by_ref = {i.candidate_ref: i for i in judge_items}
            for c in judge_candidates:
                ji = by_ref.get(c.get("candidate_ref")
                                or c["resource_ref"])
                if ji:
                    c["judge"] = ji.to_dict()
                    c["candidate_ref"] = ji.candidate_ref
    # 全量审计 P1-02：unjudged 必须按 event+words+raw 全集算——此前
    # 只算 event（20 event + 40 words + cap40 时 20 个被截掉的 words
    # 凭空消失），错误 unjudged_count=0 进入 receipt 给 Round2 背书
    _judge_pool_total = len(event_fused) + len(words_hits) + len(raw_pre)
    unjudged = max(0, _judge_pool_total - len(judge_candidates))
    # CB-013：截断浏览窗口外的 words 不能凭空从 unjudged 消失——
    # has_more 时窗口外至少还有 1 条未送判（真实数量未知，保守计 1
    # 即足以让 unjudged_count>0，完整 R1 门禁不得为截断窗口背书）
    if coverage.get("words_lexical") == "partial_topk_window":
        unjudged += 1
    return judge_result, judge_candidates, judge_items, unjudged


def _run_round_compute(session: dict, plan: dict,
                       principal=None) -> tuple[dict, dict]:
    """一轮检索（纯计算）：两路召回 → RRF → 可选精排 → 代码门控 →
    证据包组装。不写运行库；持久化材料随 effects 由最终事务提交。

    session 可以是未落库的 draft（start）：session_id 仅作为内部关联
    ID，检索只依赖排除集（新 session 为空）与 formal 库只读事实。
    """
    sid = session["session_id"]
    from . import pipeline as _pl
    from . import judge_policy
    # 每次查询固定 policy_revision 与模式（§3.2）：续页/重放核对同一快照
    policy = judge_policy.effective()
    rejected = store.rejected_resource_refs(sid)
    coverage: dict = {"truncated": False}
    degraded: list[str] = []

    channels = plan.get("channels") or ["event"]
    with db.formal() as conn:
        event_fused: list[dict] = []
        words_hits: list[dict] = []
        if "event" in channels:
            raw_hits = _event_candidates(conn, plan, rejected, coverage,
                                         degraded)
            lexical = [h for h in raw_hits if "keyword" in h["matched_by"] or
                       "summary_keyword" in h["matched_by"]]
            dense = [h for h in raw_hits if "semantic" in h["matched_by"]]
            event_fused = fusion.rrf_fuse({
                "lexical": fusion.family_rank(lexical),
                "dense": fusion.family_rank(dense),
            })
            _attach_event_evidence(conn, event_fused)
        if "words" in channels:
            if not config.RECALL_WORDS_ENABLED:
                coverage["words_lexical"] = "blocked"
                degraded.append("words_channel_disabled")
            else:
                wres = words_mod.words_search(conn, plan,
                                              config.RECALL_LEXICAL_K)
                words_hits = wres["hits"]
                coverage["words_lexical"] = wres["coverage"]
                if wres.get("forgotten_words_count"):
                    coverage["words_forgotten"] = (
                        f"disabled:{config.WORDS_FORGOTTEN_RECALL}")
                # S08/WP05：words 专项 dense——拆出至
                # _words_dense_fuse（1005B 重构：逐字搬移零变更）
                words_hits = _words_dense_fuse(conn, plan,
                                               words_hits, coverage)

        # 闭环复审 P1-2：第一轮不查 raw（S12/S13）——原文升级只有
        # Round2 门禁一条通路；证据不足时提示 continuation 走
        # memory.recall.round2，不再有 words-fallback 旁路
        raw_result: dict | None = None
        if "words" in channels:
            coverage["raw"] = "round2_only"

    # Jev 精排（可关闭/可替换）——拆出至 _judge_pass（1005B 重构）
    raw_pre = raw_result["hits"] if raw_result else []
    judge_result, judge_candidates, judge_items, unjudged = _judge_pass(
        session, plan, event_fused, words_hits, raw_pre,
        coverage, degraded, policy=policy)

    # 候选合流：event RRF 序 + words 独立序（通道间不比较未校准原始分数）
    all_candidates = fusion.dedupe_by_resource(event_fused + words_hits +
                                               (raw_result["hits"] if
                                                raw_result else []))
    sel = selection.select(all_candidates, plan, rejected,
                           unjudged_count=unjudged,
                           judge_required=policy["judge_required"])
    if policy["mode"] == "off":
        # 关闭模式（§4.1）：冻结候选全集=合法去重后的本次检索全集
        #（不套 JUDGE_CAP/DELIVERY_LIMIT/多样性淘汰/分数排序——这些
        # 已由 selection(judge_required=False) 跳过）；正文载体换全量
        # 获授权投影（不 600/4000 截断），首页由冻结集装配分页。
        from . import paging as _paging
        for c in sel["delivered"]:
            _expand_full_text(c)
        frozen_cards = [_paging.project_card(c)
                        for c in sel["delivered"]]
        page_extra = {
            "judge_mode": "off",
            "judge_policy_revision": int(policy.get("revision") or 0),
            "judgement_status": "bypassed_by_user",
            "judged_count": 0,
            "retrieval_coverage": _scope_coverage(coverage),
        }
        first_page = _paging.assemble_page(frozen_cards, (0, 0),
                                           page_extra)
    else:
        frozen_cards = None
        first_page = None

    # 检索结果主状态（§12）
    if sel["delivered"]:
        search_status = ("AMBIGUOUS" if len(sel["delivered"]) > 1
                         else "FOUND")
    elif "words" in channels and coverage.get("words_lexical") == "blocked":
        search_status = "UNAVAILABLE"
    elif degraded and not all(
            d == "semantic_unavailable" for d in degraded):
        search_status = "DEGRADED"
    else:
        search_status = "NO_MATCH_OBSERVED"

    # commit-at-end：以下只组装，不写库。持久化材料随 effects 返回，
    # 由最终事务一次性提交（检索/Jev 失败时数据库零痕迹）。
    for c in all_candidates:
        c.setdefault("candidate_ref", c["resource_ref"])
    candidate_rows = [
        {"candidate_ref": c["candidate_ref"], "resource_ref": c["resource_ref"],
         "channel": c.get("channel", "event"),
         "representation": c.get("representation", ""),
         "content_version": c.get("content_version"),
         "representation_version": c.get("representation_version"),
         "state": "seen",
         "scores": {"rrf": c.get("rrf_score"),
                    "judge": c.get("judge", {}).get("relevance_signal")}}
        for c in all_candidates]
    if policy["mode"] == "off":
        # 回执只绑真实出站的首页条目（整卡与片段都以资源为单位一次）
        _page_refs: list[str] = []
        for e in first_page["candidates"]:
            ref = e.get("resource_ref")
            if ref and ref not in _page_refs:
                _page_refs.append(ref)
        receipts = [{"receipt_id": f"rc_{uuid.uuid4().hex[:10]}",
                     "resource_ref": ref,
                     "content_version": next(
                         (c.get("content_version")
                          for c in frozen_cards
                          if c.get("resource_ref") == ref), None),
                     "representation_version": next(
                         (c.get("representation_version")
                          for c in frozen_cards
                          if c.get("resource_ref") == ref), None),
                     "permission_version": "owner_binding_v1"}
                    for ref in _page_refs]
        by_receipt = {r["receipt_id"]: r["resource_ref"] for r in receipts}
        for c in frozen_cards:
            if c.get("resource_ref") in by_receipt.values():
                c["version_receipt"] = next(
                    (k for k, v in by_receipt.items()
                     if v == c["resource_ref"]), None)
    else:
        receipts = [{"receipt_id": f"rc_{uuid.uuid4().hex[:10]}",
                     "resource_ref": c["resource_ref"],
                     "content_version": c.get("content_version"),
                     "representation_version":
                         c.get("representation_version"),
                     "permission_version": "owner_binding_v1"}
                    for c in sel["delivered"]]
        by_receipt = {r["receipt_id"]: r["resource_ref"] for r in receipts}
        for c in sel["delivered"]:
            c["version_receipt"] = next(
                (k for k, v in by_receipt.items()
                 if v == c["resource_ref"]), None)

    status = state_machine.derive_status(search_status,
                                         len(sel["delivered"]),
                                         bool(sel["conflicts"]))
    # S10：判断不可用/未配置时正文不直出——交付为空必须是显式结构化
    # 状态，不是静默空结果（关闭模式无此抑制：bypassed 不等于故障）
    if (not sel["delivered"] and policy["judge_required"]
            and coverage.get("judge") in ("not_configured", "unavailable")):
        sel["missing"].append(
            "判断层不可用（not_configured/unavailable）：候选正文"
            "不直出（S10）；可在网页配置判断 provider 或经人工关闭后"
            "以全集分页交付")
    continuation = None
    if ("words" in channels and _words_evidence_insufficient(
            plan, words_hits or [])):
        continuation = {"available": True, "action": "round2_raw",
                        "via": "memory.recall.round2"}

    if policy["mode"] == "off":
        packet = {
            "recall_session_id": sid,
            "revision": session["current_revision"],
            "intent": plan.get("intent")
            or ("find_words" if (plan.get("channels") or []) == ["words"]
                else "recall_event"),
            "status": status,
            "search_status": search_status,
            "delivery_action": "needs_validation",
            "instruction_authority": "none",
            "content_role": "retrieved_memory",
            "coverage": coverage,
            "missing": sel["missing"],
            "conflicts": sel["conflicts"],
            "degraded_reasons": sorted(set(degraded)),
            "continuation": continuation,
            "budget": budget.snapshot(_with_round_preview(session)),
            "token_count": config.RECALL_TOKENIZER,
            # 关闭模式分页合同（§4.2/§4.3）：不经 _enforce_output_budget
            #（分页装配器自管 24576 字节预算，且不得 pop 候选）
            "candidates": first_page["candidates"],
            "judge_mode": "off",
            "judge_policy_revision": int(policy.get("revision") or 0),
            "judgement_status": "bypassed_by_user",
            "judged_count": 0,
            "retrieval_coverage": first_page["retrieval_coverage"],
            "pagination": first_page["pagination"],
        }
    else:
        packet = {
            "recall_session_id": sid,
            "revision": session["current_revision"],
            # 复审#5：重放按同 intent 重校验——专项 words 不套 event phase
            "intent": plan.get("intent")
            or ("find_words" if (plan.get("channels") or []) == ["words"]
                else "recall_event"),
            "status": status,
            "search_status": search_status,
            "delivery_action": sel["delivery_action"],
            "instruction_authority": "none",
            "content_role": "retrieved_memory",
            "coverage": coverage,
            "candidates": _finalize_cards(sel["delivered"]),
            "missing": sel["missing"],
            "conflicts": sel["conflicts"],
            "degraded_reasons": sorted(set(degraded)),
            "continuation": continuation,
            # 本轮尚未落库：预算快照按"含本轮成功"预览（rounds_used+1），
            # 最终事务成功后该预览即为事实
            "budget": budget.snapshot(_with_round_preview(session)),
            "token_count": config.RECALL_TOKENIZER,
            # 判断开启路径的合同字段（§4.2；有界交付即全部交付）
            "judge_mode": policy["mode"],
            "judge_policy_revision": int(policy.get("revision") or 0),
            "judgement_status": coverage.get("judge") or "not_configured",
            "judged_count": 0,  # 下方按 judge_stats 回填
            "retrieval_coverage": _scope_coverage(coverage),
            "pagination": {
                "result_set_id": None,
                "returned_count": len(sel["delivered"]),
                "candidate_total": len(all_candidates),
                "has_more": False,
                "next_cursor": None,
            },
        }
        packet = _enforce_output_budget(packet)
    # S13：judge 统计（Round1 回执依据——attempts 的 completed 不算）
    # CB-012（2026-10-02 审计 P1）：judged 必须是"有效判断"——
    # evaluated、ref 属于送判集、candidate_version 与卡实际版本一致
    # （双方非空时）。版本错配/重复/陌生 ref 的判断按 unavailable 计：
    # selection 的硬门会拦旧版本正文，但回执此前仍把它记作 judged，
    # 完成证明 fail-open 给 Round2 背书。
    _sent_by_ref = {c.get("candidate_ref") or c["resource_ref"]: c
                    for c in judge_candidates}

    def _valid_judge_item(i) -> bool:
        if i.evaluation_status != "evaluated":
            return False
        c = _sent_by_ref.get(i.candidate_ref)
        if c is None:
            return False
        jv, cv = i.candidate_version, c.get("content_version")
        if jv and cv and str(jv) != str(cv):
            return False
        # RA-003（2026-10-02 复审 P1）：非法分值（NaN/inf/bool/越界）
        # 不是有效判断——与 selection._valid_signal 同一口径，receipt
        # 不再为 NaN 结果签完成证明
        from ..retrieval.selection import _valid_signal
        if not _valid_signal(getattr(i, "relevance_signal", None)):
            return False
        return True

    judged_n = sum(1 for i in judge_items if _valid_judge_item(i))
    # P1-02 复审：unavailable = 送判数 − 判过数——provider 漏返回的
    # 候选在此入账（judged+unavailable 恒等于送判数，缺口不得凭空
    # 消失），receipt/gate 不再被"送 5 回 1"骗过
    unavailable_n = (max(0, len(judge_candidates) - judged_n)
                     if judge_result is not None else 0)
    if policy["mode"] != "off":
        packet["judged_count"] = judged_n
    effects = {
        "candidates": candidate_rows,
        "receipts": receipts,
        "session_status": status,
        "search_status": search_status,
        "judge_stats": {
            "judged": judged_n,
            "unavailable": unavailable_n,
            "unjudged": max(0, unjudged),
            "methods": {k: v for k, v in coverage.items()
                        if k in ("event", "dense_event", "words_lexical",
                                 "judge", "raw")},
        },
        # v1.7 A05：首轮真实执行完成（无论有无候选）即签发 ROUND1_COMPLETE
        # 回执——这是 Round 2 gate 的服务端事实；故障/降级轮不签发
        "mark_round1_complete": search_status not in (
            "UNAVAILABLE", "ERROR", "DEGRADED"),
        "delivery_action": sel["delivery_action"],
        "coverage": coverage,
        # 关闭模式：冻结结果集随最终事务持久化（policy 一并冻结；
        # start/refine 的提交事务调用 paging.freeze_set 落库）
        **({"page_set": {"cards": frozen_cards, "policy": policy}}
           if policy["mode"] == "off" else {}),
        # 闭环复审 P1-3：Round2 事实源——真实证据状态而非通用
        # needs_validation 标签（rank_only 下一切正常交付都是
        # needs_validation，不能当"证据不足"）
        "first_round_facts": {
            "delivered_count": len(sel["delivered"]),
            "requirement_met": bool(sel.get("requirement_met")),
            "conflicts_count": len(sel["conflicts"]),
            "missing_kinds": [m[:40] for m in sel["missing"]][:8],
            "evidence_requirement": plan.get("evidence_requirement"),
        },
    }
    return packet, effects


def _expand_full_text(card: dict) -> None:
    """关闭模式全量投影（§4.3）：事件正文换完整获授权文本。

    旧 600 字 excerpt 是判断开启路径的有界交付裁剪；关闭模式的候选
    全集分页要求"每条获授权内容全部可取得"——authored_event 证据的
    snippet 换成完整 whitelist_body，truncated=False。words 卡的
    word 文本本身即全文。legacy forgotten_summary 保持证据缺口语义
    （无正文可展开，LEGACY_CONTENT_GAP 不冒充）。
    """
    if card.get("channel") != "event":
        return
    row = card.get("_row") or {}
    body = row.get("whitelist_body")
    if not body or card.get("representation") == "forgotten_summary":
        return
    for ev in card.get("evidence") or []:
        if ev.get("evidence_kind") == "authored_event":
            ev["snippet"] = body
            ev["truncated"] = False
            return


def _scope_coverage(coverage: dict) -> dict:
    """retrieval_coverage 出站投影（§4.1）：如实披露穷尽性。

    只有所有实际执行通道都签 complete_within_scope 才标
    retrieval_scope_exhaustive=true；任何 Top-K/partial/unavailable
    都保持 false——"结果集翻完"不冒称"数据库穷尽"（P09）。
    """
    out = dict(coverage)
    statuses = [coverage[k] for k in ("event", "dense_event",
                                      "words_lexical", "words_dense")
                if k in coverage]
    out["retrieval_scope_exhaustive"] = bool(statuses) and all(
        s == "complete_within_scope" for s in statuses)
    return out




# F12：主体经参数传递为主；此全局仅作未知调用方的最后兜底，
# 不再是常规路径（并发串主体窗口已消除）
_CURRENT = {"principal": None}


def _current_principal():
    """检索层内 raw scope 解析用当前主体（invoke 时设置）。"""
    return _CURRENT["principal"]




def _record_op_in_tx(conn, op_ctx: dict | None, result: dict) -> None:
    """最终事务内写入 operation 完成记录（P1-01：业务副作用与
    operation 同事务，崩溃窗口内不再有"状态已变、operation 未记"）。"""
    if op_ctx:
        store.record_operation_row(
            conn, op_ctx["principal_id"], op_ctx["operation_key"],
            op_ctx["payload_hash"], result)






#: S16 输出预算（版本化工程参数）






def _persist_page_set_in_tx(conn, packet: dict, effects: dict,
                            session: dict, plan: dict) -> None:
    """off 模式最终事务内：冻结结果集落库 + 首页续页游标签发。

    必须先于 operation 行记录调用——保存的 operation 结果要包含最终
    result_set_id 与 next_cursor（同 key 重放回同一首页与同一游标）。
    """
    from . import paging as _paging
    ps = effects["page_set"]
    rsid = _paging.freeze_set(
        conn, session=session, plan=plan, policy=ps["policy"],
        cards=ps["cards"], coverage=effects["coverage"])
    packet["pagination"]["result_set_id"] = rsid
    _np = packet["pagination"].pop("next_position", None)
    if _np is not None:
        packet["pagination"]["next_cursor"] = _paging.get_or_issue_cursor(
            conn, rsid, tuple(_np))



def _commit_round_effects(conn, session_id: str, revision: int,
                          effects: dict, *, plan: dict | None = None,
                          scope: str = "", kind: str = "memory",
                          cref: str | None = None) -> str:
    """最终事务内的本轮派生行写入（candidates/receipts/状态/回执/
    Round1 成功回执——S13）。返回本轮签发的 continue_request_ref。"""
    store.upsert_candidates(conn, session_id, effects["candidates"],
                            revision)
    store.add_receipts(conn, session_id, effects["receipts"],
                       revision=revision)
    # RECALL-02 + RER-01（2026-10-04 复审）：接续引用随交付同事务
    # 签发（绑定本轮 revision）；cref 由调用方在记录 operation 结果
    # 之前铸造传入——保证同 operation 重放返回的响应包含同一个
    # 仍有效的接续引用，不新发 ref 也不重复授予预算
    if cref is None:
        import uuid as _uuid
        cref = f"cont_{_uuid.uuid4().hex[:12]}"
    store.issue_continue_ref(conn, session_id, cref, revision)
    if plan is not None:
        js = effects.get("judge_stats") or {}
        cov = dict(effects.get("coverage") or {})
        if effects.get("first_round_facts") is not None:
            cov["_first_round_facts"] = effects["first_round_facts"]
        store.record_round1_receipt(
            conn, session_id=session_id, revision=revision,
            plan_hash=canonical_hash(plan),
            scope_hash=hashlib.sha256(
                (session_id + ":" + (scope or "")).encode()).hexdigest(),
            policy_version=config.RECALL_POLICY_VERSION,
            round_kind=kind,
            methods=js.get("methods") or {},
            coverage=cov,
            candidate_set_hash=canonical_hash(
                [c["candidate_ref"] for c in effects["candidates"]]),
            judged_count=js.get("judged", 0),
            unavailable_count=js.get("unavailable", 0),
            unjudged_count=js.get("unjudged", 0),
            delivery_action=effects.get("delivery_action", ""),
            completed=bool(effects.get("mark_round1_complete")))
    cur = conn.execute(
        "UPDATE recall_sessions SET status=?, updated_at=?"
        " WHERE session_id=? AND current_revision=?",
        (effects["session_status"], _now_iso(), session_id, revision))
    if cur.rowcount != 1:
        from ..errors import Forbidden as _F
        raise _F("session revision 冲突（最终事务）",
                 code="REVISION_CONFLICT", session_id=session_id)
    if effects.get("mark_round1_complete"):
        from . import pipeline as _pl
        _pl.mark_round1_complete_tx(conn, session_id)


# ---------- 七个 session 动作 ----------
    return cref

def start(principal, a: dict, op_ctx: dict | None = None) -> dict:
    """commit-at-end：计算全部完成前不落任何正式状态。

    阶段 A 只读预检 → 阶段 B 内存 draft（session_id 不落库）→
    阶段 C 检索/Jev/组装（允许失败，失败即零痕迹返回）→
    阶段 D 单事务提交 session/round/candidates/receipts/status/operation。
    崩溃在阶段 C/D 之前 → 数据库无本 operation 任何痕迹，同 key 重试
    从头计算；最终事务由 SQLite 写锁串行化，并发输家回滚并重放赢家。
    """
    _require_enabled()
    plan = validate_query_plan(a.get("query_plan") or {})
    store.purge_expired()
    draft = store.new_session_draft(
        principal.principal_id, a.get("conversation_scope") or "", plan)
    budget.require_round_available(draft)
    if op_ctx:
        store.check_operation_conflict(op_ctx["principal_id"],
                                       op_ctx["operation_key"],
                                       op_ctx["payload_hash"])
    packet, effects = _run_round_compute(draft, plan, principal)
    packet["created"] = True
    packet["query_fingerprint"] = _query_fp(plan)
    sid = draft["session_id"]
    op_key = op_ctx["operation_key"] if op_ctx else None
    # RER-01：先铸造接续引用并入 packet，operation 结果与首次响应
    # 携带同一个 ref（重放不丢、不新发）
    import uuid as _u_start
    _cref = f"cont_{_u_start.uuid4().hex[:12]}"
    # F-J-27（联合审计 2026-10-06）：合并而非覆盖——words 证据不足的
    # round2_raw 升级提示（RER-01）与新接续引用并存；保留提示不自动
    # 授予 Round2 权限或额度（gate 八条件仍逐次校验）
    _prev_cont = packet.get("continuation") or {}
    packet["continuation"] = {**_prev_cont,
                              "continue_request_ref": _cref,
                              "for_revision": 1}
    # P3（2026-10-05 审计）：continuation 在预算执行后注入——补一次
    # 终检，存储的 operation 回执与首次响应同一份受控包。
    # 关闭模式例外（§4.3）：分页装配器自管字节预算，不经旧裁剪器
    #（旧 600/4000/pop 路径会把全集候选直接丢掉）
    if packet.get("judge_mode") != "off":
        packet = _enforce_output_budget(packet)
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if effects.get("page_set"):
                _persist_page_set_in_tx(conn, packet, effects, draft,
                                        plan)
            if op_ctx:
                store.record_operation_row(
                    conn, op_ctx["principal_id"], op_ctx["operation_key"],
                    op_ctx["payload_hash"], packet)
            store.insert_session(conn, draft)
            budget.ensure_round_available_conn(conn, sid, draft)
            # S17：logical kind——纯 words 专项轮记 words
            r1_kind = ("words"
                       if (plan.get("channels") or []) == ["words"]
                       else "memory")
            store.record_round(conn, sid, burst_no=1,
                               operation_key=op_key, kind=r1_kind)
            _commit_round_effects(conn, sid, 1, effects, plan=plan,
                                  scope=draft["conversation_scope"],
                                  kind=r1_kind, cref=_cref)
            store.record_attempt(conn, sid, op_key or _op_id("round"), 1, 1,
                                 "retrieve", "completed")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return packet


def refine(principal, a: dict, op_ctx: dict | None = None) -> dict:
    """refine 同为 commit-at-end：修订/新 burst/检索在事务外完成，
    revision 前进（CAS）、round、candidates、receipts、operation 在
    最终事务内一次性提交。并发 refine 输家在写锁内 CAS 失败。"""
    _require_enabled()
    session = store.require_session(a["session_id"])
    session = store.expire_if_due(session)
    state_machine.require_action(session, "refine")
    require_owned_session(principal, session, a)
    expected_revision = int(a.get("expected_revision") or
                            session["current_revision"])
    if expected_revision != session["current_revision"]:
        raise Forbidden("expected_revision 过期（并发修改）；请 status 重读",
                        code="REVISION_CONFLICT",
                        current_revision=session["current_revision"])
    plan = validate_query_plan(a.get("query_plan") or {})
    burst_no = session["current_burst"]
    cont_ref = a.get("continue_request_ref")
    bursts_used = None
    if cont_ref:
        grant = budget.try_new_burst(session, cont_ref)
        burst_no = grant["current_burst"]
        bursts_used = grant["bursts_used"]
    if op_ctx:
        store.check_operation_conflict(op_ctx["principal_id"],
                                       op_ctx["operation_key"],
                                       op_ctx["payload_hash"])
    if budget.rounds_left_in_burst(_virtual(session, expected_revision,
                                            burst_no, bursts_used)) > 0:
        vsession = _virtual(session, expected_revision, burst_no,
                            bursts_used)
        packet, effects = _run_round_compute(vsession, plan, principal)
        packet["query_fingerprint"] = _query_fp(plan)
        sid = a["session_id"]
        op_key = op_ctx["operation_key"] if op_ctx else None
        # RER-01：接续引用先入 packet 再记录 operation 结果
        import uuid as _u_refine
        _cref = f"cont_{_u_refine.uuid4().hex[:12]}"
        # F-J-27：同 start——升级提示与接续引用合并（不整体覆盖）
        _prev_cont = packet.get("continuation") or {}
        packet["continuation"] = {**_prev_cont,
                                  "continue_request_ref": _cref,
                                  "for_revision": expected_revision + 1}
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # CB-009：提交时重验——计算期间被 close/refine 变更
                # 则整体回滚，不复活终态
                _revalidate_session_in_tx(conn, sid, expected_revision,
                                          "refine")
                if cont_ref:
                    # 裁定（2026-10-04）：线性接续——burst 授予与
                    # continue_ref 消费同事务；旧 ref 重发在此 stale
                    store.consume_continue_ref(conn, sid, cont_ref,
                                               expected_revision)
                if effects.get("page_set"):
                    _persist_page_set_in_tx(conn, packet, effects,
                                            vsession, plan)
                if op_ctx:
                    store.record_operation_row(
                        conn, op_ctx["principal_id"], op_ctx["operation_key"],
                        op_ctx["payload_hash"], packet)
                store.advance_revision(
                    conn, sid, expected_revision, query_plan=plan,
                    request_ref=plan.get("request_ref"),
                    change_reason="refine", burst_no=burst_no,
                    bursts_used=bursts_used)
                budget.ensure_round_available_conn(conn, sid, vsession)
                store.record_round(conn, sid, burst_no=burst_no,
                                   operation_key=op_key, kind="memory")
                _commit_round_effects(
                    conn, sid, expected_revision + 1, effects, plan=plan,
                    scope=session["conversation_scope"], kind="memory",
                    cref=_cref)
                store.record_attempt(conn, sid, op_key or _op_id("round"),
                                     expected_revision + 1, burst_no,
                                     "retrieve", "completed")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return packet
    # burst 轮次已尽且无显式继续请求：允许修订条件（revision 前进、
    # 保存新查询计划），但不发起有成本的新检索（v1.4 §9.3——自动
    # 自循环不产生无界额度）。设为型写入，独立小事务即可。
    session["current_revision"] = expected_revision + 1
    out_packet = {
        "recall_session_id": a["session_id"],
        "revision": expected_revision + 1,
        "status": "BUDGET_EXHAUSTED",
        "search_status": "BUDGET_EXHAUSTED",
        "delivery_action": "no_candidates",
        "instruction_authority": "none",
        "content_role": "retrieved_memory",
        "coverage": {"truncated": False},
        "candidates": [],
        "missing": ["当前 burst 轮次已尽；继续检索需要显式"
                    " continue_request_ref 申请新 burst"],
        "conflicts": [],
        "degraded_reasons": ["budget_exhausted_no_new_burst"],
        "continuation": None,
        "budget": budget.snapshot(session),
        "token_count": config.RECALL_TOKENIZER,
        "query_fingerprint": _query_fp(plan),
    }
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _revalidate_session_in_tx(conn, a["session_id"],
                                      expected_revision, "refine")
            store.advance_revision(conn, a["session_id"], expected_revision,
                                   query_plan=plan,
                                   request_ref=plan.get("request_ref"),
                                   change_reason="refine",
                                   burst_no=burst_no,
                                   bursts_used=bursts_used)
            conn.execute(
                "UPDATE recall_sessions SET status='BUDGET_EXHAUSTED',"
                " updated_at=? WHERE session_id=? AND current_revision=?",
                (_now_iso(), a["session_id"], expected_revision + 1))
            # RECALL-02：BUDGET_EXHAUSTED 也是一次已交付 revision——
            # 同事务签发接续引用（无候选包同样可续），head 绑定一致
            import uuid as _u2
            _cref = f"cont_{_u2.uuid4().hex[:12]}"
            store.issue_continue_ref(conn, a["session_id"], _cref,
                                     expected_revision + 1)
            out_packet["continuation"] = {
                "continue_request_ref": _cref,
                "for_revision": expected_revision + 1,
            }
            _record_op_in_tx(conn, op_ctx, out_packet)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return out_packet



def reject(principal, a: dict, op_ctx: dict | None = None) -> dict:
    _require_enabled()
    session = store.require_session(a["session_id"])
    session = store.expire_if_due(session)
    state_machine.require_action(session, "reject")
    require_owned_session(principal, session, a)
    target = a.get("reject_target", "candidate")
    candidate_ref = a.get("candidate_ref")
    if not candidate_ref and a.get("resource_ref"):
        # 只给了 resource_ref：反查 session 内该资源的候选引用
        for c in store.list_candidates(a["session_id"]):
            if c["resource_ref"] == a["resource_ref"]:
                candidate_ref = c["candidate_ref"]
                break
    if not candidate_ref:
        raise NotFound("candidate not found in session",
                       resource_ref=a.get("resource_ref"))
    from .models import REJECT_TARGETS
    if target not in REJECT_TARGETS:
        raise Forbidden(f"reject_target 必须是 {list(REJECT_TARGETS)}",
                        code="INVALID_ARGUMENT")
    result = {"candidate_ref": candidate_ref,
              "resource_ref": a.get("resource_ref") or "",
              "reject_target": target, "scope": "session_local"}
    out = {"rejected": result, "session_id": a["session_id"],
           "revision": session["current_revision"]}
    # P1-01：候选状态与 operation 记录同一事务（设为型副作用 +
    # 幂等收据原子提交，崩溃窗口不再产生"已拒但无收据"）
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # F03：写锁内复查——终态 session 不再接受 reject 写入
            fresh = store.require_session_in_tx(conn, a["session_id"])
            state_machine.require_action(fresh, "reject")
            require_owned_session(principal, fresh, a)
            store.set_candidate_state_tx(
                conn, a["session_id"], candidate_ref, "rejected",
                reject_target=target)
            if target in ("event", "candidate") and a.get("resource_ref"):
                # 事件级排除：同资源全部重复表示一并标记（写锁内读）
                for c in store.list_candidates(a["session_id"], conn=conn):
                    if (c["resource_ref"] == a["resource_ref"] and
                            c["state"] != "rejected"):
                        store.set_candidate_state_tx(
                            conn, a["session_id"], c["candidate_ref"],
                            "rejected", reject_target=target)
            _record_op_in_tx(conn, op_ctx, out)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return out


def accept(principal, a: dict, op_ctx: dict | None = None) -> dict:
    _require_enabled()
    session = store.require_session(a["session_id"])
    session = store.expire_if_due(session)
    state_machine.require_action(session, "accept")
    require_owned_session(principal, session, a)
    out = {"accepted": a.get("candidate_ref"), "status": session["status"]}
    # P1-01：候选状态 / 终态迁移 / operation 记录同一事务
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # F03（2026-10-03 审计 P1）：允许检查在锁外，close 不推进
            # revision——最终写锁内必须重读行重跑状态机+归属，
            # 否则本事务会把并发 close 的 CANCELLED 覆盖成 RESOLVED
            fresh = store.require_session_in_tx(conn, a["session_id"])
            state_machine.require_action(fresh, "accept")
            require_owned_session(principal, fresh, a)
            if a.get("candidate_ref"):
                store.set_candidate_state_tx(
                    conn, a["session_id"], a["candidate_ref"], "accepted")
            if a.get("close"):
                store.update_status_tx(conn, a["session_id"],
                                       fresh["current_revision"],
                                       "RESOLVED")
                out["status"] = "RESOLVED"
            _record_op_in_tx(conn, op_ctx, out)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return out


def navigate(principal, a: dict, op_ctx: dict | None = None) -> dict:
    """沿当前 temporal_axis 取前/后相邻候选（v1.3 §5.3/§6）。"""
    _require_enabled()
    session = store.require_session(a["session_id"])
    session = store.expire_if_due(session)
    state_machine.require_action(session, "navigate")
    require_owned_session(principal, session, a)
    direction = a.get("direction")
    if direction not in ("earlier", "later"):
        raise Forbidden("direction 必须是 earlier/later",
                        code="INVALID_ARGUMENT")
    plan = store.get_plan(a["session_id"]) or {}
    axis = plan.get("temporal_axis", "event_time")
    rejected = store.rejected_resource_refs(a["session_id"])
    anchor = a.get("anchor_candidate_ref") or _latest_anchor(a["session_id"])
    with db.formal() as conn:
        axis_field = {"event_time": "m.memory_date",
                      "hold_time": "m.created_at"}.get(axis)
        if axis_field is None:
            raise Forbidden(
                f"时间轴 {axis} 当前不可用于事件导航；资源日期缺失返回"
                "unknown，不用入库时间冒充（RUNTIME-05）",
                code="AXIS_UNAVAILABLE", axis=axis)
        op = "<" if direction == "earlier" else ">"
        order = "DESC" if direction == "earlier" else "ASC"
        # CB-041（2026-10-02 审计 P2）：导航复用当前 QueryPlan 的
        # AllowedScope——此前 WHERE 只有 visibility，明确分类/日期
        # 条件被丢弃（sweet 范围导航返回 daily 桶）
        from ..retrieval import query_plan as _qp
        where_t, params_t, _rej, _plan = _qp.AllowedScope.for_plan(
            plan, rejected)
        where = list(where_t)
        params: list = list(params_t)
        if anchor:
            # CB-041：公开 candidate_ref 是 memory:<id> 形态——先解析
            # 前缀并验证属于本 session，再作锚（此前原样当主键查，
            # 锚静默失效）
            anchor_id = str(anchor)
            if anchor_id.startswith("memory:"):
                anchor_id = anchor_id[len("memory:"):]
            owned = store.list_candidates(a["session_id"])
            known = {c["candidate_ref"] for c in owned} | \
                {c["resource_ref"] for c in owned}
            if anchor not in known:
                raise Forbidden(
                    "anchor_candidate_ref 不属于本 session 的候选",
                    code="INVALID_ARGUMENT", anchor=anchor)
            row = conn.execute(
                "SELECT memory_date, created_at FROM memories m"
                " JOIN retrieval_documents rd ON rd.memory_id=m.memory_id"
                " WHERE m.memory_id=?", (anchor_id,)).fetchone()
            if row:
                key = (row["memory_date"] if axis == "event_time"
                       else row["created_at"])
                where.append(f"({axis_field} {op} ? OR"
                             f" ({axis_field} = ? AND m.memory_id <> ?))")
                params += [key, key, anchor_id]
        # CB-041：NOT IN 占位符与绑定等长——先过滤出 memory ref 再
        # 生成 marks（此前按全部 rejected 生成占位符、只绑定 memory
        # 项，words reject 后直接 SQLite ProgrammingError）
        rejected_mem = [r[len("memory:"):] for r in rejected
                        if r.startswith("memory:")]
        if rejected_mem:
            marks = ",".join("?" * len(rejected_mem))
            where.append(f"m.memory_id NOT IN ({marks})")
            params += rejected_mem
        rows = conn.execute(
            "SELECT m.memory_id, m.memory_date, m.compression_state,"
            " m.current_version_no, rd.projection_kind FROM memories m"
            " JOIN retrieval_documents rd ON rd.memory_id = m.memory_id"
            f" WHERE {' AND '.join(where)}"
            f" ORDER BY {axis_field} {order}, m.memory_id LIMIT ?",
            params + [config.RECALL_DELIVERY_LIMIT]).fetchall()
        cards = []
        for r in rows:
            ref = f"memory:{r['memory_id']}"
            cards.append({
                "resource_ref": ref,
                "candidate_ref": ref,
                "channel": "event",
                "representation": r["projection_kind"],
                "content_version": str(r["current_version_no"]),
                "representation_version": str(r["current_version_no"]),
                "projection_version": config.PROJECTION_REVISION,
                "memory_date": r["memory_date"],
                "matched_by": ["temporal_navigate"],
                "matched_fields": [axis],
                "evidence": [evidence_mod.make_evidence(
                    "structured_fact", axis, "", ref,
                    structured_value={axis: r["memory_date"]})],
            })
    out = {"recall_session_id": a["session_id"],
          "direction": direction, "axis": axis,
          "candidates": cards, "status": session["status"],
          "instruction_authority": "none"}
    # 幂等 upsert（设为型）与 operation 记录同一事务（P1-01）
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # F03（2026-10-05 审计补齐）：写锁内复查——与 reject/
            # accept/close/refine 同款，终态/过期 session 不再接收
            # navigate 的候选写入（此前预检后可被并发关闭，仍写入
            # 带过期 revision 的 seen 行）
            fresh = store.require_session_in_tx(conn, a["session_id"])
            state_machine.require_action(fresh, "navigate")
            require_owned_session(principal, fresh, a)
            for c in cards:
                store.upsert_candidates(conn, a["session_id"], [{
                    "candidate_ref": c["candidate_ref"],
                    "resource_ref": c["resource_ref"],
                    "channel": "event", "representation": c["representation"],
                    "content_version": c["content_version"],
                    "representation_version": c["representation_version"],
                    "state": "seen", "scores": {}}],
                    fresh["current_revision"])
            # RA-015（2026-10-02 复审 P2）：导航包绑定查询指纹——
            # 必须在 operation 记录前注入（存的 result 带指纹），同
            # key 重试不再被指纹校验误判 stale
            out["query_fingerprint"] = _query_fp(plan)
            _record_op_in_tx(conn, op_ctx, out)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return out


def _latest_anchor(session_id: str) -> str | None:
    """RA-015（2026-10-02 复审 P2）：默认锚取本 session 最近的**真实
    memory 候选**——此前取最后一条回执，round1:complete 等证明类伪
    资源会得到 None，默认导航退化为同 scope 浏览。"""
    cands = store.list_candidates(session_id)
    mem_refs = [c["resource_ref"] for c in cands
                if isinstance(c.get("resource_ref"), str)
                and c["resource_ref"].startswith("memory:")
                and c.get("state") != "rejected"]
    if not mem_refs:
        return None
    # RA-015 修正：'最近' 按事件时间取（candidates 插入序随 judge
    # 分数随机，reversed 首条不是最新——flaky 反例：08-10 被当锚导致
    # earlier 空）
    with db.formal() as conn:
        marks = ",".join("?" * len(mem_refs))
        row = conn.execute(
            f"SELECT memory_id FROM memories WHERE memory_id IN ({marks})"
            " AND memory_date IS NOT NULL"
            " ORDER BY memory_date DESC, created_at DESC LIMIT 1",
            [r[len("memory:"):] for r in mem_refs]).fetchone()
    if row is None:
        return mem_refs[-1]
    return f"memory:{row['memory_id']}"


def status(principal, a: dict) -> dict:
    _require_enabled()
    session = store.require_session(a["session_id"])
    session = store.expire_if_due(session)
    state_machine.require_action(session, "status")
    require_owned_session(principal, session, a)
    reval = revalidate_receipts(a["session_id"])
    if reval["invalid_refs"]:
        session = store.require_session(a["session_id"])  # 重校验后重读
    plan = store.get_plan(a["session_id"])
    return {
        "session": {k: session[k] for k in (
            "session_id", "status", "current_revision", "current_burst",
            "rounds_used", "bursts_used", "expires_at")},
        "query_plan": {k: plan.get(k) for k in (
            "original_request", "channels", "temporal_axis",
            "evidence_requirement")} if plan else None,
        "candidates": [{"candidate_ref": c["candidate_ref"],
                        "resource_ref": c["resource_ref"],
                        "state": c["state"]}
                       for c in store.list_candidates(a["session_id"])],
        "receipts_revalidated": reval,
        "budget": budget.snapshot(session),
    }


def close(principal, a: dict, op_ctx: dict | None = None) -> dict:
    _require_enabled()
    session = store.require_session(a["session_id"])
    session = store.expire_if_due(session)
    state_machine.require_action(session, "close")
    require_owned_session(principal, session, a)
    outcome = a.get("outcome", "cancelled")
    if outcome not in ("resolved", "cancelled"):
        raise Forbidden("outcome 必须是 resolved/cancelled",
                        code="INVALID_ARGUMENT")
    final = "RESOLVED" if outcome == "resolved" else "CANCELLED"
    out = {"session_id": a["session_id"], "status": final}
    # P1-01：终态迁移与 operation 记录同一事务——首次成功后崩溃，
    # 同 key 重试重放首次结果，不再 INVALID_STATE
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # F03：写锁内复查——两个并发终态动作只留先提交者，
            # 后到者按当前终态被状态机拒绝（不覆盖）
            fresh = store.require_session_in_tx(conn, a["session_id"])
            state_machine.require_action(fresh, "close")
            require_owned_session(principal, fresh, a)
            store.update_status_tx(conn, a["session_id"],
                                   fresh["current_revision"], final)
            _record_op_in_tx(conn, op_ctx, out)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return out


# ---------- 读时重校验（RUNTIME-04 / §9.4） ----------

def revalidate_receipts(session_id: str) -> dict:
    """正文出站前重检权限与版本：引用与当前库比对，不重放旧正文。"""
    invalid: list[str] = []
    checked = 0
    with db.formal() as conn:
        for r in store.list_receipts(session_id):
            ref = r["resource_ref"]
            if ref.startswith("memory:"):
                mid = ref[len("memory:"):]
                row = conn.execute(
                    "SELECT visibility, compression_state,"
                    " current_version_no FROM memories WHERE memory_id=?",
                    (mid,)).fetchone()
                checked += 1
                if (row is None or row["visibility"] != "active" or
                        str(row["current_version_no"]) !=
                        (r.get("content_version") or "")):
                    invalid.append(ref)
            elif ref.startswith("our_word:"):
                wid = ref[len("our_word:"):]
                row = conn.execute(
                    "SELECT m.visibility, m.compression_state"
                    " FROM memory_our_words w JOIN memories m"
                    " ON m.memory_id = w.memory_id WHERE w.word_id=?",
                    (wid,)).fetchone()
                checked += 1
                if row is None or row["visibility"] != "active" or \
                        row["compression_state"] != "full":
                    invalid.append(ref)
            elif ref.startswith("raw_msg:"):
                # F-J-05（联合审计 2026-10-06）：raw_messages 已随 Legacy Raw 退役
                # （迁移 25 DROP）——legacy raw_msg: 前缀读侧一律不可解析=invalid
                # （CURRENT §6 gap 语义），不再查询已不存在的表（此前 500）
                checked += 1
                invalid.append(ref)
    result = {"checked": checked, "invalid_refs": invalid}
    if invalid:
        session = store.get_session(session_id)
        if session and session["status"] in ("ACTIVE", "AMBIGUOUS",
                                             "CONFLICT", "DEGRADED"):
            store.update_status(session_id, session["current_revision"],
                                "STALE_RETRY_REQUIRED")
    return result


def validate_context(principal, a: dict) -> dict:
    """memory.context.validate：对装配上下文的资源引用+版本重查（§18）。"""
    refs = a.get("resource_refs") or []
    out = []
    with db.formal() as conn:
        for ref in refs:
            entry = {"resource_ref": ref, "valid": False,
                     "current_version": None}
            if ref.startswith("memory:"):
                row = conn.execute(
                    "SELECT visibility, current_version_no FROM memories"
                    " WHERE memory_id=?", (ref[len("memory:"):],)).fetchone()
                if row and row["visibility"] == "active":
                    entry["valid"] = True
                    entry["current_version"] = str(row["current_version_no"])
            elif ref.startswith("our_word:"):
                row = conn.execute(
                    "SELECT m.visibility, m.compression_state,"
                    " m.current_version_no FROM memory_our_words w"
                    " JOIN memories m ON m.memory_id=w.memory_id"
                    " WHERE w.word_id=?", (ref[len("our_word:"):],)).fetchone()
                if row and row["visibility"] == "active" and \
                        row["compression_state"] == "full":
                    entry["valid"] = True
                    entry["current_version"] = str(row["current_version_no"])
            out.append(entry)
    return {"validated": out,
            "instruction_authority": "none"}




def _check_conversation_scope(session: dict, a: dict) -> None:
    """跨 conversation 的 session 引用不自动泄漏隐藏窗口上下文（RUNTIME-09）。"""
    # session 未声明 scope 时，显式携带其他 scope 的调用同样拒绝
    # （保守：不做"空=全局可见"的解释）。
    scope = a.get("conversation_scope")
    if scope and scope != session["conversation_scope"]:
        raise Forbidden(
            "conversation_scope 与 session 绑定范围不一致；同主体不自动"
            "获得另一会话的隐藏上下文", code="SCOPE_MISMATCH",
            session_id=session["session_id"])


def words_recall(principal, a: dict, op_ctx: dict | None = None) -> dict:
    """memory.words.recall：words 专项统一入口（S08/WP05）。

    内部复用 session 化主链（短期 session：预算、回执、同一层 Jev
    出口、≤3 交付全走 start 机制），不再直返 20-30 条正文，也不另写
    一套 pipeline。
    """
    if not config.RECALL_WORDS_ENABLED:
        raise Forbidden("words 通道未启用（MARIPOSA_WORDS_RECALL_ENABLED）",
                        code="WORDS_CHANNEL_DISABLED")
    plan = validate_query_plan({
        "original_request": a.get("query") or a.get("original_request", ""),
        "channels": ["words"],
        "semantic_query": a.get("semantic_query") or "",
        "lexical_terms": a.get("lexical_terms") or
        ([a["query"]] if a.get("query") else []),
        "exact_phrases": a.get("exact_phrases") or [],
        "explicit_constraints": a.get("explicit_constraints") or {},
        "explicit_negative_constraints":
            a.get("explicit_negative_constraints") or {},
    })
    return start(principal, {"query_plan": plan}, op_ctx=op_ctx)


# ---------- operation 幂等重放重校验（审计 F07） ----------


# ---------- 拆分批（2026-10-04）：重放校验与 Round2 外迁，重导出兼容 ----------
from .replay import _norm_recall_field, revalidate_replayed  # noqa: E402,F401
from .round2 import (_acquire_raw_lease, _reason_fact_supported,  # noqa: E402,F401
                     _release_raw_lease, round2)
