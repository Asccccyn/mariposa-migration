"""surface 服务编排：prepare / validate / check / detail.read。

调用链（registry handler → 本模块）：E 的后台 worker 在 Uₙ 提交后调
prepare；同一 Uₙ 当前修订在装配时调 validate（剩余绝对期限内）；
最终冻结前调 check（纯本地）；主模型显式调 memory_read_full →
detail.read。全部走既有 /api/capability/{name} HTTP 面 + identity 鉴权
与 binding 白名单（docs/06）。

阻断语义：任何门未过 → blocked/空交付 + 私有运行状态，不冒充"没有
找到"（docs/06 错误闭集）；错误不整串发给主模型。
"""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time

from .. import config, db
from ..errors import Busy, Forbidden, NotFound, OutcomeUnknown
from ..memory import detail as memory_detail
from ..recall import judge_policy
from . import candidates as cand_mod
from . import contract, facts
from . import judge as judge_mod
from . import selection as sel_mod
from . import store
from .judge import REQUIRED_GRANTS, SurfaceJudge
from .models import SurfaceBlocked, surface_policy_snapshot


def _source_year(source: dict) -> int:
    """无年份日期的逐年枚举上界：源消息时间的年份（缺则 UTC 当前年）。"""
    import datetime as _dt
    raw = source.get("occurred_at")
    if isinstance(raw, str) and len(raw) >= 4 and raw[:4].isdigit():
        return int(raw[:4])
    return _dt.datetime.now(_dt.timezone.utc).year


def _evidence_ref(entry: dict) -> str | None:
    for e in entry.get("evidence") or []:
        if isinstance(e, dict) and e.get("source_ref"):
            return e["source_ref"]
    return None


def _blocked_result(reason: str, **extra) -> dict:
    out = {"status": "blocked", "reason_code": reason}
    out.update(extra)
    return out


def _surface_access_gates() -> tuple[dict | None, str | None]:
    """Current feature/provider/data permission; final checks incur no charge."""

    if not config.SURFACE_ENABLED:
        return None, "SURFACE_DISABLED"
    jp = judge_policy.effective()
    if jp["mode"] == "off":
        return None, "SURFACE_JUDGE_OFF"
    if jp["mode"] != "on":
        return None, "SURFACE_JUDGE_OFF"   # unconfigured/坏行 fail-closed
    if jp.get("provider") != "typesafe_jev":
        return None, "SURFACE_PROFILE_UNSUPPORTED"
    allowed = jp.get("allowed_data") or {}
    grants = allowed if isinstance(allowed, dict) else {}
    missing = sorted(g for g in REQUIRED_GRANTS if not grants.get(g))
    if missing:
        return None, "SURFACE_OUTBOUND_NOT_GRANTED"
    return jp, None


def _surface_gates(policy: dict) -> tuple[dict | None, str | None]:
    """Early budget check; atomic reservation happens at each HTTP boundary."""
    jp, reason = _surface_access_gates()
    if reason:
        return jp, reason
    usage = store.judge_usage_today()
    if int(usage["requests"]) >= int(policy["daily_judge_request_cap"]):
        return None, "SURFACE_BUDGET_EXHAUSTED"
    cap_usd = policy["daily_judge_cost_cap_usd"]
    if cap_usd is None:
        # 费用上限 None 不是无限：有付费出站即 blocked（显式批准后配置）
        return None, "SURFACE_BUDGET_UNCONFIGURED"
    if (usage["est_cost_usd"] is not None
            and float(usage["est_cost_usd"]) >= float(cap_usd)):
        # 已用金额的早期拒绝；每次真实 HTTP 的保守金额预占仍由
        # reserve_judge_request 原子完成，未知历史金额在那里拒绝。
        return None, "SURFACE_BUDGET_EXHAUSTED"
    return jp, None


def _deadline(raw, started: float, cap_ms: int) -> float:
    """Translate one absolute upstream deadline to a monotonic local bound."""
    limit = started + cap_ms / 1000
    if raw is None:
        return limit
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("absolute deadline requires timezone")
        wall = parsed.timestamp()
        if not math.isfinite(wall):
            raise ValueError("non-finite deadline")
    except (ValueError, TypeError, OverflowError):
        raise SurfaceBlocked("SURFACE_TIMEOUT", detail="invalid_deadline") from None
    return min(limit, started + wall - time.time())


