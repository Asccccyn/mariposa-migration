"""记忆扩展能力：update（正文/标题/日期修订，§17.3）。

2026-10-13 她裁定退役：pin/protect/anchor 旗标（遗忘机制 v1.7 退役后
无消费方的死开关）整段删除——死代码是多轮审计的方法盲区（只有规格
→代码对照，没有代码→规格反查），本批清理见 registry 同日注释。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, config, db
from ..errors import Forbidden, NotFound, VersionConflict
from . import service as memory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_UNSET = object()  # 字段缺席哨兵（WR-04：缺席≠显式 null 清空）


def update_text(principal_id: str, memory_id: str, expected_version: int,
                text: str | None = None,
                memory_date=_UNSET, date_confidence=_UNSET,
                original_title=_UNSET, categories=_UNSET,
                our_words_add: list[dict] | None = None,
                our_words_revise: list[dict] | None = None) -> dict:
    """一次编辑在同一版本校验和事务内更新正文、日期、标题、分类与
    我们的话。

    2026-10-18 她裁定：修订不留历史版本——就地覆写当前版本行（CAS 锁
    保留、审计保留）。版本计数器仍递增：它是内容换代信号（查看票据/
    回忆的防串改 CAS 依赖，F01），不是历史档案。缺席字段保留现值；
    memory_date 显式 null 清空。分类仍走同一领域校验（非空、plan 绑定
    硬门），失败不留下半次修订。

    我们的话（同日裁定）：话语只存原文，说话人不许手填——一律由
    source_ref 指向的已发布消息推导（其 speaker），且 text 必须是该
    消息正文的连续片段（服务端逐字保证）。新增/修改共用此门。
    """
    from . import our_words as ow_mod
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            m = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                             (memory_id,)).fetchone()
            if m is None:
                raise NotFound("memory not found", memory_id=memory_id)
            if m["current_version_no"] != expected_version:
                raise VersionConflict("memory version moved",
                                      expected=expected_version,
                                      current=m["current_version_no"])
            v = conn.execute(
                "SELECT * FROM memory_versions WHERE memory_id=? AND version_no=?",
                (memory_id, expected_version)).fetchone()
            if text is not None:
                new_text = None if v["event_text"] is not None else text
            else:
                new_text = None if v["event_text"] is not None else v["hold_text"]
            if m["compression_state"] == "forgotten_summary":
                raise Forbidden(
                    "历史摘要存在正文缺口，不能作为完整正文原地修订；"
                    "请先核对离线迁移资料", code="LEGACY_CONTENT_GAP")
            if categories is not _UNSET:
                from . import categories as category_service
                category_service.replace(conn, memory_id, categories,
                                         principal_id)
            now = _now()
            # A01 + F04：未提供新正文时，经统一入口延续当前 revision 正文
            #（event_text 优先，v1 旧数据回退 hold_text）
            new_event = text if text is not None else memory.version_body(v)
            new_title = v["original_title"]
            if original_title is not _UNSET:
                t = str(original_title).strip()
                if not t:
                    raise Forbidden("original_title 不可修订为空",
                                    code="ORIGINAL_TITLE_REQUIRED")
                if len(t) > 30:
                    raise Forbidden(
                        f"标题超长（{len(t)} > 30 字符）",
                        code="ORIGINAL_TITLE_TOO_LONG", length=len(t))
                new_title = t
            old_schema = v["schema_version"] or 1
            # N01：hash 输入与 canonical 正文字段一致——v2 正文在
            # event_text，只 hash hold_text(NULL) 会让不同正文同指纹
            payload = {"representation": "full", "hold_text": new_text,
                       "event_text": new_event, "origin": "update",
                       "original_title": new_title}
            # 不留档（2026-10-18 她裁定）：就地覆写当前版本行，不插入
            # 历史版本。版本计数器仍 +1——它是内容换代信号（查看票据/
            # 回忆 CAS 的防串改依赖它，F01），不是历史档案。
            new_version = expected_version + 1
            conn.execute(
                "UPDATE memory_versions SET version_no=?, hold_text=?,"
                " original_title=?, payload_hash=?, event_text=?,"
                " created_at=? WHERE memory_id=? AND version_no=?",
                (new_version, new_text, new_title,
                 memory.canonical_hash(payload), new_event, now,
                 memory_id, expected_version))
            conn.execute(
                "UPDATE memories SET current_version_no=?, memory_date=?,"
                " date_confidence=?, updated_at=? WHERE memory_id=?",
                (new_version,
                 m["memory_date"] if memory_date is _UNSET else memory_date,
                 ("unknown" if memory_date is None else m["date_confidence"])
                 if date_confidence is _UNSET else date_confidence,
                 now, memory_id))
            word_ids = _apply_word_ops(conn, ow_mod, principal_id, memory_id,
                                       our_words_add, our_words_revise, now)
            from ..retrieval import field_projection as _fp
            _fp.build_for_memory(conn, memory_id)
            memory.rebuild_full_projection(conn, memory_id)
            audit.record(conn, "memory.updated", principal_id,
                         resource_id=memory_id,
                         resource_version=new_version,
                         payload={"title_changed": original_title is not _UNSET
                                  and new_title != v["original_title"],
                                  "text_changed": text is not None,
                                  "date_changed": memory_date is not _UNSET,
                                  "categories_changed": categories is not _UNSET,
                                  "words_added": len(our_words_add or []),
                                  "words_revised": len(our_words_revise or []),
                                  "in_place": True})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "version": new_version,
            "word_ids": word_ids}


def _derive_word(conn, ow_mod, spec: dict, field: str) -> dict:
    """话语录入统一门：说话人跟随出处 + 原文连续片段核验。

    spec 必须带 source_ref（source_msg:<已发布消息>）与 text；
    返回 {speaker, text, source_ref}（speaker 来自来源消息行）。
    """
    ref = spec.get("source_ref")
    text = spec.get("text")
    if not isinstance(ref, str) or not ref.startswith("source_msg:"):
        raise Forbidden(
            f"{field} 每条必须带 source_ref（source_msg:<id>）——"
            "话语只存原文，说话人与内容由出处决定，不接受无出处录入",
            code="INVALID_SOURCE_REF")
    if not isinstance(text, str) or not text.strip():
        raise Forbidden(f"{field}.text required", code="INVALID_ARGUMENT")
    msg_id = ref[len("source_msg:"):]
    rows = conn.execute(
        "SELECT speaker, text, published FROM source_messages"
        " WHERE (id=? OR provider_message_id=?)", (msg_id, msg_id)).fetchall()
    if len(rows) > 1:
        raise Forbidden("来源撞号（多 provider 同 ID），拒绝猜测绑定",
                        code="SOURCE_MESSAGE_AMBIGUOUS", got=ref)
    if not rows or not rows[0]["published"]:
        raise Forbidden("来源消息不存在或未发布", code="INVALID_SOURCE_REF",
                        got=ref)
    src = rows[0]
    if src["speaker"] not in ow_mod.SPEAKERS:
        raise Forbidden("来源消息缺少可判定的说话人", code="INVALID_SOURCE_REF",
                        got=ref)
    if text.strip() not in (src["text"] or ""):
        raise Forbidden(
            "话语必须是来源消息正文的连续片段（一字不差）——"
            "只能存原文，不接受改写", code="WORD_NOT_VERBATIM_FRAGMENT")
    return {"speaker": src["speaker"], "text": text.strip(),
            "source_ref": ref}


def _apply_word_ops(conn, ow_mod, principal_id: str, memory_id: str,
                    adds, revises, now: str) -> list[str]:
    """在 update 事务内执行话语新增/修改；返回新建 word_id 列表。"""
    ids: list[str] = []
    if adds:
        validated = [_derive_word(conn, ow_mod, a, "our_words_add")
                     for a in adds]
        row = conn.execute(
            "SELECT COALESCE(MAX(ordinal),0) AS mx FROM memory_our_words"
            " WHERE memory_id=?", (memory_id,)).fetchone()
        start = row["mx"]
        for i, w in enumerate(validated, start=1):
            wid = f"ow_{uuid.uuid4().hex[:12]}"
            ids.append(wid)
            conn.execute(
                "INSERT INTO memory_our_words(word_id, memory_id, ordinal,"
                " speaker, text, source_ref, created_by, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (wid, memory_id, start + i, w["speaker"], w["text"],
                 w["source_ref"], principal_id, now))
    if revises:
        for r in revises:
            wid = str(r.get("word_id", ""))
            cur = conn.execute(
                "SELECT * FROM memory_our_words WHERE word_id=?",
                (wid,)).fetchone()
            if cur is None:
                raise NotFound("word not found", word_id=wid)
            if cur["memory_id"] != memory_id:
                raise Forbidden("话语属于其他记忆", code="INVALID_ARGUMENT",
                                word_id=wid)
            w = _derive_word(conn, ow_mod, r, "our_words_revise")
            conn.execute(
                "UPDATE memory_our_words SET speaker=?, text=?, source_ref=?,"
                " source_binding_version=source_binding_version+1"
                " WHERE word_id=?",
                (w["speaker"], w["text"], w["source_ref"], wid))
    if adds or revises:
        from ..retrieval import words as words_mod
        words_mod.rebuild_words_index(conn)
    return ids


def update_mood(principal_id: str, memory_id: str, mood: dict) -> dict:
    """当时心情单独修订（2026-10-12）：不推进正文版本行。

    八栏口径：当时心情=感受+为什么记得+想说没说+标签。合同校验复用
    hold 时的 _validate_mood——仅 jiaming、仅 contemporaneous 桶（
    V2-REC-09/04）。已有心情行覆盖更新（mood 是当前态不是历史栈，
    修订留审计不留版本行）；标签整组重建。

    临时豁免（她 2026-10-14 要的"整理期可删补丁"）：MARIPOSA_MOOD_EDIT_OVERRIDE
    开时允许 qiaosheng 写心情（她整理记忆库期自用）。心情行 author 诚实
    记真实写入者，审计带 override 标注。撤销=删该环境变量并重启，
    合同即恢复 V2-REC-09 原样。
    """
    override = (principal_id == "qiaosheng"
                and config._flag("MARIPOSA_MOOD_EDIT_OVERRIDE"))
    if principal_id != "jiaming" and not override:
        raise Forbidden(
            "当时心情只能由char（jiaming）修订（V2-REC-09）；整理期"
            "豁免口=MARIPOSA_MOOD_EDIT_OVERRIDE（她 2026-10-14 临时批）",
            code="MOOD_AUTHOR_REQUIRED")
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            m = conn.execute("SELECT creation_mode FROM memories"
                             " WHERE memory_id=?",
                             (memory_id,)).fetchone()
            if m is None:
                raise NotFound("memory not found", memory_id=memory_id)
            from ..identity import Principal as _Principal
            mood_data = memory._validate_mood(
                _Principal(principal_id="jiaming" if override
                           else principal_id,
                           display_name=principal_id,
                           kind="ai", entry_source="web", binding_id=""),
                mood if isinstance(mood, dict) else {}, m["creation_mode"])
            now = memory._now()
            exists = conn.execute(
                "SELECT 1 FROM memory_moods WHERE memory_id=?",
                (memory_id,)).fetchone()
            # 表 CHECK 约束 author IN ('jiaming')——心情的合同归属主体恒为
            # char；豁免期的真实操作者由审计（principal+override）与
            # captured_session=web_override 承载，不在 author 列冒写
            if exists:
                conn.execute(
                    "UPDATE memory_moods SET mood_text=?,"
                    " captured_at=? WHERE memory_id=?",
                    (mood_data["text"], now, memory_id))
            else:
                conn.execute(
                    "INSERT INTO memory_moods(memory_id, mood_text, author,"
                    " captured_session, captured_at, evidence_state)"
                    " VALUES(?,?, 'jiaming', ?, ?, 'contemporaneous')",
                    (memory_id, mood_data["text"],
                     "web_override" if override else "web", now))
            conn.execute("DELETE FROM memory_mood_tags WHERE memory_id=?",
                         (memory_id,))
            for tag in mood_data["tags"]:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_mood_tags(memory_id, tag)"
                    " VALUES(?,?)", (memory_id, tag))
            audit.record(conn, "memory.mood_updated", principal_id,
                         resource_id=memory_id,
                         payload={"tags": mood_data["tags"],
                                  "override": override or None})
            conn.execute("COMMIT")
            return {"memory_id": memory_id, "mood": mood_data}
        except Exception:
            conn.execute("ROLLBACK")
            raise


