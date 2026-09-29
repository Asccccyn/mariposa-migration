"""共享 Recall Session 统一编排（v1.3 §3—§14 / v1.4 §3、§9）。

Chat（MCP）与 CC（estómago）等权走同一入口：session 正本只在 Mariposa
runtime 库；检索 = 查询校验 → 授权范围内 BM25+向量粗召回 → 去重/RRF →
可选 Jev 精排 → 代码门控 → 0—3 条证据包；纠正（reject/refine/navigate）
只改本 session 临时状态，不修改正式记忆。

检索内容全链路 instruction_authority=none（§10）：候选正文中的指令
只是历史数据，不提升权限、不触发工具、不改变 scope。
"""
from __future__ import annotations

import json
import uuid

from .. import config, db
from ..errors import Forbidden, NotFound
from ..retrieval import evidence as evidence_mod
from ..retrieval import query_plan as qp
from ..retrieval import semantic, selection, words as words_mod
from ..retrieval import fusion
from ..retrieval.judges import base as judge_base
from ..raw import recall as raw_recall

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
        coverage["event"] = "complete_within_scope"

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
    """dense 路独立函数（v1.7 接线后与阶段词法路并列）。"""
    dense_hits: list[dict] = []
    if plan.get("semantic_query") and (terms or phrases):
        if config.SEMANTIC_PROVIDER == "local_bge_zh":
            sem = semantic.semantic_search(
                conn, plan["semantic_query"], config.RECALL_DENSE_K,
                extra_where=base_where, extra_params=base_params)
            for s in sem:
                ref = f"memory:{s['memory_id']}"
                if ref in rejected:
                    continue
                dense_hits.append({
                    "resource_ref": ref, "candidate_ref": ref,
                    "memory_id": s["memory_id"], "channel": "event",
                    "representation": s.get("projection_kind", "full"),
                    "content_version": None,
                    "representation_version": None,
                    "projection_version": config.PROJECTION_REVISION,
                    "matched_by": ["semantic"],
                    "matched_fields": ["projection"],
                    "score": s.get("score"),
                })
            coverage["dense_event"] = "complete_within_scope"
        else:
            # provider 未配置：显式 unavailable，不因空结果谎报完整（HYBRID-07）
            coverage["dense_event"] = "unavailable"
            degraded.append("semantic_unavailable")
    else:
        coverage["dense_event"] = ("not_requested" if not (terms or phrases)
                                   else "unavailable")
        if config.SEMANTIC_PROVIDER != "local_bge_zh" and (terms or phrases):
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
        row = c.pop("_row", None)
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

        # words → raw 授权专项补查（仅证据不足 + 显式开启 + 授权范围内）
        raw_result: dict | None = None
        if ("words" in channels and
                plan.get("raw_fallback") == "when_evidence_insufficient" and
                _words_evidence_insufficient(plan, words_hits)):
            if not config.RECALL_RAW_FALLBACK_ENABLED:
                coverage["raw"] = "not_executed"
            else:
                scope = raw_recall.resolve_scope(
                    plan, principal or _current_principal())
                if not scope.get("allowed"):
                    coverage["raw"] = "not_authorized"
                else:
                    raw_result = raw_recall.scoped_search(
                        plan.get("lexical_terms") or
                        [plan.get("semantic_query", "")], scope,
                        limit=config.RECALL_DELIVERY_LIMIT + 2)
                    coverage["raw"] = raw_result["coverage"]
                    for h in raw_result["hits"]:
                        h["evidence"] = [evidence_mod.make_evidence(
                            "raw_verbatim", "raw_messages.body",
                            h.pop("body_excerpt"), h["resource_ref"])]
                        h["representation"] = "raw_source"
                        h["matched_fields"] = ["raw_messages.body"]
        elif "words" in channels:
            coverage["raw"] = "not_executed"

    # Jev 精排（可关闭/可替换；只评价被交付的候选）
    judge_result = None
    judge_candidates = event_fused[:config.RECALL_JUDGE_CANDIDATE_CAP]
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
        by_ref = {i.candidate_ref: i for i in judge_result.items}
        for c in event_fused:
            ji = by_ref.get(c.get("candidate_ref") or c["resource_ref"])
            if ji:
                c["judge"] = ji.to_dict()
                c["candidate_ref"] = ji.candidate_ref
    unjudged = max(0, len(event_fused) - len(judge_candidates))

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
    continuation = None
    if raw_result and raw_result.get("continuation"):
        continuation = {"available": True, "action": "raw_scan_continue",
                        "cursor": raw_result["continuation"]["cursor"]}
    elif ("words" in channels and not sel["delivered"] and
          plan.get("raw_fallback") == "when_evidence_insufficient" and
          not config.RECALL_RAW_FALLBACK_ENABLED):
        continuation = {"available": True, "action": "authorized_raw_fallback"}

    packet = {
        "recall_session_id": sid,
        "revision": session["current_revision"],
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
    effects = {
        "candidates": candidate_rows,
        "receipts": receipts,
        "session_status": status,
        "search_status": search_status,
        # v1.7 A05：首轮真实执行完成（无论有无候选）即签发 ROUND1_COMPLETE
        # 回执——这是 Round 2 gate 的服务端事实；故障/降级轮不签发
        "mark_round1_complete": search_status not in (
            "UNAVAILABLE", "ERROR", "DEGRADED"),
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
                         "judge", "version_receipt", "rrf_score")}
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




