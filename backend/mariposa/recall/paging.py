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

    在调用方事务/小事务内执行：position = (candidate_index,
    carrier_index, fragment_index) 打包为整数
    position = candidate_index*100_000_000 + carrier_index*10_000
    + fragment_index（各维天然远小于上限；服务端解码，不接受任意
    offset）。三元组自 CX-01（多载体逐载体续取）起启用。
    """
    # RRA-004：新三元编码加 2^40 偏移——与旧二元编码
    #（idx*10_000+frag，值域 <2^40 当 idx<10^9）无重叠，旧持久令牌
    # 升级后按旧编码正确解码（不静默重发首页）
    packed = ((1 << 40) + int(position[0]) * 100_000_000
              + int(position[1]) * 10_000 + int(position[2]))
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


#: RRA-004（2026-10-09 回访）：无 tag 编码的双纪元分界。f313618（提交
#: 于 2026-10-09T05:51:31Z）到 2^40 tag 版之间签发的持久游标是**无 tag
#: 三元**（idx*100_000_000+car*10_000+frag，与 8515474 二元
#: idx*10_000+frag 值域重叠、无法按值判别）——按游标行 created_at 分界：
#: 不早于 f313618 提交时刻 → 三元；更早 → 二元。缺/坏时间=无法判读
#: 纪元，明确拒绝（不猜编码）。
_UNTAGGED_TRIPLE_SINCE = "2026-10-09T05:51:31+00:00"


def _era_at_or_after(created_at, since_iso: str) -> bool:
    try:
        t = datetime.fromisoformat(
            str(created_at).strip().replace(" ", "T", 1))
    except (TypeError, ValueError):
        raise Forbidden(
            "旧游标缺少可判读的签发时间，无法判别编码纪元——明确拒绝",
            code="PAGINATION_CURSOR_INVALID")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t >= datetime.fromisoformat(since_iso)


def _resolve_cursor(result_set_id: str,
                    token: str) -> tuple[int, int, int]:
    """游标 → (candidate_index, carrier_index, fragment_index)；
    陌生/跨集游标拒绝。"""
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT result_set_id, position, created_at FROM"
            " recall_page_cursors WHERE token=?", (token,)).fetchone()
    if row is None or row["result_set_id"] != result_set_id:
        raise Forbidden(
            "游标无效或不属于该结果集；游标由服务端随页签发，不接受"
            "任意 offset", code="PAGINATION_CURSOR_INVALID",
            result_set_id=result_set_id)
    packed = int(row["position"])
    # RRA-004（二次回访）：负持久 position（DB 无 CHECK）明确拒绝——
    # 负数经整除/取模会解出 (负 idx, 9999, 9999) 类倒序坐标
    if packed < 0:
        raise Forbidden(
            "游标 position 为负（损坏/不兼容的持久游标）——明确拒绝",
            code="PAGINATION_CURSOR_INVALID",
            result_set_id=result_set_id)
    if packed >= (1 << 40):
        # 新三元（candidate, carrier, fragment）
        v = packed - (1 << 40)
        return (v // 100_000_000, (v // 10_000) % 10_000, v % 10_000)
    # RRA-004（2026-10-09 回访）：无 tag 双纪元按 created_at 判别——
    # 立即前版 f313618 的无 tag 三元此前被当二元解码（1e9 → index
    # 100000），越界空页 + 同游标 has_more=true 死循环
    if _era_at_or_after(row["created_at"], _UNTAGGED_TRIPLE_SINCE):
        return (packed // 100_000_000, (packed // 10_000) % 10_000,
                packed % 10_000)
    # 旧二元（candidate, fragment）——8515474 纪元持久令牌，carrier 维
    # 按 0（最长载体起点）解析，语义=从该卡首个载体继续
    return (packed // 10_000, 0, packed % 10_000)


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
    RRA-003（2026-10-09 回访）：分片卡不得重复携带**其他长正文载体**——
    此前分片 evidence snippet 时整段 excerpt 随每个分片原样保留（11000
    字 excerpt 跟着 1 字分片重复，首包 34482B 折半到 1 字也装不下）；
    长证据元素改为**去正文身份存根**（evidence_kind/field/source_ref/
    source_version 全留——此前 >200 字元素被整条丢弃，来源版本永久
    丢失）；短载体（≤200 字）原样保留。每个长载体的正文仍只经各自的
    片段交付（防预算翻倍）。
    """
    text = field_holder.get(field) or ""
    frag = {k: v for k, v in card.items()
            if k not in ("evidence", "excerpt")}
    excerpt = card.get("excerpt")
    excerpt_is_source = (field_holder is card and field == "excerpt")
    if (isinstance(excerpt, str) and not excerpt_is_source
            and len(excerpt) <= 200):
        frag["excerpt"] = excerpt
    stubs = []
    for ev in (card.get("evidence") or []):
        if ev is field_holder or len(ev.get("snippet") or "") > 200:
            # 身份存根：去 snippet 正文，来源版本等元数据全保留
            stubs.append({k: v for k, v in ev.items()
                          if k != "snippet"})
        else:
            stubs.append(ev)
    if stubs:
        frag["evidence"] = stubs
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


