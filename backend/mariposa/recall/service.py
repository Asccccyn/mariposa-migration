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
from .models import validate_query_plan

_SEARCH_STATUSES = ("FOUND", "AMBIGUOUS", "CONFLICT", "NO_MATCH_OBSERVED",
                    "DEGRADED", "BUDGET_EXHAUSTED", "STALE_RETRY_REQUIRED",
                    "UNAVAILABLE")


def _require_enabled() -> None:
    if not config.RECALL_RUNTIME_ENABLED:
        raise Forbidden(
            "召回运行时未启用（MARIPOSA_RECALL_ENABLED）；能力已注册但"
            "当前 blocked，完成隔离验收后分项启用",
            code="RECALL_RUNTIME_DISABLED")


def _op_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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


def _run_round_compute(session: dict, plan: dict,
                       principal=None) -> tuple[dict, dict]:
    """一轮检索（纯计算）：两路召回 → RRF → 可选精排 → 代码门控 →
    证据包组装。不写运行库；持久化材料随 effects 由最终事务提交。

    session 可以是未落库的 draft（start）：session_id 仅作为内部关联
    ID，检索只依赖排除集（新 session 为空）与 formal 库只读事实。
    """
    sid = session["session_id"]
    from . import pipeline as _pl
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
                # S08/WP05：words 专项 dense（独立向量空间，独立阈值）
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

        # 闭环复审 P1-2：第一轮不查 raw（S12/S13）——原文升级只有
        # Round2 门禁一条通路；证据不足时提示 continuation 走
        # memory.recall.round2，不再有 words-fallback 旁路
        raw_result: dict | None = None
        if "words" in channels:
            coverage["raw"] = "round2_only"

    # Jev 精排（可关闭/可替换；只评价被交付的候选）。
    # S10/WP01：words 与 raw-fallback 候选同样过一层 Jev——未判断
    # 候选不得出站（完整 mixed 各 20/cap40 与 RRF 合序送判归
    # WP02/WP04/WP05 精化）
    judge_result = None
    judge_items: list = []   # 对账后的可用判断项（P1-02 复审）
    raw_pre = raw_result["hits"] if raw_result else []
    judge_candidates = (event_fused + words_hits + raw_pre)[
        :config.RECALL_JUDGE_CANDIDATE_CAP]
    provider = judge_base.get_provider()
    if isinstance(provider, judge_base.DisabledJudge):
        coverage["judge"] = "not_configured"
    else:
        ctx = {"session_id": sid, "revision": session["current_revision"],
               "policy_version": config.RECALL_POLICY_VERSION}
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

    # 候选合流：event RRF 序 + words 独立序（通道间不比较未校准原始分数）
    all_candidates = fusion.dedupe_by_resource(event_fused + words_hits +
                                               (raw_result["hits"] if
                                                raw_result else []))
    sel = selection.select(all_candidates, plan, rejected,
                           unjudged_count=unjudged)

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
    receipts = [{"receipt_id": f"rc_{uuid.uuid4().hex[:10]}",
                 "resource_ref": c["resource_ref"],
                 "content_version": c.get("content_version"),
                 "representation_version": c.get("representation_version"),
                 "permission_version": "owner_binding_v1"}
                for c in sel["delivered"]]
    by_receipt = {r["receipt_id"]: r["resource_ref"] for r in receipts}
    for c in sel["delivered"]:
        c["version_receipt"] = next(
            (k for k, v in by_receipt.items() if v == c["resource_ref"]), None)

    status = state_machine.derive_status(search_status,
                                         len(sel["delivered"]),
                                         bool(sel["conflicts"]))
    # S10：Jev 不可用/未配置时正文不直出——交付为空必须是显式结构化
    # 状态，不是静默空结果
    if not sel["delivered"] and coverage.get("judge") in (
            "not_configured", "unavailable"):
        sel["missing"].append(
            "Jev 判断不可用（not_configured/unavailable）：候选正文"
            "不直出（S10）；可配置 judge 后重试")
    continuation = None
    if ("words" in channels and _words_evidence_insufficient(
            plan, words_hits or [])):
        continuation = {"available": True, "action": "round2_raw",
                        "via": "memory.recall.round2"}

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


def _finalize_cards(cards: list[dict]) -> list[dict]:
    out = []
    for c in cards:
        card = {k: v for k, v in c.items()
                if k in ("resource_ref", "candidate_ref", "channel",
                         "representation", "content_version",
                         "representation_version", "projection_version",
                         "memory_id", "word_id", "ordinal", "speaker",
                         "expression_kind", "memory_date", "matched_by",
                         "matched_fields", "excerpt", "truncated", "evidence",
                         "judge", "version_receipt", "rrf_score",
                         "_matched")}  # 复审#1：检索层真实命中窗
        card["evidence_requirement_met"] = evidence_mod.meets_requirement(
            card.get("evidence") or [], "verbatim_required")
        out.append(card)
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


def _query_fp(plan: dict) -> str:
    """CB-014：packet 落库时的查询计划指纹——重放时与当前 plan 比对，
    查询已修订（refine）则旧包拒绝重放，不把旧查询候选重标新 revision。"""
    from ..memory.service import canonical_hash
    return canonical_hash(plan or {})


