"""recall 共享底层助手（2026-10-04 依赖边界批）。

从 service 下沉的无 service 依赖原语：供 service / round2 / replay
三方单向引用——打破"提取模块顶部 import service + service 底部
重导出"构成的静态环（独立 import round2/replay 曾直接失败）。
monkeypatch 兼容：service 仍 `from .shared import ...` 重绑定本名，
对 service 内调用路径打补丁照常生效。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import config
from ..errors import Forbidden
from ..memory.service import canonical_hash
from ..retrieval import evidence as evidence_mod
from . import state_machine


OUTPUT_MAX_JSON_BYTES = 24576
OUTPUT_MAX_BODY_CHARS = 4000
OUTPUT_MAX_PER_CANDIDATE_CHARS = 600
OUTPUT_MAX_CANDIDATES = 3


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


def _with_round_preview(session: dict) -> dict:
    """计算阶段的预算快照视图：把本轮算作已成功（rounds_used+1）。"""
    preview = dict(session)
    preview["rounds_used"] = session["rounds_used"] + 1
    preview["_round_preview_offset"] = 1
    return preview


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
