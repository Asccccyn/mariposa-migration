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
from . import retention as retention_mod
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


def _validate_mood(principal, mood: dict, creation_mode: str) -> dict:
    """当时心情资格（R05/§5.1）：仅周家明、仅同期 hold 可写。"""
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
        if t not in deduped:
            deduped.append(t)
    return {"text": text, "tags": deduped}


def _raw_ref_hash(ref: dict) -> str:
    import hashlib as _hl
    return _hl.sha256(
        f"{ref.get('conversation_id')}:{ref.get('message_from')}"
        f":{ref.get('message_to')}".encode()).hexdigest()


def _duplicated_by_raw_ref(raw_refs: list[dict] | None) -> str | None:
    """§9.3 同源重复 Hold：相同消息范围已绑定 -> 返回已有 memory_id。"""
    if not raw_refs:
        return None
    with db.formal() as conn:
        for ref in raw_refs:
            dup = conn.execute(
                "SELECT memory_id FROM memory_raw_refs WHERE source_hash=?"
                " AND bind_confidence<>'revoked'",
                (_raw_ref_hash(ref),)).fetchone()
            if dup:
                return dup["memory_id"]
    return None


def _insert_core_rows(conn, *, memory_id: str, principal_id: str, text: str,
                      why_remember, memory_date, date_confidence, mode,
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
               "why_remember": why_remember, "authored_by": principal_id}
    if v2:
        conn.execute(
            "INSERT INTO memory_versions(memory_id, version_no,"
            " representation, hold_text, compressed_summary,"
            " why_remember, authored_by, confirmed_by, origin_kind,"
            " payload_hash, created_at, original_title, event_text,"
            " schema_version)"
            " VALUES(?,1,'full',NULL,NULL,?,?,NULL,'initial_hold',?,?,"
            "?,?,2)",
            (memory_id, why_remember, principal_id, canonical_hash(payload),
             now, original_title, text))
        # v2 投影白名单：只索引事件正文；标题/心情/话语/回忆一律不进
        projection.upsert(conn, memory_id, 1, "full",
                          projection.build_full(text, None),
                          whitelist_body=projection.normalize_search_text(text))
    else:
        conn.execute(
            "INSERT INTO memory_versions(memory_id, version_no, representation,"
            " hold_text, compressed_summary, why_remember, authored_by, confirmed_by,"
            " origin_kind, payload_hash, created_at)"
            " VALUES(?,1,'full',?,NULL,?,?,NULL,'initial_hold',?,?)",
            (memory_id, text, why_remember, principal_id,
             canonical_hash(payload), now))
        projection.upsert(conn, memory_id, 1, "full",
                          projection.build_full(text, why_remember),
                          whitelist_body=projection.normalize_search_text(text))


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
        for i, w in enumerate(
                (our_words_mod._validate_word(x) for x in our_words), start=1):
            conn.execute(
                "INSERT INTO memory_our_words(word_id, memory_id,"
                " ordinal, speaker, text, expression_kind, source_ref,"
                " created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"ow_{uuid.uuid4().hex[:12]}", memory_id, i,
                 w["speaker"], w["text"], w["expression_kind"],
                 w["source_ref"], principal_id, now))
    if v2:
        # R03/D01：期限从首次 hold 时刻起算，不用事件日期/迁移时间
        retention_mod.create_for_memory(conn, memory_id, cats or [], now)


def _insert_raw_refs(conn, memory_id: str, raw_refs: list[dict],
                     now: str) -> None:
    for ref in raw_refs:
        conn.execute(
            "INSERT OR REPLACE INTO memory_raw_refs(memory_id,"
            " conversation_id, message_from, message_to, source_hash,"
            " bind_confidence, created_at) VALUES(?,?,?,?,?,'exact',?)",
            (memory_id, ref.get("conversation_id"),
             ref.get("message_from"), ref.get("message_to"),
             _raw_ref_hash(ref), now))
    conn.execute(
        "UPDATE memories SET source_state='bound' WHERE memory_id=?",
        (memory_id,))


