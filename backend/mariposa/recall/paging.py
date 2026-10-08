"""关闭判断模式的冻结结果集分页（MANUAL_HANDOFF_JUDGE_SWITCH_V1 §4）。

关闭模式 ≠ 把旧 0-3 卡放宽成一批：候选全集在首页前冻结为获授权出站
投影快照（recall_page_sets，运行库迁移 14），之后 memory.recall.page
只读取该集合——不重新检索、不重新判断、不加 COUNT 轮、不重发副作用；
重复同游标在版本/权限/政策未变时结果稳定。

页合同：每页 ≤10 条目、完整响应（含 {"ok":true,"data":…} 信封）
≤24576 UTF-8 字节；按实际字节装页；放不下的候选顺延下页，不得从
全集 pop；单条超长按码点分片（start/end_char、content_complete），
拼回与获授权投影逐字一致；未经完整交付 has_more 不得为 false。

游标 = 服务端签发的非透明随机令牌（recall_page_cursors），不接受任意
offset；同位置复用同一令牌（重复读页不膨胀游标表）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .. import config, db
from ..errors import Forbidden, NotFound
from . import budget as budget_mod
from . import store

#: 工程初值（§4.3）：单页条目数与完整响应字节预算（含元数据与信封）
PAGE_MAX_ENTRIES = 10
PAGE_ENVELOPE_MAX_BYTES = 24576
#: 单候选超长时的初始分片字符上限（CJK 3 字节/字符 + 元数据余量）；
#: 仍超限则对半折半直至放得下（最少 1 字符，杜绝空页活锁）
_FRAGMENT_CHARS_START = 4000

#: 冻结投影白名单（shared._finalize_cards 同源 + 关闭模式正文载体）：
#: 不含 _row/judge 分值（关闭模式不伪造判分）等内部字段
_CARD_FIELDS = ("resource_ref", "candidate_ref", "channel", "representation",
                "content_version", "representation_version",
                "projection_version", "memory_id", "word_id", "ordinal",
                "speaker", "expression_kind", "memory_date", "matched_by",
                "matched_fields", "excerpt", "truncated", "evidence",
                "version_receipt", "rrf_score", "_matched",
                "evidence_requirement_met")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_card(card: dict) -> dict:
    """冻结用出站投影：白名单字段；正文载体保持完整（不 600/4000 截断）。"""
    out = {k: card[k] for k in _CARD_FIELDS if k in card}
    return out


def _new_set_id() -> str:
    return f"rps_{uuid.uuid4().hex[:12]}"


def _new_cursor_token() -> str:
    return f"pgc_{uuid.uuid4().hex[:16]}"


def freeze_set(conn, *, session: dict, plan: dict, policy: dict,
               cards: list[dict], coverage: dict) -> str:
    """off 模式最终事务内写入冻结结果集（调用方事务内执行）。

    cards 必须是已选全集的出站投影（project_card 后）。返回 result_set_id。
    """
    rsid = _new_set_id()
    from ..memory.service import canonical_hash
    conn.execute(
        "INSERT INTO recall_page_sets(result_set_id, session_id, revision,"
        " principal_id, conversation_scope, query_fingerprint,"
        " policy_revision, judge_mode, candidate_total, candidates_json,"
        " coverage_json, created_at, expires_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rsid, session["session_id"], int(session["current_revision"]),
         session["principal_id"], session.get("conversation_scope") or "",
         canonical_hash(plan or {}), int(policy.get("revision") or 0),
         "off", len(cards),
         json.dumps(cards, ensure_ascii=False),
         json.dumps(coverage, ensure_ascii=False), _now(),
         session.get("expires_at") or _now()))
    return rsid


def load_set(result_set_id: str) -> dict | None:
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT * FROM recall_page_sets WHERE result_set_id=?",
            (result_set_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["candidates"] = json.loads(out.pop("candidates_json") or "[]")
    out["coverage"] = json.loads(out.pop("coverage_json") or "{}")
    return out


def get_or_issue_cursor(conn, result_set_id: str, position: int) -> str:
    """同位置复用既有游标（重复读页零膨胀）；无则签发新随机令牌。

    在调用方事务/小事务内执行：position = (candidate_index, fragment_index)
    打包为整数 position = candidate_index * 10_000 + fragment_index
    （分片数天然远小于 10k；服务端解码，不接受任意 offset）。
    """
    packed = int(position[0]) * 10_000 + int(position[1])
    row = conn.execute(
        "SELECT token FROM recall_page_cursors WHERE result_set_id=?"
        " AND position=?", (result_set_id, packed)).fetchone()
    if row is not None:
        return row["token"]
    token = _new_cursor_token()
    conn.execute(
        "INSERT INTO recall_page_cursors(token, result_set_id, position,"
        " created_at) VALUES(?,?,?,?)",
        (token, result_set_id, packed, _now()))
    return token


def _resolve_cursor(result_set_id: str, token: str) -> tuple[int, int]:
    """游标 → (candidate_index, fragment_index)；陌生/跨集游标拒绝。"""
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT result_set_id, position FROM recall_page_cursors"
            " WHERE token=?", (token,)).fetchone()
    if row is None or row["result_set_id"] != result_set_id:
        raise Forbidden(
            "游标无效或不属于该结果集；游标由服务端随页签发，不接受"
            "任意 offset", code="PAGINATION_CURSOR_INVALID",
            result_set_id=result_set_id)
    packed = int(row["position"])
    return packed // 10_000, packed % 10_000


def _text_carriers(card: dict) -> list[tuple[dict, str]]:
    """候选卡上的正文载体（与 _enforce_output_budget 的 _carriers 同源）。"""
    slots = []
    for ev in card.get("evidence") or []:
        slots.append((ev, "snippet"))
    if isinstance(card.get("excerpt"), str):
        slots.append((card, "excerpt"))
    return slots


def _fragment_card(card: dict, field_holder: dict, field: str,
                   start: int, end: int) -> dict:
    """长正文分片卡：原卡片身份 + 单字段片段（码点边界）。

    片段保留资源/版本/字段身份与 content_complete=false；全部片段拼回
    与获授权投影逐字一致（P03）。
    """
    text = field_holder.get(field) or ""
    frag = {k: card[k] for k in ("resource_ref", "candidate_ref", "channel",
                                 "representation", "content_version",
                                 "representation_version", "memory_id",
                                 "word_id", "speaker", "memory_date",
                                 "evidence_requirement_met")
            if k in card}
    ev_index = None
    if field_holder is not card and field == "snippet":
        try:
            ev_index = (card.get("evidence") or []).index(field_holder)
        except ValueError:
            ev_index = None
    frag["fragment"] = {
        "kind": "evidence_snippet" if ev_index is not None else "excerpt",
        "evidence_index": ev_index,
        "start_char": start, "end_char": end,
        "total_chars": len(text),
        "text": text[start:end],
        "content_complete": False,
    }
    return frag


def _envelope_bytes(page: dict) -> int:
    return len(json.dumps({"ok": True, "data": page},
                          ensure_ascii=False).encode("utf-8"))


def _fragment_chars_for(page: dict, card: dict, holder: dict,
                        field: str) -> int:
    """该卡的固定分片大小（确定性：只依赖卡片与页元数据骨架）。

    从初值起折半，直至"该片段在当前空页骨架下也能放进预算"；最少
    1 字符。(idx, fidx) → 偏移 = fidx × 该固定值——调用点仅在本页
    entries 为空时到达，骨架与实页一致，映射全局确定，拼回逐字一致。
    """
    text = holder.get(field) or ""
    size = _FRAGMENT_CHARS_START
    while size > 0:
        frag = _fragment_card(card, holder, field, 0,
                              min(len(text), size))
        probe = dict(page)
        probe["candidates"] = [frag]
        if _envelope_bytes(probe) <= PAGE_ENVELOPE_MAX_BYTES:
            return size
        size //= 2
    return 1


def assemble_page(cards: list[dict], start_pos: tuple[int, int],
                  extra_page_fields: dict) -> dict:
    """确定性页装配（同位置+同冻结集 → 同页；P05/P06）。

    - 按序装整卡；整卡放不下且本页已有条目 → 整卡（或其下一片）顺延
      下页（不 pop、不丢）；
    - 本页尚空且单卡超限 → 对该卡最长载体按码点分片（固定片宽，见
      _fragment_chars_for），保证严格前进（无空页活锁）；
    - 每页 ≤PAGE_MAX_ENTRIES 条目。
    """
    page = dict(extra_page_fields)
    page["candidates"] = []
    entries: list[dict] = page["candidates"]

    def _bytes_with(extra: dict | None = None) -> int:
        probe = dict(page)
        if extra is not None:
            probe["candidates"] = entries + [extra]
        return _envelope_bytes(probe)

    idx, fidx = start_pos
    n = len(cards)
    while idx < n and len(entries) < PAGE_MAX_ENTRIES:
        card = cards[idx]
        whole = dict(card)
        if _bytes_with(whole) <= PAGE_ENVELOPE_MAX_BYTES:
            entries.append(whole)
            idx += 1
            fidx = 0
            continue
        if entries:
            # 本页已有条目：整卡（或其下一片）顺延——游标停在当前位置
            break
        carriers = _text_carriers(card)
        if not carriers:
            # 无正文载体却超限（巨型元数据）：该卡标记超限前进——
            # 不放空页活锁（P04：预算含全部字段，超限如实披露）
            entries.append({"invalid": True,
                            "resource_ref": card.get("resource_ref"),
                            "reason": "metadata_exceeds_page_budget"})
            idx += 1
            fidx = 0
            continue
        holder, field = max(
            carriers, key=lambda hf: len(hf[0].get(hf[1]) or ""))
        text = holder.get(field) or ""
        total = len(text)
        frag_chars = _fragment_chars_for(page, card, holder, field)
        served = fidx * frag_chars
        if served >= total:
            # fidx 已越过末片（上游不该签发）：防活锁，整卡收尾
            entries.append({"invalid": True,
                            "resource_ref": card.get("resource_ref"),
                            "reason": "cursor_beyond_fragments"})
            idx += 1
            fidx = 0
            continue
        end = min(total, served + frag_chars)
        frag = _fragment_card(card, holder, field, served, end)
        entries.append(frag)
        if end >= total:
            idx += 1
            fidx = 0
        else:
            fidx += 1
    next_pos = (idx, fidx)
    has_more = next_pos != (n, 0)
    page["pagination"] = {
        "returned_count": len(entries),
        "candidate_total": n,
        "has_more": has_more,
        "next_position": list(next_pos) if has_more else None,
    }
    return page


def revalidate_page_cards(cards: list[dict]) -> tuple[list[dict], list[dict]]:
    """逐页重校验当前权限与版本（P08）：失效卡剔除并显式列出。

    与 revalidate_receipts 同口径：memory 要求仍 active 且版本一致；
    our_word 要求桶仍 active+full。不静默补入相似记录、不拼旧正文。
    """
    kept: list[dict] = []
    invalidated: list[dict] = []
    with db.formal() as conn:
        for c in cards:
            ref = c.get("resource_ref") or ""
            bad = None
            if ref.startswith("memory:"):
                row = conn.execute(
                    "SELECT visibility, current_version_no FROM memories"
                    " WHERE memory_id=?", (ref[len("memory:"):],)).fetchone()
                if row is None or row["visibility"] != "active":
                    bad = "resource_inactive_or_missing"
                elif str(row["current_version_no"]) != \
                        (c.get("content_version") or ""):
                    bad = "content_version_changed"
            elif ref.startswith("our_word:"):
                row = conn.execute(
                    "SELECT m.visibility, m.compression_state FROM"
                    " memory_our_words w JOIN memories m ON"
                    " m.memory_id=w.memory_id WHERE w.word_id=?",
                    (ref[len("our_word:"):],)).fetchone()
                if row is None or row["visibility"] != "active" or \
                        row["compression_state"] != "full":
                    bad = "resource_inactive_or_missing"
            elif ref.startswith("source_msg:"):
                row = conn.execute(
                    "SELECT published FROM source_messages WHERE id=?",
                    (ref[len("source_msg:"):],)).fetchone()
                if row is None or not row["published"]:
                    bad = "resource_inactive_or_missing"
            else:
                bad = "resource_unverifiable"
            if bad:
                invalidated.append({"resource_ref": ref, "reason": bad})
            else:
                kept.append(c)
    return kept, invalidated


def serve_page(principal, a: dict) -> dict:
    """memory.recall.page：只读冻结结果集（零检索/零判断/零 COUNT）。"""
    from .shared import _require_enabled, require_owned_session
    _require_enabled()
    from . import judge_policy
    rsid = str(a.get("result_set_id") or "")
    if not rsid:
        raise Forbidden("result_set_id 必填", code="INVALID_ARGUMENT")
    pset = load_set(rsid)
    if pset is None:
        raise NotFound("结果集不存在或已随 session 清理",
                       result_set_id=rsid)
    session = store.require_session(pset["session_id"])
    session = store.expire_if_due(session)
    from .models import ACTIVE_STATUSES
    if session["status"] not in ACTIVE_STATUSES:
        raise Forbidden(
            f"session 状态 {session['status']} 不可续页",
            code="INVALID_STATE", session_id=session["session_id"])
    require_owned_session(principal, session, a)
    if pset["principal_id"] != principal.principal_id:
        raise Forbidden("结果集归属另一主体",
                        code="SESSION_OWNER_MISMATCH",
                        result_set_id=rsid)
    if a.get("session_id") and a["session_id"] != pset["session_id"]:
        raise Forbidden("session_id 与结果集绑定不一致",
                        code="INVALID_ARGUMENT", result_set_id=rsid)
    # 政策切换：模式混用拒绝（J08——人类切换（如 off→on）后，旧
    # off 结果集的续页 RECALL_POLICY_CHANGED；不自动另起查询）
    policy = judge_policy.effective()
    if policy.get("mode") != pset["judge_mode"]:
        raise Forbidden(
            "召回判断政策已切换：旧结果集的续页拒绝（RECALL_POLICY_"
            "CHANGED）；请基于新政策重新发起查询",
            code="RECALL_POLICY_CHANGED",
            set_policy_revision=int(pset["policy_revision"]),
            current_policy_revision=int(policy.get("revision") or 0),
            set_mode=pset["judge_mode"], current_mode=policy.get("mode"))
    cursor = a.get("cursor")
    start_pos = ((0, 0) if not cursor
                 else _resolve_cursor(rsid, str(cursor)))
    cards, invalidated = revalidate_page_cards(pset["candidates"])
    extra = {
        "recall_session_id": pset["session_id"],
        "revision": int(pset["revision"]),
        "result_set_id": rsid,
        "judge_mode": pset["judge_mode"],
        "judge_policy_revision": int(pset["policy_revision"]),
        "judgement_status": "bypassed_by_user",
        "judged_count": 0,
        "delivery_action": "needs_validation",
        "instruction_authority": "none",
        "content_role": "retrieved_memory",
        "retrieval_coverage": pset["coverage"],
        "invalidated": invalidated,
        "budget": budget_mod.snapshot(session),
        "token_count": config.RECALL_TOKENIZER,
    }
    page = assemble_page(cards, start_pos, extra)
    # 游标（同位置复用；新位置签发）——小事务，不计轮次、无副作用
    pagination = page["pagination"]
    if pagination["has_more"]:
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                pagination["next_cursor"] = get_or_issue_cursor(
                    conn, rsid, tuple(pagination.pop("next_position")))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    else:
        pagination.pop("next_position", None)
    # 本页真实出站的候选记交付回执（幂等 upsert；拒绝语义需要"在哪一轮
    # 真实出站"的证据，翻页交付同样是出站）
    delivered_refs = []
    seen_refs: set[str] = set()
    for c in page["candidates"]:
        ref = c.get("resource_ref")
        if ref and ref not in seen_refs:
            seen_refs.add(ref)
            delivered_refs.append({
                "receipt_id": f"rc_{uuid.uuid4().hex[:10]}",
                "resource_ref": ref,
                "content_version": c.get("content_version"),
                "representation_version": c.get("representation_version"),
                "permission_version": "owner_binding_v1"})
    if delivered_refs:
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                store.add_receipts(conn, pset["session_id"],
                                   delivered_refs,
                                   revision=int(pset["revision"]))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    page["pagination"]["result_set_id"] = rsid
    return page
