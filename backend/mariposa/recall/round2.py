"""Round 2 深搜（从 service.py 拆出——2026-10-04 深耦合拆分批）。

S13 完整门禁 + commit-at-end + raw 租约。依赖 service 的共享小工具经
顶部导入（service 的重导出在文件底部，执行到此已就绪）；service 重导出
round2 与 _reason_fact_supported，既有调用方与测试零改动。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from .. import config, db
from ..errors import Forbidden, MariposaError, NotFound, StaleOperation
from ..identity import Principal
from ..retrieval import evidence as evidence_mod
from ..retrieval import selection
from ..retrieval.judges import base as judge_base
from ..retrieval.judges import typesafe_jev
from . import budget, state_machine, store
from .models import validate_query_plan

from ..memory import service as _mem_svc
_F = Forbidden  # 原 service 内别名，随块迁移
_canonical_hash = _mem_svc.canonical_hash
from .shared import (_enforce_output_budget, _finalize_cards,
                      _now_iso, _op_id, _query_fp,
                      _require_enabled, _revalidate_session_in_tx,
                      _with_round_preview, require_owned_session)

# ---------- Round 2（S13 完整门禁 + commit-at-end，WP04 重写） ----------

#: S13 冻结闭集
def _reason_fact_supported(sid: str, reason: str, facts: dict) -> bool:
    """Round2 理由的服务端事实支持（闭环复审 P1-3 + 裁定 2026-10-04）。

    EXPLICIT_REJECT_AFTER_DELIVERY 的 delivery = 候选真实进入过
    模型侧可见的出站交付包（有交付回执且绑定出站轮）——内部 seen、
    检索池出现、judge 看过都不算；不限定当前 revision（用户可拒绝
    前一轮真实交付过的候选），但必须能证明曾经出站。
    """
    delivered_n = facts.get("delivered_count", 0)
    req_met = facts.get("requirement_met")
    conflicts_n = facts.get("conflicts_count", 0)
    rejected = store.rejected_resource_refs(sid)
    delivered_refs = {r["resource_ref"]
                      for r in store.list_receipts(sid)}
    return (
        (reason == "NO_DELIVERABLE_CANDIDATE" and delivered_n == 0)
        or (reason == "EVIDENCE_INSUFFICIENT"
            and delivered_n > 0 and req_met is False)
        or (reason == "VERBATIM_REQUIRED_NOT_MET"
            and facts.get("evidence_requirement")
            == "verbatim_required" and req_met is False)
        or (reason == "SOURCE_DISAMBIGUATION_NEEDED"
            and conflicts_n > 0)
        or (reason == "EXPLICIT_REJECT_AFTER_DELIVERY"
            and bool(rejected & delivered_refs)))


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
                       == _canonical_hash([]))
        gate["judge_no_fault"] = (
            receipt["unavailable_count"] == 0
            and receipt["coverage"].get("judge") in (
                "evaluated", "not_configured")
            and (receipt["judged_count"] > 0 or empty_input))
        # 6. 理由的服务端事实支持（闭环复审 P1-3：按真实证据状态
        # 判定，不用通用 needs_validation——rank_only 下一切正常
        # 交付都是 needs_validation，不构成升级理由）
        gate["reason_fact_supported"] = _reason_fact_supported(
            sid, reason,
            receipt["coverage"].get("_first_round_facts") or {})

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
            # RECALL-04（2026-10-04 二批）：Round2 首页与续页的实际
            # 出站卡同事务保存带 revision 的交付回执——后续
            # EXPLICIT_REJECT_AFTER_DELIVERY 等拒绝理由才有出站事实
            import uuid as _u
            store.add_receipts(
                conn, sid,
                [{"receipt_id": f"rr_{_u.uuid4().hex[:14]}",
                  "resource_ref": c["resource_ref"],
                  "content_version": c.get("content_version"),
                  "representation_version": c.get(
                      "representation_version")}
                 for c in packet.get("candidates", [])],
                revision=session["current_revision"])
            if not cont_token:
                store.record_attempt(conn, sid, op_key or _op_id("round2"),
                                     session["current_revision"],
                                     session["current_burst"],
                                     "raw_round2", "completed")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    # P3（2026-10-05 审计）：continuation 在预算执行后注入——补一次
    # 终检，S16 的 24KB 上限对最终出站包（含游标）同样成立
    return _enforce_output_budget(packet)

