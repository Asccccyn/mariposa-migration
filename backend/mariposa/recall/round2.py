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
    """条件全部以服务端事实核验（S13；2026-10-08 判断层政策化）。
    返回 (allowed, gate 详情)。

    判断类条件（unjudged_zero/judge_no_fault/外发许可）按政策分形
    （§4.4）：开启=所选 provider 自己的 source_excerpt 许可；关闭=
    无外发故不要求 provider 许可、判断统计条件按 judge_required=
    false 跳过；其余条件（首轮完成/覆盖/预算/理由闭集）两模式同权。
    """
    sid = session["session_id"]
    rev = session["current_revision"]
    from . import judge_policy
    policy = judge_policy.effective()
    gate: dict = {"reason_in_closed_set": reason in _ROUND2_REASONS,
                  "judge_mode": policy["mode"],
                  "judge_policy_revision": policy.get("revision")}

    # 3. 当前范围的有效 Round1 完成事实
    # 全量审计 P1-01：统计回执存在 ≠ 完整完成——必须本 revision 的
    # completed=1（只在 mark_round1_complete=True 的最终事务里置位；
    # 故障/降级轮的统计回执不能再给 Round2 背书）
    receipt = store.read_round1_receipt(conn, sid, rev)
    gate["round1_receipt"] = (receipt is not None
                              and receipt.get("completed") == 1)
    gate["round1_completed"] = gate["round1_receipt"]
    if receipt is not None:
        # §3.2 政策纪元核对（WP-01 A03：mode+revision——同 mode 下
        # provider/allowed_data 变更同样拒绝）：首轮执行时的政策与
        # 当前一致才放行——切换后旧 revision 的 Round2 拒绝
        # （RECALL_POLICY_CHANGED，不混纪元、不自动另起查询）
        r1_pol = (receipt["coverage"] or {}).get("_judge_policy") or {}
        if r1_pol.get("mode") and (
                r1_pol["mode"] != policy["mode"]
                or (r1_pol.get("revision") is not None
                    and int(r1_pol["revision"])
                    != int(policy.get("revision") or 0))):
            raise Forbidden(
                f"召回判断政策已切换（首轮回执 {r1_pol['mode']}"
                f"@rev{r1_pol.get('revision')}，当前 {policy['mode']}"
                f"@rev{policy.get('revision')}）：Round2 拒绝，请基于"
                "新政策重新查询",
                code="RECALL_POLICY_CHANGED",
                receipt_mode=r1_pol["mode"], current_mode=policy["mode"],
                receipt_policy_revision=r1_pol.get("revision"),
                current_policy_revision=policy.get("revision"),
                session_id=sid)
        # 复审#3 + 三轮复审#1：本轮请求过的每个 retrieval family 都必须
        # complete_within_scope——unavailable/partial/pending/truncated
        # 都不能冒充"完整搜过以后没有候选"；只检查白名单内的 family
        # 状态键，metadata 键（版本号/统计/缓存）不参与布尔判断
        cov = receipt["coverage"]
        incomplete = sorted(
            k for k in _RETRIEVAL_FAMILY_STATUS_KEYS
            if isinstance(cov.get(k), str)
            and cov[k] not in _RETRIEVAL_COMPLETE_VALUES
            # 关闭模式 judge=bypassed_by_user 不是故障——完整性按检索
            # family 判断，判断层状态由下方政策分形条件承担
            and not (policy["mode"] == "off" and k == "judge"))
        # not_configured 仅当该 family 本就未请求（plan 无
        # semantic_query 时 dense not_requested；这里 provider 未配
        # 但请求过语义 = unavailable，由请求侧写入）
        gate["retrieval_complete"] = not incomplete
        gate["incomplete_families"] = incomplete
        if policy["judge_required"]:
            # §4.4：unjudged_zero/judge_no_fault 只在 judge_required=
            # true 时使用；关闭模式 judged_count 恒 0、无判断故障可言
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
        else:
            gate["unjudged_zero"] = True
            gate["judge_no_fault"] = True
            gate["judge_conditions_bypassed"] = True
        # 6. 理由的服务端事实支持（闭环复审 P1-3：按真实证据状态
        # 判定，不用通用 needs_validation——rank_only 下一切正常
        # 交付都是 needs_validation，不构成升级理由）
        gate["reason_fact_supported"] = _reason_fact_supported(
            sid, reason,
            receipt["coverage"].get("_first_round_facts") or {})

    # 4. raw 搜索 + 判断层外发授权（S15；§4.4 政策分形）：开启=当前
    #    所选 provider 自己的 source_excerpt 许可（provider 无关，
    #    不再 isinstance TypeSafeJevJudge）；关闭=无外发不要求许可；
    #    未配置=无许可（fail-closed）
    provider = (judge_base.apply_policy_overrides(
        judge_base.get_provider_by_name(policy["provider"]),
        model_id=policy.get("model_id"),
        allowed_data=(policy.get("allowed_data") or None))
        if policy["mode"] == "on" else None)
    if policy["mode"] == "off":
        profile_ok = True  # 未向辅助判断模型外发原文
        gate["judge_outbound_basis"] = "bypassed_by_user"
    elif policy["mode"] == "on":
        profile_ok = "source_excerpt" in provider.outbound_grants()
        gate["judge_outbound_basis"] = "provider_grant"
    else:
        profile_ok = False
        gate["judge_outbound_basis"] = "unconfigured"
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