def _with_round_preview(session: dict) -> dict:
    """计算阶段的预算快照视图：把本轮算作已成功（rounds_used+1）。"""
    preview = dict(session)
    preview["rounds_used"] = session["rounds_used"] + 1
    return preview


def _commit_round_effects(conn, session_id: str, revision: int,
                          effects: dict) -> None:
    """最终事务内的本轮派生行写入（candidates/receipts/状态/回执）。"""
    store.upsert_candidates(conn, session_id, effects["candidates"],
                            revision)
    store.add_receipts(conn, session_id, effects["receipts"])
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
            store.record_round(conn, sid, burst_no=1, operation_key=op_key)
            _commit_round_effects(conn, sid, 1, effects)
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
    _check_conversation_scope(session, a)
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
        sid = a["session_id"]
        op_key = op_ctx["operation_key"] if op_ctx else None
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
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
                                   operation_key=op_key)
                _commit_round_effects(conn, sid, expected_revision + 1,
                                      effects)
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
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
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
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    session["current_revision"] = expected_revision + 1
    return {
        "recall_session_id": a["session_id"],
        "revision": session["current_revision"],
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
    }


def reject(principal, a: dict, op_ctx: dict | None = None) -> dict:
    _require_enabled()
    session = store.require_session(a["session_id"])
    session = store.expire_if_due(session)
    state_machine.require_action(session, "reject")
    _check_conversation_scope(session, a)
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
    result = state_machine.reject(
        {"session_id": a["session_id"],
         "candidate_ref": candidate_ref,
         "resource_ref": a.get("resource_ref") or ""},
        target)
    if target in ("event", "candidate") and a.get("resource_ref"):
        # 事件级排除：同资源全部重复表示一并标记
        for c in store.list_candidates(a["session_id"]):
            if (c["resource_ref"] == a["resource_ref"] and
                    c["state"] != "rejected"):
                store.set_candidate_state(a["session_id"],
                                          c["candidate_ref"], "rejected",
                                          reject_target=target)
    return {"rejected": result, "session_id": a["session_id"],
            "revision": session["current_revision"]}


def accept(principal, a: dict, op_ctx: dict | None = None) -> dict:
    _require_enabled()
    session = store.require_session(a["session_id"])
    session = store.expire_if_due(session)
    state_machine.require_action(session, "accept")
    _check_conversation_scope(session, a)
    if a.get("candidate_ref"):
        store.set_candidate_state(a["session_id"], a["candidate_ref"],
                                  "accepted")
    out = {"accepted": a.get("candidate_ref"), "status": session["status"]}
    if a.get("close"):
        session = store.update_status(a["session_id"],
                                      session["current_revision"],
                                      "RESOLVED")
        out["status"] = "RESOLVED"
    return out