def _page_skeleton(extra_page_fields: dict, candidate_total: int) -> dict:
    """装配/校验共用的页骨架（CX-06：出口最终键的等长占位——估算用；
    最终 pagination 重建时占位被真值替换，真值恒 ≤ 占位字节）。"""
    page = dict(extra_page_fields)
    page["candidates"] = []
    page["pagination"] = {
        "returned_count": PAGE_MAX_ENTRIES,
        "candidate_total": candidate_total,
        "has_more": True,
        "next_cursor": _new_cursor_token(),
        "result_set_id": str(extra_page_fields.get("result_set_id")
                             or _new_set_id()),
    }
    return page


def _fragment_plan(page: dict, card: dict, holder: dict,
                   field: str) -> list[tuple[int, int]]:
    """载体全量的**确定性分片计划**（RRA-003/010 根因修复，二次回访
    2026-10-10）：逐片按**该片真实序列化字节**从初值折半——此前
    旧实现的固定码点片宽（按首片估宽推广到全载体）在首段
    ASCII 便宜、后段控制字符 JSON 6B/字符时后续片击穿 24576。分片
    只在空页发生（整卡顺延后的页首），预算=空页骨架；每片 ≥1 字符
    （空页必装得下，杜绝空页活锁）。计划是 (骨架, 卡, 载体) 的纯
    函数——同位置同页（P05）。"""
    text = holder.get(field) or ""
    total = len(text)
    plan: list[tuple[int, int]] = []
    start = 0
    while start < total:
        w = _FRAGMENT_CHARS_START
        while w > 1:
            end = min(total, start + w)
            frag = _fragment_card(card, holder, field, start, end)
            probe = dict(page)
            probe["candidates"] = [frag]
            if _envelope_bytes(probe) <= PAGE_ENVELOPE_MAX_BYTES:
                break
            w //= 2
        end = min(total, start + max(w, 1))
        plan.append((start, end))
        start = end
    return plan