def _revalidate_session_in_tx(conn, sid: str, expected_revision: int,
                              action: str) -> None:
    """CB-009（2026-10-02 审计 P1）：commit-at-end 最终事务内统一重验
    session 当前状态与 revision。

    计算阶段在写锁外完成——close/并发 refine/expire 可发生在计算合法
    之后、提交之前；close 不推进 revision，仅靠 revision CAS 挡不住
    终态复活（refine 把 CANCELLED 拉回 ACTIVE、Round2 把旧 revision
    的 raw 结果挂上已前进的 session）。提交前在写锁内重读当前行，
    状态族或 revision 任一失配即整体回滚。
    """
    from ..errors import Forbidden as _F
    row = conn.execute(
        "SELECT status, current_revision FROM recall_sessions"
        " WHERE session_id=?", (sid,)).fetchone()
    if row is None:
        raise _F("提交时 session 已不存在", code="SESSION_STATE_CHANGED",
                 session_id=sid)
    allowed = state_machine.ACTION_PRECONDITIONS.get(action)
    if allowed is None or row["status"] not in allowed:
        raise _F(
            f"提交时 session 状态已变为 {row['status']}（计算期间被"
            "并发变更）", code="SESSION_STATE_CHANGED", session_id=sid,
            status=row["status"])
    if int(row["current_revision"]) != int(expected_revision):
        raise _F("提交时 session revision 已前进（计算期间被并发变更）",
                 code="REVISION_CONFLICT", session_id=sid,
                 expected_revision=expected_revision)


#: S16 输出预算（版本化工程参数）
OUTPUT_MAX_JSON_BYTES = 24576
OUTPUT_MAX_BODY_CHARS = 4000
OUTPUT_MAX_PER_CANDIDATE_CHARS = 600