def navigate(principal, a: dict, op_ctx: dict | None = None) -> dict:
    """沿当前 temporal_axis 取前/后相邻候选（v1.3 §5.3/§6）。"""
    _require_enabled()
    session = store.require_session(a["session_id"])
    session = store.expire_if_due(session)
    state_machine.require_action(session, "navigate")
    _check_conversation_scope(session, a)
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
        where = ["m.visibility='active'"]
        params: list = []
        if anchor:
            row = conn.execute(
                "SELECT memory_date, created_at FROM memories m"
                " JOIN retrieval_documents rd ON rd.memory_id=m.memory_id"
                " WHERE m.memory_id=?", (anchor,)).fetchone()
            if row:
                key = (row["memory_date"] if axis == "event_time"
                       else row["created_at"])
                where.append(f"({axis_field} {op} ? OR"
                             f" ({axis_field} = ? AND m.memory_id <> ?))")
                params += [key, key, anchor]
        if rejected:
            marks = ",".join("?" * len(rejected))
            where.append(f"m.memory_id NOT IN ({marks})")
            params += [r[len("memory:"):] for r in rejected
                       if r.startswith("memory:")]
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
    # 幂等 upsert（设为型）：独立事务提交，重复执行结果不变
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
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"recall_session_id": a["session_id"],
            "direction": direction, "axis": axis,
            "candidates": cards, "status": session["status"],
            "instruction_authority": "none"}


def _latest_anchor(session_id: str) -> str | None:
    receipts = store.list_receipts(session_id)
    if not receipts:
        return None
    ref = receipts[-1]["resource_ref"]
    return ref[len("memory:"):] if ref.startswith("memory:") else None


def status(principal, a: dict) -> dict:
    _require_enabled()
    session = store.require_session(a["session_id"])
    session = store.expire_if_due(session)
    state_machine.require_action(session, "status")
    _check_conversation_scope(session, a)
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
    _check_conversation_scope(session, a)
    outcome = a.get("outcome", "cancelled")
    if outcome not in ("resolved", "cancelled"):
        raise Forbidden("outcome 必须是 resolved/cancelled",
                        code="INVALID_ARGUMENT")
    final = "RESOLVED" if outcome == "resolved" else "CANCELLED"
    store.update_status(a["session_id"], session["current_revision"], final)
    return {"session_id": a["session_id"], "status": final}


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


def words_recall(principal, a: dict) -> dict:
    """memory.words.recall：独立 words 通道入口（不要求建 session）。"""
    if not config.RECALL_WORDS_ENABLED:
        raise Forbidden("words 通道未启用（MARIPOSA_WORDS_RECALL_ENABLED）",
                        code="WORDS_CHANNEL_DISABLED")
    plan = validate_query_plan({
        "original_request": a.get("query") or a.get("original_request", ""),
        "channels": ["words"],
        "lexical_terms": a.get("lexical_terms") or
        ([a["query"]] if a.get("query") else []),
        "exact_phrases": a.get("exact_phrases") or [],
        "explicit_constraints": a.get("explicit_constraints") or {},
        "explicit_negative_constraints":
            a.get("explicit_negative_constraints") or {},
    })
    with db.formal() as conn:
        res = words_mod.words_search(conn, plan,
                                     int(a.get("limit") or 20))
    res["instruction_authority"] = "none"
    res["content_role"] = "retrieved_memory"
    return res


# ---------- operation 幂等重放重校验（审计 F07） ----------

def _norm_recall_field(field: str) -> str:
    """候选卡字段名归一化：words 通道写 our_words.text，阶段字段集用
    our_words（N04）——导航轴（event_time/hold_time）保持原样。"""
    if isinstance(field, str) and field.startswith("our_words."):
        return "our_words"
    return field


def revalidate_replayed(fn_name: str, saved: dict) -> dict:
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
    - packet 层元数据（revision/budget）以当前 session 现值刷新。
    """
    _require_enabled()
    from ..errors import StaleOperation
    from .models import ACTIVE_STATUSES
    if not isinstance(saved, dict):
        raise StaleOperation("保存的 operation 响应结构不可识别，拒绝重放",
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
    if not has_candidates:
        return saved
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
                # 无法解析资源身份的卡（raw/未知前缀）：一律剔除，
                # 不允许旧正文经白放分支出站（N03）
                continue
            if ref and ref in rejected:
                continue  # 本 session 已拒绝的资源不得重放
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
