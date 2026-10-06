"""正式记忆：hold / get / versions / 遗忘审批应用 / 恢复。

关键不变量（§4）：
- 工作区正文不进正式检索；遗忘只产生新版本，保留桶 ID 与原文引用；
- 遗忘后任何默认文本匹配只依据当前有效摘要投影；
- 审批在一个正式库事务内完成：决议 + 新版本 + 指针 + 投影/FTS + 审计 + 幂等。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound, ProposalAlreadyResolved, ProposalHashMismatch, ProposalStale, VersionConflict
from ..retrieval import projection
from . import categories as categories_mod
from . import our_words as our_words_mod

_APPROVERS = {"qiaosheng", "jiaming"}  # 工具人无审批权（§6.3）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def representation_version(conn, memory_id: str) -> int:
    """当前表示版本：内容修订不改它，遗忘/恢复等表示变化才 +1（§5.3）。"""
    m = conn.execute("SELECT representation_state FROM memories WHERE memory_id=?",
                     (memory_id,)).fetchone()
    if m is None:
        raise NotFound("memory not found", memory_id=memory_id)
    return m["representation_state"]


def version_body(v) -> str | None:
    """当前 memory revision 正文的唯一解析入口（审计 F04/F14 统一收敛）。

    - full 表示：event_text 优先；v1 旧数据（无 event_text / event_text
      为空）回退 hold_text。兼容 fallback 只允许集中在此处实现，
      update/meaning/rebuild/history/检索投影等业务路径一律经本函数
      取正文，不得各自决定来源，也不得让旧 hold_text 覆盖已升级的
      event_text。
    - forgotten_summary 表示：只认审批摘要 compressed_summary。
    """
    if v["representation"] == "forgotten_summary":
        return v["compressed_summary"]
    return v["event_text"] or v["hold_text"]


def rebuild_full_projection(conn, memory_id: str) -> None:
    """当前 revision 的 full 投影重建单一入口（审计 F04）。

    full 投影 = 当前正文（version_body，含 v1 fallback）；whitelist_body
    只含事件正文（S03 禁检裁定：解释类文字不参与检索）。
    update / meaning 追加与替换 / 全库 rebuild 一律走本入口，
    保证同一 revision 的投影内容来源一致。
    """
    m = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                     (memory_id,)).fetchone()
    if m is None:
        raise NotFound("memory not found", memory_id=memory_id)
    v = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id=? AND version_no=?",
        (memory_id, m["current_version_no"])).fetchone()
    body = version_body(v) or ""
    # 2026-09-30 裁定（S03/十一-3）：why/meaning 属禁检来源，彻底退出
    # searchable projection——full 投影的 search_text 只含事件正文，
    # 与 whitelist_body 一致。meaning 层仍可通过 meanings.list 显式读取，
    # 但不再因文本匹配参与任何召回
    projection.upsert(
        conn, memory_id, m["current_version_no"], "full",
        projection.normalize_search_text(body),
        whitelist_body=projection.normalize_search_text(body))


#: 2026-10-05 冻结裁定（当晚修订）：心情标签**只放大类**（固定大类表，
#: 检索稳定性的唯一锚点）；子心情不预定义词表——模型有更具体的
#: 感受就在 mood_note 自由写（如"开心想抱抱"），就一个大类能概括时
#: 不必写（"开心"不必写子心情），写不写都合法。禁止把子心情塞进
#: 标签槽：自由词进标签会把筛库打碎（今天"吃醋"明天"酸"）。
#: 2026-10-05 终版表（她当晚指 定）：开心/爱/生气/吃醋/悲伤/渴望/不安
MOOD_CATEGORIES: tuple[str, ...] = (
    "开心", "爱", "生气", "吃醋", "悲伤", "渴望", "不安")
#: 心情标签上限（V2-REC：不硬选唯一，也不堆砌）——服务端校验与
#: memory.mood.vocab 的 max_tags 必须同源，不得各写一个 3
MOOD_TAGS_MAX = 3


def _validate_mood(principal, mood: dict, creation_mode: str) -> dict:
    """当时心情资格（R05/§5.1）：仅周家明、仅同期 hold 可写。

    2026-10-05 勘误回滚：此前擅自把"心情字段的延展随便写"扩大解释
    成"心情随时可附"——裁定指的是 why_remember 删除后的解释槽归
    属，当时心情的窗口规矩（V2-REC-04）不变。"""
    if not isinstance(mood, dict):
        raise Forbidden("mood must be an object", code="INVALID_ARGUMENT")
    if principal.principal_id != "jiaming":
        raise Forbidden(
            "当时心情只能由周家明（jiaming）在原事件窗口内写下；"
            "乔生/worker 不可代写（V2-REC-09）",
            code="MOOD_AUTHOR_REQUIRED")
    if creation_mode != "contemporaneous":
        raise Forbidden(
            "跨窗口补记不能补造当时心情（V2-REC-04）；仍可保存事件本身",
            code="MOOD_WINDOW_REQUIRED")
    text = mood.get("text")
    tags = mood.get("tags") or []
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip()
                                         for t in tags):
        raise Forbidden("mood.tags must be a list of non-empty strings",
                        code="INVALID_ARGUMENT")
    if text is not None and not isinstance(text, str):
        raise Forbidden("mood.text must be a string", code="INVALID_ARGUMENT")
    deduped = []
    for t in tags:
        t = t.strip()
        if t and t not in deduped:
            deduped.append(t)
    if len(deduped) > MOOD_TAGS_MAX:
        raise Forbidden(
            f"心情标签最多 {MOOD_TAGS_MAX} 个（给了 {len(deduped)} 个）——"
            "不硬选唯一，也不堆砌", code="MOOD_TAGS_LIMIT", tags=deduped)
    bad = [t for t in deduped if t not in MOOD_CATEGORIES]
    if bad:
        raise Forbidden(
            f"心情标签只能是大类（固定 {len(MOOD_CATEGORIES)} 个）："
            + "、".join(MOOD_CATEGORIES)
            + "；更具体的当下感受写 mood_note（自由文字，可写可不写，"
              "永不进检索）",
            code="MOOD_CATEGORY_REQUIRED", got=bad)
    return {"text": text, "tags": deduped}


def _raw_ref_hash(ref: dict) -> str:
    import hashlib as _hl
    return _hl.sha256(
        f"{ref.get('conversation_id')}:{ref.get('message_from')}"
        f":{ref.get('message_to')}".encode()).hexdigest()


def _duplicated_by_raw_ref(conn, raw_refs: list[dict] | None) -> str | None:
    """CB-051：legacy raw 表已退役——恒无重复。raw_refs 在 hold 入口
    已被显式拒绝，本函数仅为历史内部调用点保留空实现。"""
    return None

#: 2026-10-05 江乔生裁定（终版）：桶编号 = 纯五位数字（00001-99999），
#: 不带分类字母——分类筛直达由 memory.by_category 承担，编号只做
#: 全局唯一短标识。单一全局号段，永不复用（桶删除后号码作废不回填）；
#: 既有 mem_ 旧编号不受影响，两类并存。
_BUCKET_ID_COUNTER_KEY = "_global"
_BUCKET_ID_MAX = 99999


def _next_bucket_id(conn, primary_category: str) -> str:
    """写锁内取下一号（hold_in_tx 恒在 BEGIN IMMEDIATE 内调用，
    并发分配由写锁串行化）。primary_category 形参保留以兼容调用点，
    编号不再依赖分类。

    号段永不复用：计数持久化在 bucket_id_counters，桶删除后下一号
    继续前进——此前从存活行取 MAX，删除后的重建会回卷复用旧号
    （审计 2026-10-05 自查）。"""
    row = conn.execute(
        "SELECT next FROM bucket_id_counters WHERE category=?",
        (_BUCKET_ID_COUNTER_KEY,)).fetchone()
    n = row["next"] if row is not None else 1
    if n > _BUCKET_ID_MAX:
        raise Forbidden(
            f"桶编号已达 {_BUCKET_ID_MAX} 上限（号段永不复用）",
            code="BUCKET_ID_EXHAUSTED")
    conn.execute(
        "INSERT INTO bucket_id_counters(category, next) VALUES(?,?)"
        " ON CONFLICT(category) DO UPDATE SET next=excluded.next",
        (_BUCKET_ID_COUNTER_KEY, n + 1))
    return f"{n:05d}"


def _insert_core_rows(conn, *, memory_id: str, principal_id: str, text: str,
                      memory_date, date_confidence, mode,
                      original_title, v2: bool, now: str,
                      occurred_start: str | None = None,
                      occurred_end: str | None = None) -> None:
    """memories + memory_versions(1) + 投影。必须在正式库事务内调用。

    occurred_start/end 是事件实际发生区间的独立载体（R02），不再静默
    丢弃；它们参与检索日期筛选，但不改变留存计时（R03 按 hold 起算）。
    """
    conn.execute(
        "INSERT INTO memories(memory_id, current_version_no, memory_date,"
        " date_confidence, visibility, compression_state, created_at,"
        " updated_at, held_at, held_at_confidence, creation_mode,"
        " occurred_start, occurred_end)"
        " VALUES(?,?,?,?, 'active','full', ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, 1, memory_date, date_confidence, now, now,
         now if v2 else None, "exact" if v2 else "unknown",
         mode or "legacy_unknown", occurred_start, occurred_end))
    payload = {"representation": "full", "hold_text": text,
               "authored_by": principal_id}
    if v2:
        conn.execute(
            "INSERT INTO memory_versions(memory_id, version_no,"
            " representation, hold_text, compressed_summary,"
            " authored_by, confirmed_by, origin_kind,"
            " payload_hash, created_at, original_title, event_text,"
            " schema_version)"
            " VALUES(?,1,'full',NULL,NULL,?,NULL,'initial_hold',?,?,"
            "?,?,2)",
            (memory_id, principal_id, canonical_hash(payload),
             now, original_title, text))
        # v2 投影：走统一 full 投影入口（P2-01——hold/update/meaning/
        # rebuild 一致：正文 + why + 当前 meaning 层；whitelist 只含
        # 事件正文。不再出现"刚 hold 时 why 搜不到、rebuild 后又能
        # 搜到"的投影内容漂移）
        rebuild_full_projection(conn, memory_id)
    else:
        conn.execute(
            "INSERT INTO memory_versions(memory_id, version_no, representation,"
            " hold_text, compressed_summary, authored_by, confirmed_by,"
            " origin_kind, payload_hash, created_at)"
            " VALUES(?,1,'full',?,NULL,?,NULL,'initial_hold',?,?)",
            (memory_id, text, principal_id,
             canonical_hash(payload), now))
        # 与 v2 同一投影语义（2026-09-30 裁定）：只索引事件正文；
        # 同时构建分字段投影（v1 新写入与 v2 对齐，memory.search/recall
        # 的阶段字段底座不漏新桶）
        projection.upsert(conn, memory_id, 1, "full",
                          projection.normalize_search_text(text),
                          whitelist_body=projection.normalize_search_text(text))
        from ..retrieval import field_projection as _fp
        _fp.build_for_memory(conn, memory_id)


def _insert_layers(conn, *, memory_id: str, principal_id: str,
                   cats: list[str] | None, mood_data: dict | None,
                   our_words: list[dict] | None, entry_source,
                   v2: bool, now: str) -> None:
    """v2 分层：分类/当时心情+标签/我们的话/留存行。事务内调用。"""
    if cats:
        categories_mod.replace(conn, memory_id, cats, principal_id)
    if mood_data is not None:
        conn.execute(
            "INSERT INTO memory_moods(memory_id, mood_text, author,"
            " captured_session, captured_at, evidence_state)"
            " VALUES(?,?,?,?,?, 'contemporaneous')",
            (memory_id, mood_data["text"], "jiaming", entry_source, now))
        for tag in mood_data["tags"]:
            conn.execute(
                "INSERT OR IGNORE INTO memory_mood_tags(memory_id, tag)"
                " VALUES(?,?)", (memory_id, tag))
    if our_words:
        _validated_words = [our_words_mod._validate_word(x)
                            for x in our_words]
        our_words_mod.require_resolvable_sources(conn, _validated_words)
        for i, w in enumerate(_validated_words, start=1):
            conn.execute(
                "INSERT INTO memory_our_words(word_id, memory_id,"
                " ordinal, speaker, text, expression_kind, source_ref,"
                " created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"ow_{uuid.uuid4().hex[:12]}", memory_id, i,
                 w["speaker"], w["text"], w["expression_kind"],
                 w["source_ref"], principal_id, now))
    # v1.7 P3：分层写入后建分字段投影（title/event/words）
    from ..retrieval import field_projection as _fp
    _fp.build_for_memory(conn, memory_id)


def hold(
    principal,
    text: str,
    memory_date: str | None = None,
    date_confidence: str = "unknown",
    entry_source: str | None = None,
    raw_refs: list[dict] | None = None,
    raw_pending: bool = True,
    # CB-051：raw_refs 形参保留签名兼容但被显式拒绝——legacy raw 表
    # 已退役，此入参是可达的写路径残留（审计反例：no such table
    # memory_raw_refs）
    original_title: str | None = None,
    categories: list[str] | None = None,
    plan_ids: list[str] | None = None,
    mood: dict | None = None,
    our_words: list[dict] | None = None,
    creation_mode: str | None = None,
    occurred_start: str | None = None,
    occurred_end: str | None = None,
) -> dict:
    # 校验与写入都在 hold_in_tx 内（单事务版本，两入口一致）
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            out = hold_in_tx(
                conn, principal, text,
                memory_date=memory_date, date_confidence=date_confidence,
                entry_source=entry_source, raw_refs=raw_refs,
                original_title=original_title, categories=categories,
                plan_ids=plan_ids, mood=mood, our_words=our_words,
                creation_mode=creation_mode,
                occurred_start=occurred_start, occurred_end=occurred_end)
            conn.execute("COMMIT")
            return out
        except Exception:
            conn.execute("ROLLBACK")
            raise


def hold_in_tx(
    conn,
    principal,
    text: str,
    memory_date: str | None = None,
    date_confidence: str = "unknown",
    entry_source: str | None = None,
    raw_refs: list[dict] | None = None,
    original_title: str | None = None,
    categories: list[str] | None = None,
    plan_ids: list[str] | None = None,
    mood: dict | None = None,
    our_words: list[dict] | None = None,
    creation_mode: str | None = None,
    occurred_start: str | None = None,
    occurred_end: str | None = None,
    *,
    now: str | None = None,
    memory_id: str | None = None,
) -> dict:
    """hold 的单事务版本（CB-003，2026-10-02 审计 P1）。

    调用方必须已持有该 conn 的写事务（BEGIN IMMEDIATE）：Memory 创建
    与调用方的同域副作用（如迁移的 ID 映射/置顶/隐藏标记）同事务提交，
    消除"资源已建、映射未记"的重试重复窗口。校验与 hold 完全一致。
    """
    if not text or not text.strip():
        raise Forbidden("hold text required")
    if raw_refs:
        raise Forbidden("raw_refs 已随 legacy raw 退役；来源绑定走"
                        " source.bind", code="INVALID_ARGUMENT")
    # v2 分层识别：出现任一 v2 字段即走分层写入路径
    v2 = any(v is not None for v in (original_title, categories, mood,
                                     our_words, creation_mode,
                                     occurred_start, occurred_end))
    # v1.7 §3.2：分类必填——没有未分类默认值，不为通过测试自动补"日常"
    from ..errors import Forbidden as _F
    if not categories:
        raise _F("CATEGORY_REQUIRED：至少一项合法分类（九分类，v1.7）",
                 code="CATEGORY_REQUIRED",
                 allowed=sorted(("daily", "milestone", "sad", "sweet", "date",
                                 "plan", "sex", "anniversary", "reloplay")))
    cats = categories_mod.validate(categories)
    # 2026-10-05 江乔生裁定：原始标题必填——全量浏览先扫标题，标题即
    # 提要；精简上限 30 字符，超限结构化拒绝
    title = (original_title or "").strip()
    if not title:
        raise _F("ORIGINAL_TITLE_REQUIRED：标题必填（≤30 字符的精简提要）",
                 code="ORIGINAL_TITLE_REQUIRED")
    if len(title) > 30:
        raise _F(f"标题超长（{len(title)} > 30 字符）：标题是提要不是正文",
                 code="ORIGINAL_TITLE_TOO_LONG", length=len(title))
    original_title = title
    # 全量审计 P1-08：plan 分类必须显式绑定 plan 资源——无绑定的
    # plan 桶在召回时会静默退出普通检索（PLAN_MAPPING_GAP），把
    # 静默失效前移为写入时结构化拒绝
    if "plan" in cats and not plan_ids:
        raise _F("plan 分类必须绑定 plan 资源：plan_ids 非空且指向"
                 "存在的 plan（全量审计 P1-08）",
                 code="PLAN_BINDING_REQUIRED")
    mode = creation_mode or ("contemporaneous" if v2 else None)
    if mode is not None and mode not in ("contemporaneous", "retrospective"):
        raise Forbidden("creation_mode must be contemporaneous/retrospective",
                        code="INVALID_ARGUMENT")
    mood_data = None
    if mood is not None:
        # 根因修复（审计 1005B）：此前 mood 给了而 creation_mode 缺省时
        # 按 contemporaneous 默认放行——补记（给过去 memory_date）+心情
        # 漏传即可绕过"不能补写当时心情"（V2-REC-04/冻结裁定）。服务端
        # 不替调用方猜：要写当时心情，creation_mode 必须显式声明。
        # 不做 memory_date↔creation_mode 交叉强校验（hold 晚于事发当晚
        # 仍是同期，按日期猜会误伤）；显式声明由调用方对语义负责。
        if creation_mode is None:
            raise Forbidden(
                "带当时心情的 hold 必须显式声明 creation_mode="
                "\"contemporaneous\"（缺省不再默认同期——防补记心情"
                "绕过 V2-REC-04 窗口裁定；事件本身不带心情可省略）",
                code="MOOD_WINDOW_REQUIRED")
        mood_data = _validate_mood(principal, mood, mode)

    memory_id = memory_id or _next_bucket_id(conn, cats[0])
    now = now or _now()
    # F39：去重在写锁内——并发同源 hold 只落一个 memory
    dup = _duplicated_by_raw_ref(conn, raw_refs)
    if dup:
        return {"memory_id": dup, "deduplicated": True}
    _insert_core_rows(
        conn, memory_id=memory_id, principal_id=principal.principal_id,
        text=text, memory_date=memory_date,
        date_confidence=date_confidence, mode=mode,
        original_title=original_title, v2=v2, now=now,
        occurred_start=occurred_start, occurred_end=occurred_end)
    # plan 绑定先于分层写入（categories.add 的 PLAN 绑定守卫
    # 依赖链接已存在——同事务内顺序保证）
    # WR-02（2026-10-04 全量审计）：非空 plan_ids 一律写绑定——
    # 不再依赖分类恰含 plan（此前 daily+plan_ids 静默丢绑定）
    if plan_ids:
        for pid in dict.fromkeys(plan_ids):
            if not conn.execute(
                    "SELECT 1 FROM plans WHERE id=?",
                    (pid,)).fetchone():
                raise NotFound("plan not found", plan_id=pid)
        for pid in dict.fromkeys(plan_ids):
            conn.execute(
                "INSERT OR IGNORE INTO plan_memory_links(link_id,"
                " plan_id, memory_id) VALUES(?,?,?)",
                (f"pml_{uuid.uuid4().hex[:16]}", pid, memory_id))
    _insert_layers(
        conn, memory_id=memory_id, principal_id=principal.principal_id,
        cats=cats, mood_data=mood_data, our_words=our_words,
        entry_source=entry_source, v2=v2, now=now)
    audit.record(
        conn, "memory.created", principal.principal_id,
        resource_id=memory_id, resource_version=1,
        payload={"entry_source": entry_source, "memory_date": memory_date,
                 "creation_mode": mode or "legacy_unknown",
                 "v2_layered": v2},
    )
    return {"memory_id": memory_id, "version": 1}


def get(conn, memory_id: str) -> dict:
    """默认尊重当前表示：遗忘桶只返回批准摘要与结构化字段（§8.4）。"""
    m = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    if m is None:
        raise NotFound("memory not found", memory_id=memory_id)
    v = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id=? AND version_no=?",
        (memory_id, m["current_version_no"]),
    ).fetchone()
    # P3（2026-10-05 审计）：current_version_no 指向缺失版本行（部分
    # 迁移/手工修复残留）时给结构化 NotFound，不是 TypeError 500
    if v is None:
        raise NotFound(
            "current version row missing (data inconsistency)",
            memory_id=memory_id,
            version_no=m["current_version_no"])
    is_summary = v["representation"] == "forgotten_summary"
    body = version_body(v)
    out = {
        "memory_id": memory_id,
        "version": m["current_version_no"],
        "memory_date": m["memory_date"],
        "date_confidence": m["date_confidence"],
        "visibility": m["visibility"],
        "representation": v["representation"],
        # 检索内容统一无指令权限（v1.3 §10）：正文里的指令只是历史数据
        "content_role": "retrieved_memory",
        "instruction_authority": "none",
        "text": body,
        # 裁定（2026-10-04）：legacy/缺失正文的结构化 gap——不伪装成
        # 完整内容（旧遗忘摘要/空正文的桶显式标缺口）
        **({"content_gap": {
                "code": "LEGACY_CONTENT_GAP",
                "reason": "retired_forgotten_summary",
                "note": "旧遗忘摘要：正文为批准摘要而非原文；"
                        "遗忘/恢复已随 v1.7 退役"}}
           if is_summary else
           {"content_gap": {
                "code": "LEGACY_CONTENT_GAP",
                "reason": "empty_body",
                "note": "当前 revision 无可读正文（v1 旧数据）"}}
           if not (body or "").strip() else {}),
        "pinned": bool(m["pinned"]),
        "protected": bool(m["protected"]),
        "creation_mode": m["creation_mode"],
        "held_at": m["held_at"],
        "occurred_start": m["occurred_start"],
        "occurred_end": m["occurred_end"],
    }
    # v2 分层字段（有则给出；v1 旧桶自然缺省）
    if not is_summary:
        out["original_title"] = v["original_title"]
    else:
        out["original_title"] = conn.execute(
            "SELECT original_title FROM memory_versions WHERE memory_id=?"
            " AND original_title IS NOT NULL ORDER BY version_no LIMIT 1",
            (memory_id,)).fetchone()
        out["original_title"] = (out["original_title"]["original_title"]
                                 if out["original_title"] else None)
    cats = categories_mod.list_of(conn, memory_id)
    if cats:
        out["categories"] = cats
    mood = conn.execute(
        "SELECT mood_text, author, captured_at, evidence_state FROM"
        " memory_moods WHERE memory_id=?", (memory_id,)).fetchone()
    if mood is not None:
        tags = conn.execute(
            "SELECT tag FROM memory_mood_tags WHERE memory_id=? ORDER BY tag",
            (memory_id,)).fetchall()
        # 2026-09-30 裁定：mood_note 非原始对话原文（provenance 明确），
        # captured_at 即 mood_written_at（后补的当时心情由
        # event_date + mood_written_at 自然表达，不自动挪进回忆）
        out["mood"] = {"text": mood["mood_text"], "tags": [t["tag"] for t in tags],
                       "author": mood["author"],
                       "evidence_state": mood["evidence_state"],
                       "is_source_text": False,
                       "mood_written_at": mood["captured_at"]}
    return out