def _before_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise SurfaceBlocked("SURFACE_TIMEOUT")


def _hypotheses_from_args(a: dict) -> list[dict]:
    out = []
    for h in a.get("event_hypotheses") or []:
        out.append({
            "hypothesis_id": str(h["hypothesis_id"]),
            "start_cp": int(h["start_cp"]),
            "end_cp": int(h["end_cp"]),
            "inherited_date_span": h.get("inherited_date_span"),
        })
    return out


# --------------------------------------------------------------- prepare

def prepare(principal, a: dict) -> dict:
    policy = surface_policy_snapshot()
    operation_id = str(a["operation_id"])
    op_key = f"surface:prepare:{operation_id}"
    phash = store.payload_hash(a)
    replay = store.claim_operation(principal.principal_id, op_key, phash)
    if replay is not None:
        # 幂等重放只恢复原作业，不无声重跑付费 judge
        if replay.get("result"):
            return replay["result"]
        if replay.get("status") == "unknown":
            return {"operation_id": operation_id,
                    "surface_run_id": "",
                    "status": "unknown_outcome",
                    "candidate_snapshot_ref": None,
                    "selected_count": 0, "coverage": {},
                    "policy_revision": policy["policy_revision"],
                    "prompt_revision": policy["prompt_prepare"],
                    "expires_at": None,
                    "reason_code": "SURFACE_UNKNOWN_OUTCOME"}
        return {"operation_id": operation_id,
                "surface_run_id": "", "status": "blocked",
                "candidate_snapshot_ref": None, "selected_count": 0,
                "coverage": {},
                "policy_revision": policy["policy_revision"],
                "prompt_revision": policy["prompt_prepare"],
                "expires_at": None,
                "reason_code": "SURFACE_PREPARE_NOT_READY"}

    started = time.monotonic()
    try:
        deadline = _deadline(a.get("deadline_at"), started, config.SURFACE_PREPARE_DEADLINE_MS)
        _before_deadline(deadline)
        result = _prepare_inner(principal, a, policy, op_key, deadline)
    except (SurfaceBlocked, sqlite3.OperationalError) as sb:
        if isinstance(sb, sqlite3.OperationalError) and time.monotonic() < deadline:
            raise
        result = {"operation_id": operation_id, "surface_run_id": "",
                  "status": "blocked", "candidate_snapshot_ref": None,
                  "selected_count": 0, "coverage": {},
                  "policy_revision": policy["policy_revision"],
                  "prompt_revision": policy["prompt_prepare"],
                  "expires_at": None, "reason_code": getattr(sb, "code", "SURFACE_TIMEOUT")}
    store.complete_operation(principal.principal_id, op_key, result)
    return result