def _enforce_output_budget(packet: dict) -> dict:
    """S16：输出预算实际计算——完整 packet 的 UTF-8 序列化 ≤24576
    字节、候选正文合计 ≤4000 字符、卡数 ≤3、单候选文本 ≤600——
    超限时按序裁剪证据片段并显式标 truncated/budget_truncated，
    不是只依赖 config 常量。"""
    import json as _json

    def _carriers(c: dict) -> list:
        """CB-042：候选上所有正文载体——evidence snippets、excerpt、
        _matched 字段值（此前只裁 evidence，9997 字符的 _matched 原样
        出站）。返回 [(holder_dict, key)] 供统一裁剪。"""
        slots = []
        for ev in c.get("evidence") or []:
            slots.append((ev, "snippet"))
        if isinstance(c.get("excerpt"), str):
            slots.append((c, "excerpt"))
        m = c.get("_matched")
        if isinstance(m, dict):
            for k, v in m.items():
                if isinstance(v, str):
                    slots.append((m, k))
        return slots

    def total_body(p):
        n = 0
        for c in p.get("candidates") or []:
            for holder, key in _carriers(c):
                n += len(holder.get(key) or "")
        return n

    # 单候选窗（全部载体共享单卡额度——RA-016：600 是单卡上限，
    # 不是每载体各自 600）
    for c in packet.get("candidates") or []:
        from ..retrieval import evidence as _em
        room_c = OUTPUT_MAX_PER_CANDIDATE_CHARS
        over = None
        for holder, key in _carriers(c):
            val = holder.get(key)
            if not isinstance(val, str) or not val:
                continue
            if room_c <= 0:
                holder[key] = ""  # 单卡额度耗尽：剩余载体清空
                over = True
                continue
            if len(val) > room_c:
                cut, _tr = _em.excerpt(val, limit=room_c)
                holder[key] = cut
                over = True
            room_c -= len(holder.get(key) or "")
        if over:
            c.setdefault("budget_flags", []).append("candidate_600")
    # 正文总量（全部载体）
    if total_body(packet) > OUTPUT_MAX_BODY_CHARS:
        room = OUTPUT_MAX_BODY_CHARS
        exhausted = False
        for c in packet.get("candidates") or []:
            for holder, key in _carriers(c):
                val = holder.get(key)
                if not isinstance(val, str) or not val:
                    continue
                if exhausted:
                    holder[key] = ""  # RA-016：额度耗尽清空剩余载体
                    c.setdefault("budget_flags", []).append("body_4000")
                    continue
                if len(val) > room:
                    holder[key] = val[:max(0, room)]
                    c.setdefault("budget_flags", []).append("body_4000")
                room -= len(holder.get(key) or "")
                if room <= 0:
                    exhausted = True
    # 完整 JSON 字节
    for _ in range(4):
        blob = _json.dumps(packet, ensure_ascii=False).encode("utf-8")
        if len(blob) <= OUTPUT_MAX_JSON_BYTES:
            break
        cands = packet.get("candidates") or []
        trimmed = False
        for c in reversed(cands):
            for ev in reversed(c.get("evidence") or []):
                snip = ev.get("snippet")
                if isinstance(snip, str) and len(snip) > 60:
                    ev["snippet"] = snip[:max(60, len(snip) // 2)]
                    ev["truncated"] = True
                    c.setdefault("budget_flags", []).append("json_24576")
                    trimmed = True
                    break
            if trimmed:
                break
        if not trimmed:
            packet["budget_truncated"] = True
            packet["candidates"] = []
            break
    # CB-042：终检——固定四次裁剪后仍超限时按序丢卡直至达标（或空
    # 集），不再放行超限 JSON（metadata 全量计入后的最终序列化检查）
    while packet.get("candidates"):
        blob = _json.dumps(packet, ensure_ascii=False).encode("utf-8")
        if len(blob) <= OUTPUT_MAX_JSON_BYTES:
            break
        packet["candidates"].pop()
        packet["budget_truncated"] = True
        packet.setdefault("budget_flags", []).append("dropped_candidate")
    packet.setdefault("budget", {}).setdefault(
        "output_limits", {
            "json_bytes": OUTPUT_MAX_JSON_BYTES,
            "body_chars": OUTPUT_MAX_BODY_CHARS,
            "candidates": config.RECALL_DELIVERY_LIMIT,
            "per_candidate_chars": OUTPUT_MAX_PER_CANDIDATE_CHARS})
    return packet


def _with_round_preview(session: dict) -> dict:
    """计算阶段的预算快照视图：把本轮算作已成功（rounds_used+1）。"""
    preview = dict(session)
    preview["rounds_used"] = session["rounds_used"] + 1
    return preview


def _commit_round_effects(conn, session_id: str, revision: int,
                          effects: dict, *, plan: dict | None = None,
                          scope: str = "", kind: str = "memory") -> None:
    """最终事务内的本轮派生行写入（candidates/receipts/状态/回执/
    Round1 成功回执——S13）。"""
    store.upsert_candidates(conn, session_id, effects["candidates"],
                            revision)
    store.add_receipts(conn, session_id, effects["receipts"])
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
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
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
                                  kind=r1_kind)
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
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # CB-009：提交时重验——计算期间被 close/refine 变更
                # 则整体回滚，不复活终态
                _revalidate_session_in_tx(conn, sid, expected_revision,
                                          "refine")
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
                _commit_round_effects(conn, sid, expected_revision + 1,
                                      effects, plan=plan,
                                      scope=session["conversation_scope"],
                                      kind="memory")
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
            if a.get("candidate_ref"):
                store.set_candidate_state_tx(
                    conn, a["session_id"], a["candidate_ref"], "accepted")
            if a.get("close"):
                store.update_status_tx(conn, a["session_id"],
                                       session["current_revision"],
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
            for c in cards:
                store.upsert_candidates(conn, a["session_id"], [{
                    "candidate_ref": c["candidate_ref"],
                    "resource_ref": c["resource_ref"],
                    "channel": "event", "representation": c["representation"],
                    "content_version": c["content_version"],
                    "representation_version": c["representation_version"],
                    "state": "seen", "scores": {}}],
                    session["current_revision"])
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
            store.update_status_tx(conn, a["session_id"],
                                   session["current_revision"], final)
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
                checked += 1
                if conn.execute("SELECT 1 FROM raw_messages WHERE id=?",
                                (ref[len("raw_msg:"):],)).fetchone() is None:
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


def require_owned_session(principal, session: dict,
                          a: dict | None = None) -> None:
    """S04/WP 补丁（闭环复审 P1-1）：session 归属主体 + 可信 scope。

    - session.principal_id 必须等于当前主体——两 owner 共享**记忆**，
      但 session 是主体发起的操作上下文，跨主体持 id 不得读/操作；
    - scope 省略不绕过：调用方不带 conversation_scope 时按 session
      绑定执行；显式携带且不一致则拒绝（RUNTIME-09 保持）。
    每个动作（含 status/close/round2/replay guard）统一走本入口。
    """
    pid = getattr(principal, "principal_id", principal)
    if session.get("principal_id") != pid:
        raise Forbidden(
            "recall session 归属另一主体；跨主体 session 操作被拒绝",
            code="SESSION_OWNER_MISMATCH", session_id=session["session_id"])
    # 复审#4：session 绑定了非空 scope 时，请求必须显式携带匹配的
    # conversation_scope——省略不再等于继承（Chat/CC 等权 ≠ 自动
    # 共享隐藏窗口；省略不能成为跨窗口绕过）
    bound = session.get("conversation_scope") or ""
    req_scope = (a or {}).get("conversation_scope")
    if bound:
        if req_scope is None:
            raise Forbidden(
                "session 绑定了 conversation_scope；请求必须显式携带"
                "匹配的 scope（省略不等于继承）",
                code="SCOPE_REQUIRED", session_id=session["session_id"])
    if req_scope is not None and req_scope != bound:
        # 绑定非空必须匹配；未绑定 session 显式携带 scope 同样拒绝
        # （RUNTIME-09 保持：不做"空=全局可见"解释）
        raise Forbidden(
            "conversation_scope 与 session 绑定范围不一致",
            code="SCOPE_MISMATCH", session_id=session["session_id"])


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

def _norm_recall_field(field: str) -> str:
    """候选卡字段名归一化：words 通道写 our_words.text，阶段字段集用
    our_words（N04）——导航轴（event_time/hold_time）保持原样。"""
    if isinstance(field, str) and field.startswith("our_words."):
        return "our_words"
    return field


def revalidate_replayed(fn_name: str, saved: dict,
                        request_args: dict | None = None) -> dict:
    """runtime operation 重放的当前状态重校验（审计 F07 + 复审 N03/N04）。

    旧 operation 的保存响应不得绕过当前状态判断：
    - 能力开关：Recall 运行时被禁用后旧 key 不再出站（fresh 拒绝时
      replay 同样拒绝）；
    - session 必须仍存在且活跃（EXPIRED/CLOSED/RESOLVED → 拒绝重放）；
    - 候选卡按真实资源身份逐张校验：resource_ref 可解析出 memory 的
      （含无 memory_id 字段的导航卡），要求 memory 仍 active、卡片
      content_version == 当前 revision、当前分类与卡内结构化事实一致、
      并按当前 phase 的 AllowedFields 重过滤 matched_fields/evidence
      （字段名先归一化：our_words.text → our_words）；本 session 已
      rejected 的资源剔除；无法验证资源身份的卡一律剔除（保守）；
    - packet 层元数据（revision/budget）以当前 session 现值刷新；
    - WP01：重放同样核对可信 scope——请求携带的 session 归属与
      conversation_scope 必须与 session 绑定一致（省略不等于绕过，
      不一致拒绝）。
    """
    _require_enabled()
    from ..errors import StaleOperation
    from .models import ACTIVE_STATUSES
    if not isinstance(saved, dict):
        raise StaleOperation("保存的 operation 响应结构不可识别，拒绝重放",
                             operation=fn_name)
    # CB-010（2026-10-02 审计 P1）：Raw 通道的旧响应同样受当前开关
    # 约束——撤回 Raw 访问后，已保存的 round2 包不得继续出站
    if saved.get("round") == 2 and not config.RECALL_RAW_FALLBACK_ENABLED:
        raise StaleOperation(
            "Raw 通道当前已关闭，旧 round2 响应拒绝重放",
            operation=fn_name)
    sid = saved.get("recall_session_id")
    has_candidates = isinstance(saved.get("candidates"), list)
    if not sid:
        if has_candidates:
            raise StaleOperation(
                "保存的 operation 响应缺少 session 引用，拒绝重放",
                operation=fn_name)
        return saved  # 无 session、无正文的轻量响应：无泄露面
    session = store.get_session(sid)
    if session is None:
        raise StaleOperation("recall session 不存在", session_id=sid,
                             operation=fn_name)
    session = store.expire_if_due(session)
    if session["status"] not in ACTIVE_STATUSES:
        raise StaleOperation(
            "recall session 已过期/关闭，旧 operation 响应拒绝重放",
            session_id=sid, session_status=session["status"],
            operation=fn_name)
    # WP01 scope 归属：重放与首次同权——session 动作必须核对
    # conversation_scope（请求省略 scope 时以 session 绑定为准，
    # 显式携带且不一致则拒绝）
    if request_args is not None:
        req_scope = request_args.get("conversation_scope")
        if req_scope and req_scope != session["conversation_scope"]:
            raise StaleOperation(
                "重放请求的 conversation_scope 与 session 绑定不一致",
                session_id=sid, operation=fn_name)
    if not has_candidates:
        return saved
    # CB-014（2026-10-02 审计 P1）：旧 operation 的候选属于签发时的
    # 查询计划——当前 plan 已修订（refine 换词/日期/分类）则旧包拒绝
    # 重放，不得把旧查询结果重标当前 revision；无指纹的旧格式含候选
    # 包同样 fail-closed
    current_plan = store.get_plan(sid)
    cur_fp = _query_fp(current_plan) if current_plan else None
    if saved.get("query_fingerprint") != cur_fp:
        raise StaleOperation(
            "查询计划已修订或响应缺查询指纹，旧 operation 响应拒绝重放",
            session_id=sid, operation=fn_name)
    from . import phase_policy
    rejected = store.rejected_resource_refs(sid)
    with db.formal() as conn:
        kept = []
        for c in saved["candidates"]:
            if not isinstance(c, dict):
                continue
            ref = c.get("resource_ref") or ""
            mid = c.get("memory_id")
            if not mid and isinstance(ref, str) and ref.startswith("memory:"):
                mid = ref[len("memory:"):]  # 导航卡等只带 resource_ref 的形状
            if not mid:
                # 闭环复审 P2-6：source_msg 引用按当前库校验（存在
                # 且 published）后保留——round2 幂等重放不得丢候选
                # CB-014：本 session 已拒绝的资源同样不得经 source_msg
                # 卡重放（此前该分支在 rejected 检查前直接保留）
                if (isinstance(ref, str) and ref.startswith("source_msg:")
                        and ref not in rejected):
                    row = conn.execute(
                        "SELECT published FROM source_messages WHERE"
                        " id=?", (ref[len("source_msg:"):],)).fetchone()
                    if row is not None and row["published"]:
                        kept.append(c)
                # 其余无法解析资源身份的卡一律剔除（N03）
                continue
            if ref and ref in rejected:
                continue  # 本 session 已拒绝的资源不得重放
            if isinstance(ref, str) and ref.startswith("our_word:"):
                # RA-012（2026-10-02 复审 P2）：重放按当前 provenance
                # 校验——来源撤销/换代后不再交付旧 word_verbatim
                wrow = conn.execute(
                    "SELECT w.expression_kind, w.source_ref,"
                    " w.source_binding_version, m.current_version_no"
                    " FROM memory_our_words w JOIN memories m ON"
                    " m.memory_id=w.memory_id WHERE w.word_id=?",
                    (ref[len("our_word:"):],)).fetchone()
                if wrow is None:
                    continue
                from ..retrieval.words import _word_evidence
                cur_ev = _word_evidence(wrow, conn)
                card_ev = (c.get("evidence") or [{}])[0] or {}
                if (cur_ev and cur_ev[0].get("evidence_kind")
                        != card_ev.get("evidence_kind")):
                    continue
            m = conn.execute(
                "SELECT visibility, current_version_no FROM memories"
                " WHERE memory_id=?", (mid,)).fetchone()
            if m is None or m["visibility"] != "active":
                continue  # 已删除/归档：不再可见
            if str(c.get("content_version")) != str(m["current_version_no"]):
                continue  # 版本已前进：旧 revision 正文不得出站
            # 分类变更（不产生新 revision）：卡内结构化事实过期即剔卡
            current_cats = sorted(r["category"] for r in conn.execute(
                "SELECT category FROM memory_categories WHERE memory_id=?",
                (mid,)))
            stale_fact = False
            for ev in c.get("evidence") or []:
                if not isinstance(ev, dict):
                    continue
                if ev.get("field") == "categories":
                    val = (ev.get("structured_value") or {}).get("categories")
                    if isinstance(val, list) and sorted(val) != current_cats:
                        stale_fact = True
            if stale_fact:
                continue
            if saved.get("intent") == "find_words":
                # 专项 words 跨阶段（S02/S18）：不套 event phase 过滤
                kept.append(c)
                continue
            allowed = phase_policy.eligible_fields(
                phase_policy.phase_of(mid))
            orig_fields = [_norm_recall_field(f)
                           for f in (c.get("matched_fields") or [])]
            fields = [f for f in orig_fields if f in allowed]
            if orig_fields and not fields:
                continue  # 命中字段全部不再被当前 phase 允许
            if fields != list(c.get("matched_fields") or []):
                c = dict(c)
                c["matched_fields"] = fields
                c["evidence"] = [
                    ev for ev in (c.get("evidence") or [])
                    if not isinstance(ev, dict)
                    or _norm_recall_field(ev.get("field")) in allowed]
            kept.append(c)
    out = dict(saved)
    out["candidates"] = kept
    # packet 层元数据以当前 session 刷新（N03：refine 后旧包不再宣称
    # 旧 revision/预算）
    out["revision"] = session["current_revision"]
    if isinstance(out.get("budget"), dict):
        out["budget"] = budget.snapshot(session)
    return out


# ---------- Round 2（S13 完整门禁 + commit-at-end，WP04 重写） ----------

#: S13 冻结闭集
_ROUND2_REASONS = frozenset({
    "NO_DELIVERABLE_CANDIDATE", "EVIDENCE_INSUFFICIENT",
    "VERBATIM_REQUIRED_NOT_MET", "SOURCE_DISAMBIGUATION_NEEDED",
    "EXPLICIT_REJECT_AFTER_DELIVERY"})


#: 三轮复审#1 + 全量审计 P1-01：coverage 里真正的"检索 family 状态键"
#: 闭集——lexical_scorer/stage_filter/words_forgotten/dense_pending_
#: vectors/judge_cache/_first_round_facts 等 metadata 不参与完整性
#: 判断，逐键白名单防止新增 metadata 字符串被误当"不完整 family"
#:（lexical_scorer 误杀反例；words_forgotten=disabled:<策略> 同类）。
#: P1-01 收紧：blocked（words 通道被配置关闭≠完整搜过）与
#: unavailable_pass（无写入点的死值）出列——unavailable/partial/
#: pending/truncated/blocked 都不能冒充完整，与门禁注释语义一致
_RETRIEVAL_FAMILY_STATUS_KEYS = frozenset({
    "event", "dense_event", "words_lexical", "words_dense", "judge",
})
_RETRIEVAL_COMPLETE_VALUES = frozenset({
    "complete_within_scope", "not_requested", "round2_only",
    "evaluated", "not_configured",
})


def _round2_gate(conn, session: dict, reason: str) -> tuple[bool, dict]:
    """六条件全部以服务端事实核验（S13）。返回 (allowed, gate 详情)。"""
    sid = session["session_id"]
    rev = session["current_revision"]
    gate: dict = {"reason_in_closed_set": reason in _ROUND2_REASONS}

    # 3. 当前范围的有效 Round1 完成事实
    # 全量审计 P1-01：统计回执存在 ≠ 完整完成——必须本 revision 的
    # completed=1（只在 mark_round1_complete=True 的最终事务里置位；
    # 故障/降级轮的统计回执不能再给 Round2 背书）
    receipt = store.read_round1_receipt(conn, sid, rev)
    gate["round1_receipt"] = (receipt is not None
                              and receipt.get("completed") == 1)
    gate["round1_completed"] = gate["round1_receipt"]
    if receipt is not None:
        # 复审#3 + 三轮复审#1：本轮请求过的每个 retrieval family 都必须
        # complete_within_scope——unavailable/partial/pending/truncated
        # 都不能冒充"完整搜过以后没有候选"；只检查白名单内的 family
        # 状态键，metadata 键（版本号/统计/缓存）不参与布尔判断
        cov = receipt["coverage"]
        incomplete = sorted(
            k for k in _RETRIEVAL_FAMILY_STATUS_KEYS
            if isinstance(cov.get(k), str)
            and cov[k] not in _RETRIEVAL_COMPLETE_VALUES)
        # not_configured 仅当该 family 本就未请求（plan 无
        # semantic_query 时 dense not_requested；这里 provider 未配
        # 但请求过语义 = unavailable，由请求侧写入）
        gate["retrieval_complete"] = not incomplete
        gate["incomplete_families"] = incomplete
        gate["unjudged_zero"] = receipt["unjudged_count"] == 0
        empty_input = (receipt["judged_count"] == 0
                       and receipt["unavailable_count"] == 0
                       and receipt["candidate_set_hash"]
                       == canonical_hash([]))
        gate["judge_no_fault"] = (
            receipt["unavailable_count"] == 0
            and receipt["coverage"].get("judge") in (
                "evaluated", "not_configured")
            and (receipt["judged_count"] > 0 or empty_input))
        # 6. 理由的服务端事实支持（闭环复审 P1-3：按真实证据状态
        # 判定，不用通用 needs_validation——rank_only 下一切正常
        # 交付都是 needs_validation，不构成升级理由）
        facts = receipt["coverage"].get("_first_round_facts") or {}
        delivered_n = facts.get("delivered_count", 0)
        req_met = facts.get("requirement_met")
        conflicts_n = facts.get("conflicts_count", 0)
        rejected = store.rejected_resource_refs(sid)
        fact_ok = (
            (reason == "NO_DELIVERABLE_CANDIDATE" and delivered_n == 0)
            or (reason == "EVIDENCE_INSUFFICIENT"
                and delivered_n > 0 and req_met is False)
            or (reason == "VERBATIM_REQUIRED_NOT_MET"
                and facts.get("evidence_requirement")
                == "verbatim_required" and req_met is False)
            or (reason == "SOURCE_DISAMBIGUATION_NEEDED"
                and conflicts_n > 0)
            or (reason == "EXPLICIT_REJECT_AFTER_DELIVERY"
                and bool(rejected)))
        gate["reason_fact_supported"] = fact_ok

    # 4. raw 搜索 + 当前 Jev 供应商外发授权（S15：无 source_excerpt
    #    许可则 raw 不开始）
    from ..retrieval.judges import base as judge_base
    from ..retrieval.judges.typesafe_jev import TypeSafeJevJudge
    provider = judge_base.get_provider()
    profile_ok = False
    if isinstance(provider, TypeSafeJevJudge):
        profile_ok = "source_excerpt" in (provider._data_profile
                                          or frozenset())
    gate["raw_search_authorized"] = bool(
        config.RECALL_RUNTIME_ENABLED
        and config.RECALL_RAW_FALLBACK_ENABLED)
    gate["judge_outbound_authorized"] = profile_ok

    # 5. 预算（真实 burst 计数）+ 同 burst 无既有 raw 轮
    # 全量审计 P1-03：预检必须同时确认 session 总额与**当前 burst**
    # 剩余额度——此前只查总额，burst 满 3/3 时 raw 搜索与 Jev 出站
    # 已发生、最终事务才拒（有成本操作先于预算确认）。最终事务的
    # ensure_round_available_conn 保留为并发 CAS 二次防线
    used_in_burst = conn.execute(
        "SELECT COUNT(*) AS c FROM recall_rounds WHERE session_id=?"
        " AND burst_no=?", (sid, session["current_burst"])).fetchone()["c"]
    gate["budget_available"] = (
        store.count_rounds(conn, sid)
        < config.RECALL_SESSION_BURSTS_MAX * config.RECALL_BURST_ROUNDS
        and used_in_burst < config.RECALL_BURST_ROUNDS)
    gate["no_prior_raw_round"] = not store.has_raw_round(
        conn, sid, session["current_burst"])

    allowed = all([
        gate["reason_in_closed_set"],
        gate.get("round1_receipt"),
        gate.get("retrieval_complete"),
        gate.get("unjudged_zero"),
        gate.get("judge_no_fault"),
        gate.get("reason_fact_supported"),
        gate["raw_search_authorized"],
        gate["judge_outbound_authorized"],
        gate["budget_available"],
        gate["no_prior_raw_round"],
    ])
    return allowed, gate


#: CB-011：raw 深搜租约 TTL——异常退出（进程崩溃/kill）的持有者在
#: 此窗口后可被抢占；正常路径 finally 立即释放
_RAW_LEASE_TTL_S = 180


def _acquire_raw_lease(sid: str, revision: int, burst: int) -> str | None:
    """CB-011（2026-10-02 审计 P1）：昂贵调用（raw_deep_search + Jev）
    前的跨进程互斥。抢到返回 lease_token；已有未过期租约返回 None
    （另一进程正在算同一 (session, revision, burst) 的 raw 轮）。"""
    from datetime import datetime, timedelta, timezone as _tz
    token = uuid.uuid4().hex
    now = datetime.now(_tz.utc)
    expired = (now - timedelta(seconds=_RAW_LEASE_TTL_S)).isoformat()
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT created_at FROM recall_raw_leases WHERE"
                " session_id=? AND revision=? AND burst_no=?",
                (sid, revision, burst)).fetchone()
            if row is not None and row["created_at"] > expired:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "INSERT OR REPLACE INTO recall_raw_leases(session_id,"
                " revision, burst_no, lease_token, created_at)"
                " VALUES(?,?,?,?,?)",
                (sid, revision, burst, token, now.isoformat()))
            conn.execute("COMMIT")
            return token
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _raw_lease_owned(sid: str, revision: int, burst: int,
                     token: str) -> bool:
    """RA-002（2026-10-02 复审 P1）：租约归属检查——TTL 过期被接管后，
    原持有者不得继续外发或提交（fencing）。"""
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT lease_token FROM recall_raw_leases WHERE"
            " session_id=? AND revision=? AND burst_no=?",
            (sid, revision, burst)).fetchone()
    return row is not None and row["lease_token"] == token


