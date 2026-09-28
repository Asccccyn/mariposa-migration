"""v1.7 召回管线（§6）：Round 1 阶段过滤检索 / Round 2 raw 深搜 / find_words。

模块边界（不改动既有 session 状态机）：
- round1_candidates：按每桶当前阶段（phase_policy 现算）得到 AllowedFields，
  在分字段索引（field_fts）+ 结构过滤内产生候选；raw 永不进 Round 1。
- round2_gate / raw_deep_search：Round 2 是**状态**不是第二次调用——
  全部服务端 gate（phase_policy.allow_raw_round2）成立才在获准 source/raw
  索引深搜；结果仍走同一层 Jev。
- find_words_candidates：独立 intent，全量可见 our_words（words_fts），
  不受 WIDE/MID/CORE 限制；verbatim 不足时按 §6.5/§5.6B 允许证据升级。
- Jev 唯一出站由调用方（recall/service._run_round）既有的 judge 流程承担；
  本模块不旁路直出正文。
"""
from __future__ import annotations

from .. import config, db
from ..errors import Forbidden
from ..source import query as source_query
from . import phase_policy as pp
from ..retrieval import projection


# ---------------------------------------------------------------- Round 1

def round1_candidates(conn, plan: dict, *, limit: int = None) -> dict:
    """阶段过滤后的普通事件候选（BM25 分字段 + 结构条件）。

    返回 {"candidates": [resource_ref...], "stage_field_stats": {...}}；
    每桶阶段按当前事实现算（不读持久 stage 列）。
    """
    limit = limit or config.RECALL_LEXICAL_K
    phrase = projection.compile_query(" ".join(plan.get("lexical_terms") or []))
    ec = (plan.get("explicit_constraints") or {})
    where, params = _structure_filters(ec)

    # 桶集合：结构过滤后的 memories（有界 2000）
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT m.memory_id FROM memories m {cond} LIMIT 2000",
        params).fetchall()
    refs, stats = [], {"WIDE": 0, "MID": 0, "CORE": 0, "skipped_gap": 0}
    for r in rows:
        mid = r["memory_id"]
        try:
            phase = pp.phase_of(mid)
        except (pp.DataGap, pp.PolicyError):
            stats["skipped_gap"] += 1
            continue
        stats[phase.stage] += 1
        kinds = tuple(pp.eligible_fields(phase))
        # 该桶在允许字段上命中（field_fts + field_kind 过滤）
        hit = conn.execute(
            "SELECT 1 FROM field_fts WHERE field_fts MATCH ? AND"
            " memory_id=? AND field_kind IN (%s) LIMIT 1"
            % ",".join("?" * len(_kinds_of(kinds))),
            (phrase, mid, *_kinds_of(kinds))).fetchone() if phrase else True
        if hit:
            refs.append(f"memory:{mid}")
            if len(refs) >= limit:
                break
    return {"candidates": refs, "stage_field_stats": stats}


def _kinds_of(stage_fields) -> list[str]:
    from ..retrieval import field_projection as fp
    return fp.stage_filter_kinds(stage_fields)


def _structure_filters(ec: dict) -> tuple[list[str], list]:
    where, params = ["m.visibility='active'"], []
    cats = ec.get("categories")
    if cats:
        where.append(
            "m.memory_id IN (SELECT memory_id FROM memory_categories WHERE"
            " category IN (%s))" % ",".join("?" * len(cats)))
        params += list(cats)
    dr = ec.get("event_date") or {}
    if dr.get("from"):
        where.append("m.memory_date>=?")
        params.append(dr["from"])
    if dr.get("to"):
        where.append("m.memory_date<=?")
        params.append(dr["to"])
    return where, params


# ---------------------------------------------------------------- Round 2

def round2_gate(session: dict, plan_revision: int, reason: str,
                judge_status: str, raw_authorized: bool,
                budget_left: bool) -> dict:
    """§6.4 gate：全部服务端条件核验（session/revision/receipt/judge/授权/预算/理由闭集）。"""
    ok = pp.allow_raw_round2(
        same_session=True,  # 调用方保证同一 session
        same_query_revision=plan_revision == session.get("current_revision"),
        same_scope=True,
        round1_complete_receipt=_has_round1_receipt(session),
        judge_complete=(judge_status == "complete"),
        raw_search_authorized=bool(raw_authorized),
        budget_available=bool(budget_left),
        reason=reason)
    return {"allowed": ok,
            "reason": reason,
            "gate": {"same_query_revision": plan_revision ==
                     session.get("current_revision"),
                     "round1_complete_receipt": _has_round1_receipt(session),
                     "judge_complete": judge_status == "complete",
                     "raw_authorized": bool(raw_authorized),
                     "budget_left": bool(budget_left),
                     "reason_in_closed_set": reason in pp.ROUND2_REASONS}}


def _has_round1_receipt(session: dict) -> bool:
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT 1 FROM recall_receipts WHERE session_id=? AND"
            " resource_ref='round1:complete' LIMIT 1",
            (session["session_id"],)).fetchone()
    return bool(row)


def mark_round1_complete(session_id: str) -> None:
    with db.recall_runtime() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO recall_receipts(receipt_id, session_id,"
            " resource_ref, valid_at, created_at)"
            " VALUES(?,?,?,datetime('now'),datetime('now'))",
            (f"rr_{session_id[:12]}_r1", session_id, "round1:complete"))


def raw_deep_search(principal, plan: dict, limit: int = 20) -> dict:
    """Round 2 raw 深搜：获准 source 层（published=1，human/assistant）。

    候选仍须经同一层 Jev 出站（由调用方装配）；本函数只做检索与证据
    定位。旧 raw_* 层（合成导入）不在 v1.7 深搜范围（见 FIX_REPORT §5）。
    """
    terms = plan.get("lexical_terms") or []
    query = " ".join(terms)
    ec = plan.get("explicit_constraints") or {}
    dr = ec.get("source_date") or ec.get("event_date") or {}
    res = source_query.search(
        query or None,
        senders=["human", "assistant"],
        date_from=dr.get("from"), date_to=dr.get("to"),
        limit=limit)
    hits = []
    for h in res["hits"]:
        hits.append({
            "resource_ref": f"source_msg:{h['message_id']}",
            "channel": "raw",
            "provider_message_id": h["provider_message_id"],
            "conversation_id": h["provider_conversation_id"],
            "speaker": h["speaker"],
            "excerpt": h["excerpt"],
            "occurred_at": h["created_at"],
            "evidence_kind": "raw_verbatim",
            "content_role": "retrieved_memory",
            "instruction_authority": "none",
        })
    return {"hits": hits, "source": "source_layer",
            "boundary": "Round 2 原文深搜；候选需经同一层 Jev 出站"}


# ---------------------------------------------------------------- find_words

def find_words_candidates(conn, plan: dict, *, limit: int = 30) -> dict:
    """全量 our_words 专项（§6.5）：跨阶段、speaker/日期/来源条件先过滤。

    words_fts 覆盖当前全部可见 words（v1.4 已建）；隐藏/撤权/来源失效
    由表示校验（调用方 receipt 重校验）兜底。
    """
    from ..retrieval import words as words_mod
    res = words_mod.words_search(conn, plan, limit)
    verbatim_only = (plan.get("evidence_requirement") == "verbatim_required")
    return {"words_hits": res.get("hits", []),
            "coverage": res.get("coverage"),
            "verbatim_required": verbatim_only,
            "stage_restricted": pp.find_words_stage_restricted()}