def _prepare_inner(principal, a: dict, policy: dict, op_key: str,
                   deadline: float) -> dict:
    operation_id = str(a["operation_id"])
    base = {"operation_id": operation_id,
            "policy_revision": policy["policy_revision"],
            "prompt_revision": policy["prompt_prepare"]}

    hypotheses = _hypotheses_from_args(a)
    if len(hypotheses) > int(policy["max_hypotheses"]):
        # 超 hypothesis 上限拒绝整次分解，不截前半段猜意图
        return {**base, "surface_run_id": "", "status": "rejected",
                "candidate_snapshot_ref": None, "selected_count": 0,
                "coverage": {}, "expires_at": None,
                "reason_code": "SURFACE_EVENT_AMBIGUOUS"}
    source = a["source_input"]
    ctx_bytes = len(json.dumps(
        [source, a.get("recent_dialogue") or []],
        ensure_ascii=False).encode("utf-8"))
    if ctx_bytes > int(policy["max_source_context_bytes"]):
        # 超源上限整次拒绝，不把源消息截成不完整请求
        return {**base, "surface_run_id": "", "status": "rejected",
                "candidate_snapshot_ref": None, "selected_count": 0,
                "coverage": {"source_context_bytes": ctx_bytes},
                "expires_at": None,
                "reason_code": "SURFACE_BODY_TOO_LARGE"}

    jp, reason = _surface_gates(policy)
    if reason:
        return {**base, "surface_run_id": "", "status": "blocked",
                "candidate_snapshot_ref": None, "selected_count": 0,
                "coverage": {}, "expires_at": None, "reason_code": reason}

    if not hypotheses:
        run_id = store.new_surface_run(
            op_key, str(a["conversation_scope"]), "empty",
            {"winners": [], "hypotheses": []}, {"queries": 0},
            policy["policy_revision"], policy["prompt_prepare"], None)
        return {**base, "surface_run_id": run_id, "status": "empty",
                "candidate_snapshot_ref": run_id, "selected_count": 0,
                "coverage": {"queries": 0},
                "expires_at": _run_expiry(run_id), "reason_code": None}

    src_dict = {"message_id": source["message_id"],
                "revision": source["revision"], "text": source["text"],
                "body_hash": source["body_hash"], "occurred_at": source.get("occurred_at")}
    groups = judge_mod.partition_hypotheses(src_dict, hypotheses)
    now_year = _source_year(source)
    text = source["text"]
    span_of = {g.event_id: (text[hypotheses[i]["start_cp"]:hypotheses[i]["end_cp"]]
                            or text) for i, g in enumerate(groups)}

    # —— P2 分级检索（Tier0–2）+ 事实 + 确定性准入；Dense 只在确定性失败
    #    后按预算补；JEV 只问未解决分区里 facts 未否定的候选 ——
    retrieval: dict[str, dict] = {}
    deterministic: dict[str, contract.DeterministicAdmission] = {}
    with db.formal() as conn:
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        for g in groups:
            _before_deadline(deadline)
            r = cand_mod.tiered_partition_retrieval(
                conn, span_of[g.event_id], g.hypothesis_ids[0],
                g.constraints, now_year)
            _before_deadline(deadline)
            for e in r["candidates"]:
                e["facts"] = facts.hard_checks(e, g.constraints)
                e["evidence_ref"] = _evidence_ref(e)
            deterministic[g.event_id] = contract.admit_deterministic(
                r["candidates"], g.constraints, r["completeness"], policy)
            retrieval[g.event_id] = r
        dense_budget = int(policy["dense_queries_per_job"])
        for g in groups:
            _before_deadline(deadline)
            if deterministic[g.event_id].outcome in contract.CONFIRMED_OUTCOMES:
                continue
            r = retrieval[g.event_id]
            existing = {e["memory_id"]: e for e in r["candidates"]}
            added, status = cand_mod.dense_supplement(
                conn, span_of[g.event_id], g.hypothesis_ids[0], existing,
                dense_budget)
            _before_deadline(deadline)
            r["tiers"]["dense"] = status
            if status["status"] in ("complete_within_scope",
                                    "partial_vectors_pending"):
                dense_budget -= 1
            for e in added:
                e["facts"] = facts.hard_checks(e, g.constraints)
                e["evidence_ref"] = _evidence_ref(e)
                r["candidates"].append(e)

    skip = {ev for ev, d in deterministic.items()
            if d.outcome in contract.CONFIRMED_OUTCOMES}
    cap = int(policy["per_hypothesis_cap"])
    judge_pool: list[dict] = []
    selection_pool: list[dict] = []
    for g in groups:
        r = retrieval[g.event_id]
        d = deterministic[g.event_id]
        if g.event_id in skip:
            winner = next(c for c in r["candidates"]
                          if c["resource_ref"] == d.candidate_ref)
            selection_pool.append(winner)
            continue
        ordered = sorted(r["candidates"], key=lambda c: (
            -int(c.get("strong_match") is not None), -int(bool(c.get("recall_hit"))),
            -float(c.get("dense_score") or 0.0), c["resource_ref"]))
        judge_pool.extend(ordered[:cap])
        selection_pool.extend(ordered[:cap])
    degraded = sorted({
        f"dense_{r['tiers']['dense']['status']}" for r in retrieval.values()
        if r["tiers"]["dense"]["status"] not in ("not_requested",
                                                  "complete_within_scope")})
    coverage = {
        "queries": len(groups),
        "pool_size": len(selection_pool),
        "tiers": {ev: r["tiers"] for ev, r in retrieval.items()},
        "deterministic": {ev: d.as_dict() for ev, d in deterministic.items()},
        "union_cap": policy["candidate_union_cap"],
        "per_hypothesis_cap": cap,
    }
    if degraded:
        coverage["degraded"] = degraded
    if not selection_pool:
        coverage["judge_requests_prepare"] = 0
        run_id = store.new_surface_run(
            op_key, str(a["conversation_scope"]), "empty",
            {"winners": [], "hypotheses": hypotheses,
             "constraints": {g.event_id: g.constraints for g in groups},
             "deterministic": coverage["deterministic"],
             "tiers": coverage["tiers"]}, coverage,
            policy["policy_revision"], policy["prompt_prepare"], None)
        return {**base, "surface_run_id": run_id, "status": "empty",
                "candidate_snapshot_ref": run_id, "selected_count": 0,
                "coverage": coverage, "expires_at": _run_expiry(run_id),
                "reason_code": None}

    _before_deadline(deadline)
    judge = SurfaceJudge(jp["provider"], policy, deadline_monotonic=deadline,
                         operation_key=op_key, phase="prepare")
    try:
        if judge_pool:
            decision = judge.judge_prepare(
                src_dict, a.get("recent_dialogue") or [], hypotheses,
                judge_pool, groups=groups, skip_events=skip)
        else:
            decision = judge_mod.JudgeDecision(
                schema_version=config.SURFACE_JUDGE_SCHEMA_VERSION,
                phase="prepare", event_groups=groups, assessments=[],
                resolved_model=None,
                contract_fingerprint=judge.contract_fingerprint)
    except SurfaceBlocked as sb:
        store.record_judge_requests(op_key, "prepare", jp["provider"],
                                    judge.request_count, None, "blocked")
        return {**base, "surface_run_id": "", "status": "blocked",
                "candidate_snapshot_ref": None, "selected_count": 0,
                "coverage": coverage, "expires_at": None,
                "reason_code": sb.code}
    store.record_judge_requests(
        op_key, "prepare", jp["provider"], judge.request_count,
        None, "completed")
    coverage["judge_requests_prepare"] = judge.request_count

    det_assessments = [
        {"event_id": ev, "candidate_ref": d.candidate_ref,
         "verdict": "confirmed", "outcome": d.outcome,
         "checks": {"same_occurrence": None, "participants_consistent": None,
                    "time_status_consistent": True, "evidence_sufficient": True},
         "rank_signal": None, "reason_code": d.reason}
        for ev, d in deterministic.items() if ev in skip]
    winners, abstain = sel_mod.choose_winners(
        [{"event_id": g.event_id, "hypothesis_ids": g.hypothesis_ids,
          "grouping_confirmed": g.grouping_confirmed}
         for g in groups],
        det_assessments + [
            {"event_id": x.event_id, "candidate_ref": x.candidate_ref,
             "verdict": x.verdict, "outcome": x.outcome, "checks": x.checks,
             "rank_signal": x.rank_signal, "reason_code": x.reason_code}
            for x in decision.assessments], selection_pool)
    winners = sel_mod.finalize_prepare(winners, policy)
    usable = [w for w in winners if not w.get("skipped_reason")]

    snapshot = {
        "winners": [_snapshot_winner(w) for w in winners],
        "hypotheses": hypotheses,
        "source_input": {"message_id": source["message_id"],
                         "revision": source["revision"],
                         "body_hash": source["body_hash"]},
        "deadline_at": a.get("deadline_at"),
        # 每分区的用户原话事实约束：交付时按当前记忆状态重跑 facts
        "constraints": {g.event_id: g.constraints for g in groups},
        "deterministic": coverage["deterministic"],
        "tiers": coverage["tiers"],
        "judge_requests_prepare": judge.request_count,
        "abstain": abstain,
        "coverage": coverage,
        "contract_fingerprint": judge.contract_fingerprint,
        "resolved_model": decision.resolved_model,
    }
    status = "prepared" if usable else "empty"
    reason = None
    if not usable:
        reasons = {w.get("skipped_reason") for w in winners} | set(
            abstain.values())
        reason = next(iter(sorted(r for r in reasons if r)), None)
    run_id = store.new_surface_run(
        op_key, str(a["conversation_scope"]), status, snapshot, coverage,
        policy["policy_revision"], policy["prompt_prepare"], reason)
    return {**base, "surface_run_id": run_id, "status": status,
            "candidate_snapshot_ref": run_id,
            "selected_count": len(usable), "coverage": coverage,
            "expires_at": _run_expiry(run_id), "reason_code": reason}