def _release_raw_lease(sid: str, revision: int, burst: int,
                       token: str) -> None:
    with db.recall_runtime() as conn:
        conn.execute(
            "DELETE FROM recall_raw_leases WHERE session_id=? AND"
            " revision=? AND burst_no=? AND lease_token=?",
            (sid, revision, burst, token))


def _raw_round_exclusive(sid: str, revision: int, burst: int):
    """CB-011 收尾（2026-10-02）：raw 深搜的互斥入口（上下文管理器）。

    防护两层 + 一个错误合同，各司其职：
    ① 本租约——昂贵调用（raw 深搜 + Jev）前在 runtime 库抢占
       (session, revision, burst)。同进程线程与跨进程经 BEGIN
       IMMEDIATE 落在同一个互斥上（原进程内 threading.Lock 被
       完全覆盖，已删）；TTL 过期可抢占（持有者异常退出不永久
       阻塞），finally 即时释放。
    ② raw-per-burst partial unique index——DB 最终不变量。
    ③ 提交事务内的 has_raw_round 复查——不是独立防护层，是把 ②
       的失败翻译成结构化 ROUND2_GATE_DENIED（TTL 抢占窗口下的
       输家得到 409 而非 IntegrityError 500）；正常路径租约已互斥。
    """
    import contextlib
    from ..errors import Forbidden as _F
    token = _acquire_raw_lease(sid, revision, burst)
    if token is None:
        raise _F("另一进程正在执行本 (session, revision, burst) 的"
                 " Raw 深搜；昂贵调用不重复执行",
                 code="RAW_ROUND_IN_PROGRESS", session_id=sid)

    @contextlib.contextmanager
    def _ctx():
        try:
            yield
        finally:
            _release_raw_lease(sid, revision, burst, token)

    return _ctx()