def assemble_page(cards: list[dict], start_pos: tuple[int, int, int],
                  extra_page_fields: dict,
                  skip_flags: list[str | None] | None = None) -> dict:
    """确定性页装配（同位置+同冻结集 → 同页；P05/P06）。

    - skip_flags[i] 非空的卡跳过（页间失效/通道关闭——JFA-007/A04）：
      不产出条目、不占条目数，坐标仍按**冻结全集**前进（装配循环吃
      冻结全集+跳过标记，绝不收缩子集——收缩坐标会静默丢卡）；
    - 按序装整卡；整卡放不下且本页已有条目 → 整卡（或其下一片）顺延
      下页（不 pop、不丢）；
    - 本页尚空且单卡超限 → 逐载体分片（载体顺序=evidence 序
      +excerpt；**每片按该片真实序列化字节折半**（_fragment_plan，
      RRA-003/010 根因：固定码点片宽无法约束后段转义变宽）；
      (carrier_index, fragment_index) 双维续取，一个载体的片段耗尽才
      进下一载体，**全部载体耗尽才进下一卡**——多载体超限卡不再只取
      最长载体丢第二载体，CX-01）；
    - 每页 ≤PAGE_MAX_ENTRIES 条目；被跳过卡不占条目数；
    - 字节预算按**最终出站信封形状**估算：骨架预置 pagination 终态键
      占位（next_cursor/result_set_id 与真值等长、returned_count 取
      两位上限），出口不再追加新键——预算与出口同源（CX-06）。
    """
    page = _page_skeleton(extra_page_fields, len(cards))
    entries: list[dict] = page["candidates"]

    def _bytes_with(extra: dict | None = None) -> int:
        probe = dict(page)
        if extra is not None:
            probe["candidates"] = entries + [extra]
        return _envelope_bytes(probe)

    idx, car, fidx = start_pos
    n = len(cards)
    while idx < n and len(entries) < PAGE_MAX_ENTRIES:
        if skip_flags is not None and idx < len(skip_flags) \
                and skip_flags[idx]:
            idx += 1
            car = 0
            fidx = 0
            continue
        card = cards[idx]
        whole = dict(card)
        if _bytes_with(whole) <= PAGE_ENVELOPE_MAX_BYTES:
            entries.append(whole)
            idx += 1
            car = 0
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
            car = 0
            fidx = 0
            continue
        if car >= len(carriers):
            # 全部载体耗尽=该卡完整交付（RRA-003：正常收尾不产伪
            # invalid——position 只由服务端签发，陌生游标在
            # _resolve_cursor 已拒）
            idx += 1
            car = 0
            fidx = 0
            continue
        holder, field = carriers[car]
        text = holder.get(field) or ""
        if not text:
            car += 1
            fidx = 0
            continue
        # RRA-003/010（根因修复）：分片计划逐片按真实字节折半——
        # fidx 索引计划表（不再是 fidx×固定宽；固定宽无法约束后段
        # 转义变宽的控制字符片）
        plan = _fragment_plan(page, card, holder, field)
        if fidx >= len(plan):
            # 该载体片段耗尽 → 下一载体（不进下一卡——CX-01）
            car += 1
            fidx = 0
            continue
        start, end = plan[fidx]
        frag = _fragment_card(card, holder, field, start, end)
        entries.append(frag)
        if end >= len(text):
            car += 1
            fidx = 0
        else:
            fidx += 1
    next_pos = (idx, car, fidx)
    has_more = next_pos != (n, 0, 0)
    page["pagination"] = {
        "returned_count": len(entries),
        "candidate_total": n,
        "has_more": has_more,
        "next_position": list(next_pos) if has_more else None,
    }
    return page