def _snapshot_winner(w: dict) -> dict:
    return {k: w.get(k) for k in (
        "event_id", "resource_ref", "memory_id", "content_version",
        "memory_date", "date_confidence", "occurred_start",
        "occurred_end", "event_text", "body_hash", "domain_versions",
        "evidence", "skipped_reason", "judge_reason", "judge_outcome",
        "merged_event_ids", "rank_signal", "tier_hits", "strong_match")}


def _run_expiry(run_id: str) -> str | None:
    row = store.load_surface_run(run_id)
    return row["expires_at"] if row else None


# --------------------------------------------------------------- validate

def validate(principal, a: dict) -> dict:
    policy = surface_policy_snapshot()
    operation_id = str(a["operation_id"])
    op_key = f"surface:validate:{operation_id}"
    phash = store.payload_hash(a)
    replay = store.claim_operation(principal.principal_id, op_key, phash)
    if replay is not None:
        if replay.get("result"):
            return replay["result"]
        if replay.get("status") == "unknown":
            raise OutcomeUnknown("validation already dispatched; reconcile before retry")
        raise Busy()

    base = {"policy_revision": policy["policy_revision"]}
    deadline_at = str(a.get("deadline_at") or "")
    try:
        deadline_ts = float(deadline_at)
    except ValueError:
        from datetime import datetime
        try:
            deadline_ts = datetime.fromisoformat(
                deadline_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            deadline_ts = 0.0
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    if deadline_ts and now.timestamp() >= deadline_ts:
        result = {**base, "status": "timed_out",
                  "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None,
                  "items": [], "rejected_count": 0,
                  "reason_code": "SURFACE_TIMEOUT"}
        store.complete_operation(principal.principal_id, op_key, result)
        return result

    run = store.load_surface_run(str(a["candidate_snapshot_ref"]))
    if run is None or run["status"] not in ("prepared", "empty"):
        result = {**base, "status": "rejected", "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None,
                  "items": [], "rejected_count": 0,
                  "reason_code": "SURFACE_PREPARE_NOT_READY"}
        store.complete_operation(principal.principal_id, op_key, result)
        return result
    if run["expires_at"] < now.isoformat():
        result = {**base, "status": "rejected", "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None,
                  "items": [], "rejected_count": 0,
                  "reason_code": "SURFACE_VERSION_STALE"}
        store.complete_operation(principal.principal_id, op_key, result)
        return result
    if run["policy_revision"] != policy["policy_revision"]:
        result = {**base, "status": "rejected", "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None,
                  "items": [], "rejected_count": 0,
                  "reason_code": "SURFACE_POLICY_CHANGED"}
        store.complete_operation(principal.principal_id, op_key, result)
        return result
    if run["conversation_scope"] != str(a["conversation_scope"]):
        result = {**base, "status": "rejected", "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None,
                  "items": [], "rejected_count": 0,
                  "reason_code": "SURFACE_SCOPE_DENIED"}
        store.complete_operation(principal.principal_id, op_key, result)
        return result

    winners = [w for w in run["snapshot"].get("winners") or []
               if not w.get("skipped_reason")]
    if not winners:
        result = {**base, "status": "empty", "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None,
                  "items": [], "rejected_count": 0, "reason_code": None}
        store.complete_operation(principal.principal_id, op_key, result)
        return result

    source_input = run["snapshot"].get("source_input") or {}
    source_ref = a["source_input_ref"]
    target = a["target_input"]
    if (source_ref.get("message_id") != source_input.get("message_id")
            or source_ref.get("revision") != source_input.get("revision")
            or target.get("message_id") != source_input.get("message_id")
            or target.get("revision") != source_input.get("revision")
            or target.get("body_hash") != source_input.get("body_hash")):
        result = {**base, "status": "rejected", "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None,
                  "items": [], "rejected_count": 0,
                  "reason_code": "SURFACE_TARGET_MISMATCH"}
        store.complete_operation(principal.principal_id, op_key, result)
        return result

    jp, reason = _surface_gates(policy)
    if reason:
        result = {**base, "status": "blocked", "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None,
                  "items": [], "rejected_count": 0,
                  "reason_code": reason}
        store.complete_operation(principal.principal_id, op_key, result)
        return result

    started = time.monotonic()
    try:
        deadline = min(_deadline(a.get("deadline_at"), started, config.SURFACE_DELIVERY_VALIDATION_DEADLINE_MS),
                       _deadline(run["snapshot"].get("deadline_at"), started, config.SURFACE_DELIVERY_VALIDATION_DEADLINE_MS))
        _before_deadline(deadline)
    except SurfaceBlocked:
        result = {**base, "status": "timed_out", "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None, "items": [],
                  "rejected_count": 0, "reason_code": "SURFACE_TIMEOUT"}
        store.complete_operation(principal.principal_id, op_key, result)
        return result
    judge = SurfaceJudge(jp["provider"], policy,
                         deadline_monotonic=deadline, operation_key=op_key, phase="delivery")
    constraints_by_event = run["snapshot"].get("constraints") or {}
    try:
        decision = judge.judge_delivery(
            {"message_id": source_input["message_id"],
             "revision": source_input["revision"],
             "body_hash": source_input["body_hash"]},
            a.get("recent_dialogue") or [], a["target_input"],
            run["snapshot"].get("hypotheses") or [], winners,
            constraints_by_event)
    except SurfaceBlocked as sb:
        store.record_judge_requests(op_key, "delivery", jp["provider"],
                                    judge.request_count, None, "blocked")
        result = {**base, "status": "blocked", "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None,
                  "items": [], "rejected_count": 0,
                  "reason_code": sb.code}
        store.complete_operation(principal.principal_id, op_key, result)
        return result
    store.record_judge_requests(
        op_key, "delivery", jp["provider"], judge.request_count,
        None, "completed")

    items: list[dict] = []
    rejected = 0
    reason_code = None
    for w, asm in zip(winners, decision.assessments):
        # 同一份合同：delivery 语义 + relevant_now（contract.admit 内）
        if not asm.strictly_confirmed():
            rejected += 1
            reason_code = reason_code or (
                "SURFACE_NOT_RELEVANT_NOW"
                if asm.reason_code == "TOPIC_CHANGED" else
                "SURFACE_UNCERTAIN")
            continue
        # 版本/正文/跨域向量复核：赢家与当前正式状态不一致 → 该事件
        # 零注入（不退第二名，R23/R34）
        try:
            with db.formal() as conn:
                cur = cand_mod.memory_current_state(conn, w["memory_id"])
        except NotFound:
            rejected += 1
            reason_code = reason_code or "SURFACE_VERSION_STALE"
            continue
        stale = (str(cur["content_version"]) != str(w["content_version"])
                 or _hash(cur["event_text"]) != w["body_hash"]
                 or cur["domain_versions"] != (w.get("domain_versions")
                                               or {}))
        if stale:
            rejected += 1
            reason_code = reason_code or "SURFACE_VERSION_STALE"
            continue
        # 事实按**当前状态**重走同一份合同（她 1020：交付前事实/版本/
        # 权限基于当前状态重验，不只读 prepare 时的确认标记）
        facts_now = facts.hard_checks(
            {**w, **cur}, constraints_by_event.get(w["event_id"]) or {})
        adm_now = contract.admit(
            asm.semantic, facts_now,
            relevant_now=asm.checks.get("relevant_now"),
            require_relevance=True)
        if not adm_now.deliverable:
            rejected += 1
            reason_code = reason_code or (
                "SURFACE_NOT_RELEVANT_NOW"
                if adm_now.reason_code == "TOPIC_CHANGED" else
                "SURFACE_UNCERTAIN")
            continue
        detail_ref = "sdr_" + secrets.token_hex(16)
        items.append({
            "event_id": w["event_id"],
            "memory_id": w["memory_id"],
            "memory_version": str(w["content_version"]),
            "domain_versions": cur["domain_versions"],
            "memory_date": w.get("memory_date"),
            "date_confidence": w.get("date_confidence"),
            "event_text": w["event_text"],
            "body_hash": w["body_hash"],
            "detail_ref": detail_ref,
        })

    # Local version/fact checks also consume the same phase budget. Never
    # issue a fresh receipt for a result already outside its delivery deadline.
    if time.monotonic() >= judge.deadline_monotonic:
        result = {**base, "status": "timed_out", "validation_receipt": None,
                  "as_of": now.isoformat(), "expires_at": None, "items": [],
                  "rejected_count": rejected, "reason_code": "SURFACE_TIMEOUT"}
        store.complete_operation(principal.principal_id, op_key, result)
        return result
    status = "ready" if items else (
        "rejected" if rejected else "empty")
    ttl = max(1, int(config.SURFACE_CANDIDATE_TTL_SECONDS))
    receipt_id = None
    expires = None
    if status == "ready":
        receipt_id = store.save_validation_receipt(
            run["surface_run_id"], str(a["conversation_scope"]),
            str(a["target_input"]["message_id"]),
            int(a["target_input"]["revision"]), str(a["window_id"]),
            str(a["target_input"]["body_hash"]), items,
            policy["policy_revision"], "issued", ttl)
        expires = store.load_validation_receipt(receipt_id)["expires_at"]
    result = {**base, "status": status, "validation_receipt": receipt_id,
              "as_of": now.isoformat(), "expires_at": expires,
              "items": items, "rejected_count": rejected,
              "reason_code": reason_code}
    store.complete_operation(principal.principal_id, op_key, result)
    return result


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------- check

def check(principal, a: dict) -> dict:
    """纯本地版本/权限/政策校验（无检索、无 judge、无网络）。

    as-of 校验非跨服务锁：check→发送超过 max_receipt_age_ms 需重检。
    """
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    policy = surface_policy_snapshot()
    receipt = store.load_validation_receipt(str(a["validation_receipt"]))
    invalid = lambda code: {          # noqa: E731
        "valid": False, "as_of": now.isoformat(), "expires_at": None,
        "accepted_detail_refs": [], "reason_code": code}
    _, reason = _surface_access_gates()
    if reason:
        return invalid(reason)
    if receipt is None or receipt["status"] != "issued":
        return invalid("DETAIL_REF_NOT_SEEN"
                       if receipt is None else "SURFACE_TARGET_CHANGED")
    if receipt["conversation_scope"] != str(a["conversation_scope"]):
        return invalid("SURFACE_SCOPE_DENIED")
    if (receipt["target_message_id"] != str(a["target_message_id"])
            or int(receipt["target_revision"]) != int(a["target_revision"])):
        return invalid("SURFACE_TARGET_MISMATCH")
    if receipt["window_id"] != str(a["window_id"]):
        return invalid("SURFACE_TARGET_CHANGED")
    if receipt["policy_revision"] != policy["policy_revision"]:
        return invalid("SURFACE_POLICY_CHANGED")
    if receipt["expires_at"] < now.isoformat():
        return invalid("SURFACE_TIMEOUT")

    accepted: list[str] = []
    for item in receipt["items"]:
        with db.formal() as conn:
            try:
                cur = cand_mod.memory_current_state(
                    conn, item["memory_id"])
            except Exception:
                continue   # 桶已删 → 该项不通过（DETAIL_NOT_FOUND 语义）
        if (str(cur["content_version"]) != item["memory_version"]
                or _hash(cur["event_text"]) != item["body_hash"]):
            continue
        accepted.append(item["detail_ref"])
    if not accepted:
        return invalid("SURFACE_VERSION_STALE")
    age_ms = max(1, int(config.SURFACE_MAX_RECEIPT_AGE_MS))
    expires = (_dt.datetime.now(_dt.timezone.utc)
               + _dt.timedelta(milliseconds=age_ms)).isoformat()
    return {"valid": True, "as_of": now.isoformat(), "expires_at": expires,
            "accepted_detail_refs": accepted, "reason_code": None}


# --------------------------------------------------------------- detail

def detail_read(principal, a: dict) -> dict:
    page = memory_detail.read_page(principal, a)
    if a.get("view") == "model":
        from ..memory.model_projection import detail_view
        # Resolve history labels from the complete private snapshot, not just
        # this page. Never expose bare supersedes IDs or mislabel an old version.
        snap = store.load_detail_snapshot_by_ref(memory_detail.snapshot_key(str(a["detail_ref"]), a.get("section"))) or {}
        historical = {r.get("supersedes") for p in snap.get("pages", [])
                      for r in (p.get("sections", {}).get("recollections") or [])
                      if isinstance(r, dict) and r.get("supersedes")}
        out = detail_view(page, historical)
        section = a.get("section")
        if section in ("source", "relations") and page.get("section_status", {}).get(section) == "empty":
            out[section] = []
        return out
    return page
