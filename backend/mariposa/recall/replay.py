"""operation 幂等重放重校验（从 service.py 拆出——2026-10-04 深耦合拆分批）。

原审计 F07 咽喉：旧 operation 保存响应的当前状态重校验（开关/会话/
Judge/资源身份/查询指纹/元数据刷新）。service 底部重导出
revalidate_replayed，既有调用方与测试零改动。
"""
from __future__ import annotations

from .. import config, db
from ..errors import Forbidden, StaleOperation
from . import budget, phase_policy, store
from .shared import _query_fp, _require_enabled

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
    # F02（2026-10-03 审计 P1）：words 开关对重放无例外（CURRENT §4）
    # ——fresh/continuation/operation replay 同权执行当前通道开关；
    # 专项 find_words 的旧响应整体拒绝，与 fresh 的 Forbidden 同权
    if saved.get("intent") == "find_words" and not config.RECALL_WORDS_ENABLED:
        raise StaleOperation(
            "Words 通道当前已关闭，旧响应拒绝重放",
            operation=fn_name)
    # RECALL-03 + CR-01（2026-10-04 全量审计 P1）：Judge 不可用或
    # 原文许可撤回后，旧 operation 不释放正文——request_ref 幂等的
    # 是结果身份，不是缓存正文的出站许可。与 fresh/continuation 共用
    # 同一当前 provider 可用性判定（不只认 DisabledJudge 类名）：
    # TypeSafe 配置失效/缺 key/无有效 profile 同样不可用；raw 卡
    # 还须当前 profile 仍含 source_excerpt。
    from ..retrieval.judges import base as _jb
    from ..retrieval.judges.typesafe_jev import TypeSafeJevJudge
    _provider = _jb.get_provider()
    _judge_down = isinstance(_provider, _jb.DisabledJudge)
    _profile = None
    if isinstance(_provider, TypeSafeJevJudge):
        if getattr(_provider, "_disabled_reason", None):
            _judge_down = True
        _profile = getattr(_provider, "_data_profile", None) \
            or frozenset()
        if not _profile:
            _judge_down = True
        # 缺 key 只对"仍在用原生 judge()（真实 HTTP 路径）"的实例
        # 判不可用——测试注入的 fake 覆写 judge() 且常以类属性声明
        # key，实例级 env 缺失会遮蔽它，但 fake 根本不走 HTTP
        if (not getattr(_provider, "_api_key", None)
                and type(_provider).judge is
                TypeSafeJevJudge.judge):
            _judge_down = True
    if _judge_down and isinstance(saved.get("candidates"), list) \
            and saved["candidates"]:
        degraded = dict(saved)
        degraded["candidates"] = []
        degraded["degraded_reasons"] = list(
            saved.get("degraded_reasons") or []) + [
                "judge_disabled_replay_body_suppressed"]
        degraded["coverage"] = dict(saved.get("coverage") or {})
        degraded["coverage"]["judge"] = "unavailable"
        degraded["delivery_action"] = "no_candidates"
        saved = degraded
    elif (_profile is not None
            and isinstance(saved.get("candidates"), list)):
        # 原文许可缩权（CR-01-R1 扩展）：每个候选按共享判定
        # required_excerpt_role 算出必要证据角色，当前 profile 缺该
        # 角色即剔卡——raw/source 缺 source_excerpt、words 缺
        # word_excerpt、事件正文卡缺 event_excerpt、纯标题卡缺
        # title_cue，与 fresh 的出站许可同权（撤回许可后旧 operation
        # 不得继续释放正文）。抑制正文时 operation/canonical/预算不变
        from ..retrieval.judges.typesafe_jev import \
            required_excerpt_role as _need_role
        _kept = []
        _suppressed_channels: set[str] = set()
        for _c in saved["candidates"]:
            if isinstance(_c, dict):
                _ch = _c.get("channel") or "event"
                if _need_role(_c) not in _profile:
                    _suppressed_channels.add(_ch)
                    continue
            _kept.append(_c)
        if _suppressed_channels:
            degraded = dict(saved)
            degraded["candidates"] = _kept
            degraded["degraded_reasons"] = list(
                saved.get("degraded_reasons") or []) + [
                    "evidence_role_withdrawn_replay_body_suppressed"]
            degraded["coverage"] = dict(saved.get("coverage") or {})
            for _ch in _suppressed_channels:
                # 通道名归一（words 通道的 coverage 键与 fresh 一致）
                degraded["coverage"][_ch if _ch != "word" else "words"] = \
                    "unavailable_profile"
            saved = degraded
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
            # F02：混合通道旧包中的 words 卡同样受当前开关约束，
            # 关闭即剔除（专项 find_words 已在入口整体拒绝）
            if (c.get("channel") == "words" or "our_words" in [
                    _norm_recall_field(f)
                    for f in (c.get("matched_fields") or [])]) \
                    and not config.RECALL_WORDS_ENABLED:
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
            if fn_name.endswith("navigate"):
                # RER-02（2026-10-04 复审）：导航卡的时间轴字段
                #（event_time/hold_time）不属 event phase 白名单——
                # 结构卡仍有效时重放不得过滤为空；可见性/版本/身份
                # 检查在上游已过
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