def _round2_resolve_offset(a: dict, session: dict) -> int:
    """翻页/门禁解析（1005B 重构：从 _round2_body 逐字拆出，
    行为零变更）。返回本页 raw offset——continuation_token 存在
    则走服务端游标（不重走六条件门禁，三轮复审#2）；无 token 是
    raw round 的开始，须过完整门禁。"""
    from .. import db as _db
    from ..errors import Forbidden as _F
    sid = session["session_id"]
    reason = str(a.get("reason", ""))
    # ——offset 由服务端游标给出，不重走六条件门禁、不记新轮；无
    # token 才是 raw round 的开始，须过完整门禁（no_prior_raw_round
    # 只约束"新开 raw round"，不约束同一轮的翻页）
    cont_token = str(a.get("continuation_token") or "")
    # CB-010：已签发游标不豁免当前授权——Raw 开关关闭后翻页拒绝，
    # 不再继续释放原文/执行 Raw 与 Jev 成本
    if cont_token and not config.RECALL_RAW_FALLBACK_ENABLED:
        raise _F("Raw 通道当前已关闭（MARIPOSA_RAW_FALLBACK_ENABLED）",
                 code="RAW_DISABLED")
    # RA-018（2026-10-02 复审 P2）+ §4.4（2026-10-08）：翻页同样复核
    # 当前有效门——许可生命周期必须覆盖续页；按政策分形：开启=当前
    # 所选 provider 自己的 source_excerpt 许可（provider 无关）；关闭
    # =无外发不要求许可（R08：切换/撤权后按当前模式重检，不静默重开）
    if cont_token:
        from . import judge_policy as _jp
        _pol = _jp.effective()
        if _pol["mode"] == "on":
            _prov = judge_base.apply_policy_overrides(
                judge_base.get_provider_by_name(_pol["provider"]),
                model_id=_pol.get("model_id"),
                allowed_data=(_pol.get("allowed_data") or None))
            if "source_excerpt" not in _prov.outbound_grants():
                raise _F("当前判断 provider 不含 source_excerpt 外发"
                         "许可；Raw 翻页拒绝", code="RAW_PROFILE_WITHDRAWN")
        elif _pol["mode"] == "unconfigured":
            raise _F("判断层未配置（provider 缺失）；原文外发许可不"
                     "存在，Raw 翻页拒绝", code="RAW_PROFILE_WITHDRAWN")
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
    return offset