def hold(
    principal,
    text: str,
    why_remember: str | None = None,
    memory_date: str | None = None,
    date_confidence: str = "unknown",
    entry_source: str | None = None,
    raw_refs: list[dict] | None = None,
    raw_pending: bool = True,
    original_title: str | None = None,
    categories: list[str] | None = None,
    mood: dict | None = None,
    our_words: list[dict] | None = None,
    creation_mode: str | None = None,
    occurred_start: str | None = None,
    occurred_end: str | None = None,
) -> dict:
    if not text or not text.strip():
        raise Forbidden("hold text required")
    # v2 分层识别：出现任一 v2 字段即走分层写入路径
    v2 = any(v is not None for v in (original_title, categories, mood,
                                     our_words, creation_mode,
                                     occurred_start, occurred_end))
    cats = categories_mod.validate(categories) if categories else None
    mode = creation_mode or ("contemporaneous" if v2 else None)
    if mode is not None and mode not in ("contemporaneous", "retrospective"):
        raise Forbidden("creation_mode must be contemporaneous/retrospective",
                        code="INVALID_ARGUMENT")
    mood_data = None
    if mood is not None:
        mood_data = _validate_mood(principal, mood, mode or "contemporaneous")

    dup = _duplicated_by_raw_ref(raw_refs)
    if dup:
        return {"memory_id": dup, "deduplicated": True}

    memory_id = f"mem_{uuid.uuid4().hex[:12]}"
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _insert_core_rows(
                conn, memory_id=memory_id, principal_id=principal.principal_id,
                text=text, why_remember=why_remember, memory_date=memory_date,
                date_confidence=date_confidence, mode=mode,
                original_title=original_title, v2=v2, now=now,
                occurred_start=occurred_start, occurred_end=occurred_end)
            _insert_layers(
                conn, memory_id=memory_id, principal_id=principal.principal_id,
                cats=cats, mood_data=mood_data, our_words=our_words,
                entry_source=entry_source, v2=v2, now=now)
            if raw_refs:
                _insert_raw_refs(conn, memory_id, raw_refs, now)
            audit.record(
                conn, "memory.created", principal.principal_id,
                resource_id=memory_id, resource_version=1,
                payload={"entry_source": entry_source, "memory_date": memory_date,
                         "creation_mode": mode or "legacy_unknown",
                         "v2_layered": v2},
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
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
    is_summary = v["representation"] == "forgotten_summary"
    body = v["compressed_summary"] if is_summary else (
        v["event_text"] if v["event_text"] is not None else v["hold_text"])
    out = {
        "memory_id": memory_id,
        "version": m["current_version_no"],
        "memory_date": m["memory_date"],
        "date_confidence": m["date_confidence"],
        "visibility": m["visibility"],
        "representation": v["representation"],
        "text": body,
        "why_remember": v["why_remember"] if not is_summary else None,
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
        out["mood"] = {"text": mood["mood_text"], "tags": [t["tag"] for t in tags],
                       "author": mood["author"], "evidence_state": mood["evidence_state"]}
    return out


def versions_read(conn, memory_id: str) -> list[dict]:
    """明确的按权限展开历史版本；不自动 restore，不刷新 reengagement。"""
    rows = conn.execute(
        "SELECT version_no, representation, hold_text, compressed_summary, why_remember,"
        " authored_by, origin_kind, payload_hash, created_at"
        " FROM memory_versions WHERE memory_id=? ORDER BY version_no",
        (memory_id,),
    ).fetchall()
    if not rows:
        raise NotFound("memory not found", memory_id=memory_id)
    return [dict(r) for r in rows]


def apply_forget_approval(
    conn,
    *,
    proposal_id: str,
    proposal_revision: int,
    proposal_hash: str,
    expected_memory_version: int,
    decided_by: str,
    decided_binding: str,
    payload: dict,
) -> dict:
    """在正式库事务内应用遗忘审批（§6.4 第 3-4 步）。conn 由调用方开启事务。"""
    envelope = conn.execute(
        "SELECT * FROM proposal_envelopes WHERE proposal_id=?", (proposal_id,)
    ).fetchone()
    if envelope is None:
        raise NotFound("proposal envelope not found", proposal_id=proposal_id)
    if envelope["proposal_hash"] != proposal_hash:
        raise ProposalHashMismatch(
            "proposal hash differs from submitted revision",
            submitted=envelope["proposal_hash"], provided=proposal_hash,
        )
    if conn.execute(
        "SELECT 1 FROM proposal_resolutions WHERE proposal_id=?", (proposal_id,)
    ).fetchone():
        raise ProposalAlreadyResolved("proposal already resolved", proposal_id=proposal_id)

    recomputed = canonical_hash(payload)
    if recomputed != proposal_hash:
        raise ProposalHashMismatch(
            "workspace payload no longer matches frozen hash",
            expected=proposal_hash, recomputed=recomputed,
        )

    memory_id = envelope["target_memory_id"]
    m = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    if m is None:
        raise NotFound("target memory missing", memory_id=memory_id)
    if conn.execute(
            "SELECT 1 FROM memory_retention WHERE memory_id=?",
            (memory_id,)).fetchone():
        # v2 分层桶由 v2 审查闭环独占管理（保留线索/受限审查/终裁）；
        # v1 审批应用对其关闭，防止绕过 REV-05/06/07 的线索防线
        raise Forbidden("v2 分层桶必须走 v2 审查闭环生效",
                        code="V2_MANAGED_TARGET", memory_id=memory_id)
    if m["current_version_no"] != expected_memory_version:
        raise ProposalStale(
            "base memory version moved",
            expected=expected_memory_version,
            current=m["current_version_no"],
        )
    if m["pinned"] or m["protected"] or m["anchor"]:
        raise Forbidden("memory is pinned/protected/anchor", memory_id=memory_id)

    summary = payload.get("compressed_summary")
    if not summary or not str(summary).strip():
        raise Forbidden("compressed_summary required")

    new_version = m["current_version_no"] + 1
    now = _now()
    version_payload = {
        "representation": "forgotten_summary",
        "compressed_summary": summary,
        "origin": "forget_approval",
        "proposal_id": proposal_id,
    }
    conn.execute(
        "INSERT INTO memory_versions(memory_id, version_no, representation, hold_text,"
        " compressed_summary, why_remember, authored_by, confirmed_by, origin_kind,"
        " payload_hash, created_at)"
        " VALUES(?,?, 'forgotten_summary', NULL, ?, NULL, 'worker', ?, 'forget_approval', ?, ?)",
        (memory_id, new_version, summary, decided_by, canonical_hash(version_payload), now),
    )
    conn.execute(
        "UPDATE memories SET current_version_no=?, compression_state='forgotten_summary',"
        " representation_state=representation_state+1, updated_at=? WHERE memory_id=?",
        (new_version, now, memory_id),
    )
    projection.upsert(
        conn, memory_id, new_version, "forgotten_summary",
        projection.build_forgotten(str(summary)),
        whitelist_body=projection.normalize_search_text(str(summary)),
    )
    conn.execute(
        "INSERT INTO proposal_resolutions(proposal_id, decision, decided_by, decided_binding,"
        " applied_memory_version, decided_at) VALUES(?, 'approved', ?, ?, ?, ?)",
        (proposal_id, decided_by, decided_binding, new_version, now),
    )
    audit.record(
        conn, "workspace.proposal.resolved", decided_by,
        resource_id=proposal_id,
        payload={"decision": "approved", "memory": memory_id})
    audit.record(
        conn, "memory.forgotten", decided_by,
        resource_id=memory_id, resource_version=new_version,
        initiated_by=envelope["submitted_by"],
        payload={
            "proposal_id": proposal_id,
            "representation": "forgotten_summary",
            "reversible": True,
        },
    )
    return {"memory_id": memory_id, "new_version": new_version}


def reject_or_withdraw(
    conn, proposal_id: str, decision: str, decided_by: str, decided_binding: str
) -> None:
    if conn.execute(
        "SELECT 1 FROM proposal_resolutions WHERE proposal_id=?", (proposal_id,)
    ).fetchone():
        raise ProposalAlreadyResolved("proposal already resolved", proposal_id=proposal_id)
    conn.execute(
        "INSERT INTO proposal_resolutions(proposal_id, decision, decided_by, decided_binding,"
        " applied_memory_version, decided_at) VALUES(?,?,?, ?,NULL,?)",
        (proposal_id, decision, decided_by, decided_binding, _now()),
    )
    audit.record(
        conn, "workspace.proposal.resolved", decided_by,
        resource_id=proposal_id, payload={"decision": decision})
    audit.record(
        conn, f"workspace.proposal.{decision}", decided_by,
        resource_id=proposal_id,
        payload={"decision": decision},
    )


def restore(
    principal,
    memory_id: str,
    expected_current_version: int,
    target_history_version: int | None = None,
) -> dict:
    """恢复：默认回到最近一次压缩前的 full 版本；新版本号，不改写旧笔迹。"""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            m = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if m is None:
                raise NotFound("memory not found", memory_id=memory_id)
            if m["current_version_no"] != expected_current_version:
                raise VersionConflict(
                    "memory version moved",
                    expected=expected_current_version, current=m["current_version_no"]
                )
            rows = conn.execute(
                "SELECT * FROM memory_versions WHERE memory_id=? ORDER BY version_no",
                (memory_id,),
            ).fetchall()
            source = None
            if target_history_version is not None:
                source = next(
                    (r for r in rows if r["version_no"] == target_history_version), None
                )
                if source is None:
                    raise NotFound("target history version not found",
                                   memory_id=memory_id, version=target_history_version)
            else:
                source = next(
                    (r for r in reversed(rows) if r["representation"] == "full"), None
                )
            if source is None:
                raise NotFound("no full version to restore to", memory_id=memory_id)

            new_version = m["current_version_no"] + 1
            now = _now()
            payload = {
                "representation": "full",
                "hold_text": source["hold_text"],
                "why_remember": source["why_remember"],
                "restored_from_version": source["version_no"],
            }
            conn.execute(
                "INSERT INTO memory_versions(memory_id, version_no, representation,"
                " hold_text, compressed_summary, why_remember, authored_by, confirmed_by,"
                " origin_kind, payload_hash, created_at)"
                " VALUES(?,?,'full',?,NULL,?,? ,?,'restore',?,?)",
                (
                    memory_id, new_version, source["hold_text"], source["why_remember"],
                    source["authored_by"], principal.principal_id, canonical_hash(payload), now,
                ),
            )
            conn.execute(
                "UPDATE memories SET current_version_no=?, compression_state='full',"
                " representation_state=representation_state+1, updated_at=?"
                " WHERE memory_id=?",
                (new_version, now, memory_id),
            )
            projection.upsert(
                conn, memory_id, new_version, "full",
                projection.build_full(source["hold_text"] or "", source["why_remember"]),
                whitelist_body=projection.normalize_search_text(
                    source["hold_text"] or ""),
            )
            audit.record(
                conn, "memory.restored", principal.principal_id,
                resource_id=memory_id, resource_version=new_version,
                payload={"restored_from_version": source["version_no"]},
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "new_version": new_version}
