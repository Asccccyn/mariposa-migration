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
    from ..retrieval import query_plan as qp
    phrase = qp.compile_plan_lexical(plan)
    if not phrase:
        phrase = "''"  # 空表达式 = 浏览（全部桶按阶段给出）
    ec = (plan.get("explicit_constraints") or {})
    where, params = _structure_filters(ec)

    # 桶集合：结构过滤后的 memories（有界 2000）
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT m.memory_id FROM memories m {cond} LIMIT 2000",
        params).fetchall()
    refs, stats = [], {"WIDE": 0, "MID": 0, "CORE": 0, "skipped_gap": 0}
    from datetime import datetime as _dt, timezone as _tz
    _now = _dt.now(_tz.utc)
    all_facts = pp.facts_for_many(conn, [r["memory_id"] for r in rows])
    for r in rows:
        mid = r["memory_id"]
        try:
            phase = pp.phase_from_facts(all_facts.get(mid, {}), now=_now)
        except (pp.DataGap, pp.PolicyError, KeyError):
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


def mark_round1_complete_tx(conn, session_id: str) -> None:
    """同语义的事务内版本（commit-at-end 最终事务调用）。"""
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


# ------------------------------------------------ 主线适配层（A04 接线）

def round1_lexical_hits(conn, plan: dict, rejected: set[str],
                        coverage: dict) -> list[dict]:
    """阶段过滤词法检索：给 _run_round 主线调用的适配层。

    与 v1.4 _event_candidates 词法路同构返回（resource_ref/_row/
    matched_by/matched_fields），但检索面按每桶当前阶段的 AllowedFields
    在分字段索引（field_fts）上执行（v1.7 §6.3）。dense 路仍由主线
    自行处理（结构过滤前置不变）。
    """
    from ..retrieval import query_plan as qp
    from ..retrieval import field_projection as fp_mod
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
        return []  # 浏览模式由主线旧路径处理（结构化浏览不带词法）

    # 复用主线的安全 FTS 编译器（terms OR 组 + phrases AND；消毒同源）
    phrase = qp.compile_plan_lexical(plan)
    if not phrase:
        return []
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    pool = conn.execute(
        f"SELECT m.memory_id FROM memories m {cond} LIMIT 2000",
        params).fetchall()

    hits: list[dict] = []
    stats = {"WIDE": 0, "MID": 0, "CORE": 0, "gap": 0}
    from datetime import datetime as _dt, timezone as _tz
    _now = _dt.now(_tz.utc)
    all_facts = pp.facts_for_many(conn, [r["memory_id"] for r in pool])
    for r in pool:
        mid = r["memory_id"]
        ref = f"memory:{mid}"
        if ref in rejected:
            continue
        try:
            phase = pp.phase_from_facts(all_facts.get(mid, {}), now=_now)
        except (pp.DataGap, pp.PolicyError, KeyError):
            stats["gap"] += 1
            continue
        stats[phase.stage] += 1
        kinds = fp_mod.stage_filter_kinds(pp.eligible_fields(phase))
        if not kinds:
            continue
        row = conn.execute(
            "SELECT memory_id, field_kind FROM field_fts"
            " WHERE field_fts MATCH ? AND memory_id=? AND field_kind IN (%s)"
            " LIMIT 3" % ",".join("?" * len(kinds)),
            (phrase, mid, *kinds)).fetchall()
        if not row:
            continue
        matched_kinds = sorted({x["field_kind"] for x in row})
        m = conn.execute(
            "SELECT m.memory_id, m.memory_date, m.compression_state,"
            " m.current_version_no, rd.projection_kind, rd.search_text,"
            " rd.whitelist_body FROM memories m"
            " JOIN retrieval_documents rd ON rd.memory_id=m.memory_id"
            " WHERE m.memory_id=?", (mid,)).fetchone()
        if m is None:
            continue
        hits.append({
            "resource_ref": ref, "candidate_ref": ref,
            "memory_id": mid, "channel": "event",
            "representation": m["projection_kind"],
            "content_version": str(m["current_version_no"]),
            "representation_version": str(m["current_version_no"]),
            "projection_version": config.PROJECTION_REVISION,
            "memory_date": m["memory_date"],
            "matched_by": (["summary_keyword"]
                           if m["compression_state"] == "forgotten_summary"
                           else ["keyword"]),
            "matched_fields": matched_kinds,
            "_row": dict(m),
        })
    coverage["event"] = "complete_within_scope"
    coverage["stage_filter"] = stats
    return hits


def round2_server_facts(session: dict) -> dict:
    """A05/F3：Round 2 gate 的服务端事实（不从客户端参数取）。"""
    from .. import db as _db
    with _db.recall_runtime() as rc:
        judge_row = rc.execute(
            "SELECT status FROM recall_attempts WHERE session_id=?"
            " ORDER BY created_at DESC LIMIT 1",
            (session["session_id"],)).fetchone()
    judge_status = judge_row["status"] if judge_row else "unknown"
    raw_authorized = (config.RECALL_RUNTIME_ENABLED
                      and config.RECALL_RAW_FALLBACK_ENABLED
                      and session.get("principal_id") in ("qiaosheng",
                                                          "jiaming"))
    bursts_used = session.get("bursts_used", 1)
    budget_left = bursts_used < config.RECALL_SESSION_BURSTS_MAX
    return {"judge_status": judge_status,
            "raw_search_authorized": raw_authorized,
            "budget_available": budget_left}