def _round2_judge_cards(sid: str, session: dict, plan: dict,
                        raw_cards: list, raw_has_more: bool,
                        policy: dict | None = None,
                        ) -> tuple[list, dict, list]:
    """Round2 的判断层出站+基数对账（1005B 拆出；2026-10-08 政策化）。
    返回 (judge_candidates, coverage, degraded)。

    政策关闭：不构造/不调用任何 provider，本批候选全集直通分页
    （§4.4：无外发故不要求 provider 许可，judged_count=0）；开启：
    构造**所选** provider（provider 无关接口，不串联接力）。"""
    from ..retrieval.judges import base as judge_base
    from . import judge_policy
    policy = policy or judge_policy.effective()
    coverage = {"raw": ("partial_has_more" if raw_has_more
                        else "complete_within_scope"),
                "judge": "evaluated"}
    degraded: list[str] = []
    if policy["mode"] == "off":
        coverage["judge"] = "bypassed_by_user"
        return raw_cards, coverage, degraded
    provider = judge_base.apply_policy_overrides(
        judge_base.get_provider_by_name(
            policy["provider"] if policy["mode"] == "on" else None),
        model_id=policy.get("model_id"),
        allowed_data=(policy.get("allowed_data") or None))
    judge_candidates = raw_cards[:config.RECALL_JUDGE_CANDIDATE_CAP]
    if isinstance(provider, judge_base.DisabledJudge):
        coverage["judge"] = "not_configured"
    else:
        judge_result = provider.judge(plan, judge_candidates,
                                      {"session_id": sid,
                                       "revision": session[
                                           "current_revision"],
                                       "judge_policy_revision":
                                           policy.get("revision")})
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
    return judge_candidates, coverage, degraded