def round2(principal, a: dict, op_ctx: dict | None = None) -> dict:
    """Round 2 raw 深搜（S13）：服务端 plan、六条件门禁、raw 候选过
    同一层 Jev 出站、commit-at-end 单事务提交。

    首轮互斥见 _raw_round_exclusive（租约两层防护）；翻页走
    continuation CAS，不进互斥。
    """
    sid0 = str(a.get("session_id", ""))
    session0 = store.expire_if_due(store.require_session(sid0))
    if not str(a.get("continuation_token") or "").strip():
        token = _acquire_raw_lease(
            sid0, session0["current_revision"],
            session0["current_burst"])
        if token is None:
            raise Forbidden(
                "另一进程正在执行本 (session, revision, burst) 的"
                " Raw 深搜；昂贵调用不重复执行",
                code="RAW_ROUND_IN_PROGRESS", session_id=sid0)
        try:
            return _round2_body(principal, a, op_ctx, lease_token=token)
        finally:
            _release_raw_lease(sid0, session0["current_revision"],
                               session0["current_burst"], token)
    return _round2_body(principal, a, op_ctx)


def _round2_body(principal, a: dict, op_ctx: dict | None = None,
                 lease_token: str | None = None) -> dict:
    """round2 主体（首轮由 round2 的租约互斥包装调用，携带 fencing
    token——TTL 被接管后原持有者不得外发/提交，RA-002）。"""
    from .. import db as _db
    from . import pipeline as _pl
    from ..errors import Forbidden as _F, StaleOperation
    _require_enabled()
    sid = str(a.get("session_id", ""))
    session = store.require_session(sid)
    session = store.expire_if_due(session)
    state_machine.require_action(session, "refine")  # 活跃族状态
    require_owned_session(principal, session, a)
    reason = str(a.get("reason", ""))
    if op_ctx:
        store.check_operation_conflict(op_ctx["principal_id"],
                                       op_ctx["operation_key"],
                                       op_ctx["payload_hash"])
    # 三轮复审#2：带 continuation_token 的调用是同一 raw round 的翻页
    # ——offset 由服务端游标给出，不重走六条件门禁、不记新轮；无
    # token 才是 raw round 的开始，须过完整门禁（no_prior_raw_round
    # 只约束"新开 raw round"，不约束同一轮的翻页）
    cont_token = str(a.get("continuation_token") or "")
    # CB-010：已签发游标不豁免当前授权——Raw 开关关闭后翻页拒绝，
    # 不再继续释放原文/执行 Raw 与 Jev 成本
    if cont_token and not config.RECALL_RAW_FALLBACK_ENABLED:
        raise _F("Raw 通道当前已关闭（MARIPOSA_RAW_FALLBACK_ENABLED）",
                 code="RAW_DISABLED")
    # RA-018（2026-10-02 复审 P2）：翻页同样复核当前 source_excerpt
    # 出站授权——首页 gate 的许可生命周期必须覆盖续页
    if cont_token:
        from ..retrieval.judges import base as _jb_r2
        from ..retrieval.judges.typesafe_jev import TypeSafeJevJudge
        _prov = _jb_r2.get_provider()
        _profile_ok = (
            isinstance(_prov, TypeSafeJevJudge)
            and "source_excerpt" in (_prov._data_profile or frozenset()))
        if not _profile_ok:
            raise _F("当前 judge profile 不含 source_excerpt 许可；"
                     "Raw 翻页拒绝", code="RAW_PROFILE_WITHDRAWN")
    with _db.recall_runtime() as conn:
        if cont_token:
            cont = store.read_raw_continuation(
                conn, sid, session["current_revision"],
                session["current_burst"])
            if (cont is None or cont["token"] != cont_token
                    or not store.has_raw_round(
                        conn, sid, session["current_burst"])):
                raise _F("continuation 无效：已翻尽、被新游标取代或"
                         "raw round 不存在", code="CONTINUATION_INVALID")
            offset = int(cont["next_offset"])
        else:
            allowed, gate = _round2_gate(conn, session, reason)
            # 自审（2026-10-01）：raw round 的起始页 offset 一律由
            # 服务端定（0）——翻页位置只经服务端游标流转，客户端
            # 传 offset 不再被采纳（与服务端签发模型矛盾的自由起跳）
            offset = 0
            if not allowed:
                raise _F("Round 2 gate 未满足（S13）",
                         code="ROUND2_GATE_DENIED", gate=gate)

    # 服务端已存 plan（不接受可更换的 Round2 query_plan，S13-4）
    plan = store.get_plan(sid) or {}
    # 计算（事务外）：raw 深搜 → 候选卡 → 同层 Jev → selection
    raw_limit = max(20, config.RECALL_DELIVERY_LIMIT * 4)
    raw_out = _pl.raw_deep_search(
        principal, plan, limit=raw_limit, offset=offset)
    # RA-002：昂贵调用后、Jev 外发前校验租约归属——TTL 过期被接管的
    # 原持有者在此中止（接管者已获得新租约）
    if lease_token is not None and not _raw_lease_owned(
            sid, session["current_revision"],
            session["current_burst"], lease_token):
        raise _F("Raw 租约已被接管（TTL 过期）；本执行者放弃外发与提交",
                 code="RAW_LEASE_LOST", session_id=sid)
    raw_cards = []
    for h in raw_out.get("hits", []):
        raw_cards.append({
            "resource_ref": h["resource_ref"],
            "candidate_ref": h["resource_ref"],
            "channel": "raw",
            "representation": "raw_source",
            "content_version": None,
            "representation_version": None,
            "projection_version": config.PROJECTION_REVISION,
            "memory_date": h.get("occurred_at", "")[:10] or None,
            "matched_by": ["raw_deep"],
            "matched_fields": ["raw_messages"],
            "excerpt": h.get("excerpt"),
            "speaker": h.get("speaker"),
            "evidence": [evidence_mod.make_evidence(
                "raw_verbatim", "raw_messages", h.get("excerpt") or "",
                h["resource_ref"])],
        })
    # Jev 一层出站（fake/真实 provider 由此过 S10 硬门）
    from ..retrieval.judges import base as judge_base
    provider = judge_base.get_provider()
    raw_has_more = bool(raw_out.get("has_more"))
    coverage = {"raw": ("partial_has_more" if raw_has_more
                        else "complete_within_scope"),
                "judge": "evaluated"}
    degraded: list[str] = []
    judge_candidates = raw_cards[:config.RECALL_JUDGE_CANDIDATE_CAP]
    if isinstance(provider, judge_base.DisabledJudge):
        coverage["judge"] = "not_configured"
    else:
        judge_result = provider.judge(plan, judge_candidates,
                                      {"session_id": sid,
                                       "revision": session[
                                           "current_revision"]})
        # CB-012：Round2 judge 对账——重复 ref/陌生 ref 不是有效判断，
        # 不得静默去重后照签 evaluated（反例：provider 对一张卡返回
        # 两个相同 ref 仍 coverage=evaluated 且交付）
        _r2_sent = {c.get("candidate_ref") or c["resource_ref"]
                    for c in judge_candidates}
        _r2_seen: set = set()
        _r2_dropped = 0
        for i in judge_result.items:
            if i.candidate_ref in _r2_seen or i.candidate_ref not in _r2_sent:
                _r2_dropped += 1
                continue
            _r2_seen.add(i.candidate_ref)
        # RA-003：基数违例使整批判断无效——不保留首项附着（此前首项
        # 仍挂卡上被 selection 放行交付正文）；无 judge 的卡过不了
        # S10 硬门，fail closed
        if not _r2_dropped:
            by_ref = {i.candidate_ref: i for i in judge_result.items}
            for c in judge_candidates:
                ji = by_ref.get(c.get("candidate_ref")
                                or c["resource_ref"])
                if ji:
                    c["judge"] = ji.to_dict()
        coverage["judge"] = ("unavailable" if _r2_dropped
                             else judge_result.provider_status)
        if judge_result.degraded_reason:
            degraded.append(f"judge_{judge_result.degraded_reason}")
    sel = selection.select(judge_candidates, plan,
                           store.rejected_resource_refs(sid))
    cards = _finalize_cards(sel["delivered"])
    packet = {
        "recall_session_id": sid,
        "revision": session["current_revision"],
        "round": 2,
        "status": session["status"],
        "search_status": "FOUND" if cards else "NO_MATCH_OBSERVED",
        "delivery_action": sel["delivery_action"],
        "instruction_authority": "none",
        "content_role": "retrieved_memory",
        "coverage": coverage,
        "candidates": cards,
        "missing": sel["missing"],
        "conflicts": sel["conflicts"],
        "degraded_reasons": sorted(set(degraded)),
        "query_fingerprint": _query_fp(plan),
        "continuation": None,  # 事务内签发（三轮复审#2：服务端游标）
        # 全量审计 P2-03：首页按"本轮已成功"预览（与 Round1 同口径，
        # 不再少算一轮）；翻页不消耗轮次，用当前快照
        "budget": budget.snapshot(
            _with_round_preview(session) if not cont_token else session),
        "token_count": config.RECALL_TOKENIZER,
    }
    packet = _enforce_output_budget(packet)
    op_key = op_ctx["operation_key"] if op_ctx else None
    with _db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # CB-009：提交时重验——raw 计算期间被 refine/close 变更则
            # 整体回滚，不把旧 revision 的结果挂上已前进的 session
            _revalidate_session_in_tx(conn, sid,
                                      session["current_revision"], "refine")
            # RA-002：提交事务内 fencing——失去租约所有权的执行者不得
            # 成为提交赢家（与接管者竞争时输家在此回滚）
            if lease_token is not None:
                owned = conn.execute(
                    "SELECT lease_token FROM recall_raw_leases WHERE"
                    " session_id=? AND revision=? AND burst_no=?",
                    (sid, session["current_revision"],
                     session["current_burst"])).fetchone()
                if owned is None or owned["lease_token"] != lease_token:
                    raise _F("Raw 租约已被接管；本执行者拒绝提交",
                             code="RAW_LEASE_LOST", session_id=sid)
            # 游标先行：翻尽清除、未翻尽签发/重签（单活跃），packet 的
            # continuation 在 operation 行落库前定型。自审（2026-10-01）：
            # 翻页签发走 CAS——校验与最终事务之间无锁，并发双花恰一
            # 赢家，输家在此回滚拒 CONTINUATION_INVALID
            if raw_has_more:
                if cont_token:
                    token = store.replace_raw_continuation(
                        conn, session_id=sid,
                        revision=session["current_revision"],
                        burst_no=session["current_burst"],
                        expect_token=cont_token,
                        next_offset=raw_out["next_offset"])
                    if token is None:
                        raise _F("continuation 已被并发翻页消费",
                                 code="CONTINUATION_INVALID")
                else:
                    token = store.issue_raw_continuation(
                        conn, session_id=sid,
                        revision=session["current_revision"],
                        burst_no=session["current_burst"],
                        next_offset=raw_out["next_offset"])
                packet["continuation"] = {
                    "available": True, "action": "round2_raw_continue",
                    "offset": raw_out["next_offset"],
                    "continuation_token": token}
            elif cont_token and not store.clear_raw_continuation_if(
                    conn, session_id=sid,
                    revision=session["current_revision"],
                    burst_no=session["current_burst"],
                    expect_token=cont_token):
                raise _F("continuation 已被并发翻页消费",
                         code="CONTINUATION_INVALID")
            if op_ctx:
                store.record_operation_row(
                    conn, op_ctx["principal_id"], op_ctx["operation_key"],
                    op_ctx["payload_hash"], packet)
            # 翻页不消耗新轮（三轮复审#2）：round/budget/attempt 只在
            # raw round 首页记录
            if not cont_token:
                # ③ 错误合同（见 _raw_round_exclusive 注释）：raw-per-
                # burst 唯一索引的失败在此翻译成结构化拒绝——TTL 抢占
                # 窗口下的输家得到 409 而非 IntegrityError 500；正常
                # 路径租约已互斥，到不了这里
                if store.has_raw_round(
                        conn, sid, session["current_burst"]):
                    raise _F("并发 Round2 输家：本 burst 已有 raw 轮"
                             "（事务内复查）", code="ROUND2_GATE_DENIED")
                budget.ensure_round_available_conn(conn, sid, session)
                store.record_round(conn, sid,
                                   burst_no=session["current_burst"],
                                   operation_key=op_key, kind="raw")
            store.upsert_candidates(
                conn, sid,
                [{"candidate_ref": c["candidate_ref"],
                  "resource_ref": c["resource_ref"],
                  "channel": "raw", "representation": "raw_source",
                  "content_version": c.get("content_version"),
                  "representation_version": c.get(
                      "representation_version"),
                  "state": "seen", "scores": {}}
                 for c in raw_cards],
                session["current_revision"])
            if not cont_token:
                store.record_attempt(conn, sid, op_key or _op_id("round2"),
                                     session["current_revision"],
                                     session["current_burst"],
                                     "raw_round2", "completed")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return packet