def revalidate_page_cards(cards: list[dict]) -> tuple[list[str | None],
                                                      list[dict]]:
    """逐页重校验当前权限与版本（P08+A04）：失效卡标记并显式列出。

    返回 (flags, invalidated)：flags 与 cards 原序等长，第 i 项为
    失效原因或 None——**不返回收缩子集**（JFA-007 根因：装配坐标
    必须吃冻结全集，收缩子集坐标会静默丢卡）。

    与 revalidate_receipts 同口径：memory 要求仍 active 且版本一致；
    our_word 要求桶仍 active+full；通道开关（A04）对翻页同权执行
    （words/raw 关闭 → 对应前缀卡失效披露，与 replay.py 的通道拒绝
    同一开关源）。不静默补入相似记录、不拼旧正文。
    """
    flags: list[str | None] = []
    invalidated: list[dict] = []
    with db.formal() as conn:
        for c in cards:
            ref = c.get("resource_ref") or ""
            bad = None
            if ref.startswith("our_word:") \
                    and not config.RECALL_WORDS_ENABLED:
                bad = "channel_words_disabled"
            elif ref.startswith("source_msg:") \
                    and not config.RECALL_RAW_FALLBACK_ENABLED:
                bad = "channel_raw_disabled"
            elif ref.startswith("memory:"):
                row = conn.execute(
                    "SELECT visibility, current_version_no FROM memories"
                    " WHERE memory_id=?",
                    (ref[len("memory:"):],)).fetchone()
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
            flags.append(bad)
            if bad:
                invalidated.append({"resource_ref": ref, "reason": bad})
    return flags, invalidated


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
    # 政策纪元：模式混用或 revision 前进（同 mode 下 provider/
    # allowed_data 变更）都拒绝（J08+A03——不混纪元、不自动另起查询）
    policy = judge_policy.effective()
    if policy.get("mode") != pset["judge_mode"] or \
            int(policy.get("revision") or 0) != \
            int(pset["policy_revision"]):
        raise Forbidden(
            "召回判断政策已切换：旧结果集的续页拒绝（RECALL_POLICY_"
            "CHANGED）；请基于新政策重新发起查询",
            code="RECALL_POLICY_CHANGED",
            set_policy_revision=int(pset["policy_revision"]),
            current_policy_revision=int(policy.get("revision") or 0),
            set_mode=pset["judge_mode"], current_mode=policy.get("mode"),
            current_provider=policy.get("provider"))
    cursor = a.get("cursor")
    start_pos = ((0, 0, 0) if not cursor
                 else _resolve_cursor(rsid, str(cursor)))
    # RRA-004（2026-10-09 回访）：越界位置明确拒绝——合法签发的游标恒有
    # idx < 候选数（终态 (n,0,0) 不签游标）；越界位置照常装配会产出
    # 0 条目 + has_more=true + 同位置同令牌的空页死循环
    _n_total = len(pset["candidates"])
    if start_pos[0] < 0 or start_pos[0] > _n_total \
            or (start_pos[0] == _n_total
                and (start_pos[1] or start_pos[2])):
        raise Forbidden(
            "游标位置超出冻结全集（不兼容旧游标或损坏位置）——明确拒绝，"
            "不产出空页循环", code="PAGINATION_CURSOR_INVALID",
            result_set_id=rsid)
    # JFA-007：重校验返回原序失效标记（不收缩）——装配吃冻结全集，
    # 坐标与游标同一坐标系，任何页间失效不得静默丢失未交付候选
    flags, invalidated = revalidate_page_cards(pset["candidates"])
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
        "invalidated": invalidated,
        "token_count": config.RECALL_TOKENIZER,
    }
    # JSON 瘦身专项（2026-10-10 她）：查询级元数据只在**首页**发——
    # retrieval_coverage（~270B）与 budget 快照（~110B）此前每页重复
    # （续页 15% 纯重复）；E 消费面为条件透传（缺省容忍），invalidated
    # 是页间失效披露仍逐页携带（空表省略）
    if not cursor:
        extra["retrieval_coverage"] = pset["coverage"]
        extra["budget"] = budget_mod.snapshot(session)
    if not invalidated:
        extra.pop("invalidated")
    # RRA-004（二次回访 2026-10-10）：解码三元组**全量** fail-closed——
    # 此前只检 idx 正向上界：负 idx（position=-1 解出 (-1,9999,9999)）
    # 会倒序重发末卡+首页；carrier/fragment 越界被当"载体耗尽"静默跳卡/
    # 跳载体。合法签发位置恒满足：idx∈[0,n)；car<该卡载体数且 fidx<
    # 该载体分片计划长度，或 car==载体数且 fidx==0（装配循环的"载体
    # 集耗尽"合法中间态——下一页从下一卡起）；骨架与装配同源计算
    if start_pos[0] < _n_total:
        _skel = _page_skeleton(extra, _n_total)
        _card = pset["candidates"][start_pos[0]]
        _carriers = _text_carriers(_card)
        _pos_ok = False
        if 0 <= start_pos[1] < len(_carriers):
            _holder, _field = _carriers[start_pos[1]]
            _text = _holder.get(_field) or ""
            if not _text:
                _pos_ok = start_pos[2] == 0
            else:
                _pos_ok = 0 <= start_pos[2] < len(
                    _fragment_plan(_skel, _card, _holder, _field))
        elif start_pos[1] == len(_carriers):
            _pos_ok = start_pos[2] == 0
        if not _pos_ok:
            raise Forbidden(
                "游标位置超出冻结全集的合法坐标域（损坏/不兼容的持久"
                "游标）——明确拒绝，不静默跳卡或倒序重发",
                code="PAGINATION_CURSOR_INVALID",
                result_set_id=rsid, position=list(start_pos))
    page = assemble_page(pset["candidates"], start_pos, extra,
                         skip_flags=flags)
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