def _round2_body(principal, a: dict, op_ctx: dict | None = None,
                 lease_token: str | None = None) -> dict:
    """round2 主体（首轮由 round2 的租约互斥包装调用，携带 fencing
    token——TTL 被接管后原持有者不得外发/提交，RA-002）。

    2026-10-08（§4.4）：判断层按政策分形——关闭模式本批候选全集冻结
    分页（上游 raw 游标与交付分页游标分槽，R05/R06），开启模式保持
    原判断后有界交付。"""
    from .. import db as _db
    from . import pipeline as _pl
    from . import judge_policy
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
    # 翻页/门禁解析——拆出至 _round2_resolve_offset（1005B 重构）；
    # cont_token 主函数留存（提交事务/预算判断多处使用）
    cont_token = str(a.get("continuation_token") or "")
    offset = _round2_resolve_offset(a, session)
    # 政策快照（§3.2：查询固定 policy_revision/模式；交付前复核）
    policy = judge_policy.effective()

    # 服务端已存 plan（不接受可更换的 Round2 query_plan，S13-4）
    plan = store.get_plan(sid) or {}
    # 计算（事务外）：raw 深搜 → 候选卡 → 判断层（按政策）→ selection
    raw_limit = max(20, config.RECALL_DELIVERY_LIMIT * 4)
    raw_out = _pl.raw_deep_search(
        principal, plan, limit=raw_limit, offset=offset)
    # RA-002：昂贵调用后、外发前校验租约归属——TTL 过期被接管的
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
    # 判断层出站+对账（政策分形）——_round2_judge_cards
    raw_has_more = bool(raw_out.get("has_more"))
    judge_candidates, coverage, degraded = _round2_judge_cards(
        sid, session, plan, raw_cards, raw_has_more, policy=policy)
    sel = selection.select(judge_candidates, plan,
                           store.rejected_resource_refs(sid),
                           judge_required=policy["judge_required"])
    if policy["mode"] == "off":
        # 关闭模式（§4.4/R05）：本批候选全集冻结为分页结果集——上游
        # raw 游标推进以"本批全部可交付"（已冻结可逐页取）为前提；
        # 交付分页游标与上游 raw_search 续页分槽（R06）。
        # WP-01 A6（CX-06）：装页骨架=最终 packet 形状（终态字段全部
        # 前置，出口不再追加新键）
        from . import paging as _paging
        frozen_cards = [_paging.project_card(c)
                        for c in sel["delivered"]]
        page_extra = {
            "recall_session_id": sid,
            "revision": session["current_revision"],
            "round": 2,
            "status": session["status"],
            "search_status": ("FOUND" if sel["delivered"]
                              else "NO_MATCH_OBSERVED"),
            "delivery_action": "needs_validation",
            "instruction_authority": "none",
            "content_role": "retrieved_memory",
            "coverage": coverage,
            "missing": sel["missing"],
            "conflicts": sel["conflicts"],
            "degraded_reasons": sorted(set(degraded)),
            "query_fingerprint": _query_fp(plan),
            "continuation": None,  # 事务内签发（三轮复审#2）
            "budget": budget.snapshot(
                _with_round_preview(session) if not cont_token
                else session),
            "token_count": config.RECALL_TOKENIZER,
            "judge_mode": policy["mode"],
            "judge_policy_revision": int(policy.get("revision") or 0),
            "judgement_status": coverage.get("judge"),
            "judged_count": 0,
            "retrieval_coverage": dict(coverage),
        }
        first_page = _paging.assemble_page(frozen_cards, (0, 0, 0),
                                           page_extra)
        cards = first_page["candidates"]
    else:
        frozen_cards = None
        cards = _finalize_cards(sel["delivered"])
    if policy["mode"] == "off":
        packet = first_page  # 装页器产出即最终 packet（骨架=出口同源）
    else:
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
            # §4.2/§4.4 合同字段（两模式同形；关闭=分页、开启=有界交付）
            "judge_mode": policy["mode"],
            "judge_policy_revision": int(policy.get("revision") or 0),
            "judgement_status": coverage.get("judge"),
            "judged_count": 0,
            "retrieval_coverage": dict(coverage),
        }
        packet = _enforce_output_budget(packet)
    op_key = op_ctx["operation_key"] if op_ctx else None
    # §3.2：结果交付前复核当前政策纪元（WP-01 A03：mode+revision）——
    # 计算期间被人类切换（含同 mode 换 provider/allowed_data）则拒绝
    # 交付（RECALL_POLICY_CHANGED），不在同一结果集混入另一种纪元
    _cur_pol = judge_policy.effective()
    if _cur_pol["mode"] != policy["mode"] or \
            int(_cur_pol.get("revision") or 0) != \
            int(policy.get("revision") or 0):
        raise _F(
            f"召回判断政策已切换（计算时 {policy['mode']}"
            f"@rev{policy.get('revision')}，交付时 {_cur_pol['mode']}"
            f"@rev{_cur_pol.get('revision')}）：Round2 拒绝提交",
            code="RECALL_POLICY_CHANGED",
            compute_mode=policy["mode"], current_mode=_cur_pol["mode"],
            compute_policy_revision=policy.get("revision"),
            current_policy_revision=_cur_pol.get("revision"),
            session_id=sid)
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
            # 关闭模式（§4.4/R05）：本批结果集冻结落库+首页续页游标
            # ——先于 operation 行（保存结果包含最终 result_set_id 与
            # next_cursor，同 op 重放回同一首页）
            if frozen_cards is not None:
                from .service import _persist_page_set_in_tx
                _persist_page_set_in_tx(
                    conn, packet,
                    {"page_set": {"cards": frozen_cards,
                                  "policy": policy},
                     "coverage": coverage},
                    session, plan)
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
    # 终检，S16 的 24KB 上限对最终出站包（含游标）同样成立。
    # 关闭模式例外（§4.3/§4.4）：分页装配器自管字节预算，不经旧
    # 裁剪器（不得 pop 本批候选）
    if packet.get("judge_mode") == "off":
        return packet
    return _enforce_output_budget(packet)

