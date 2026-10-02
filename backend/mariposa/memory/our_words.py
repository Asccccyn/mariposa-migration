"""我们的话（spec_v2 R08）：桶内双方话语，按说话人与顺序保存。

可原话（verbatim）可概括（paraphrase），不参与普通召回索引；
来源匹配不得自动改写其表达。说话人固定真实两主体。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound

SPEAKERS = ("jiaming", "qiaosheng")
EXPRESSION_KINDS = ("verbatim", "paraphrase", "unspecified")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_word(w: dict) -> dict:
    if not isinstance(w, dict):
        raise Forbidden("each word must be an object", code="INVALID_ARGUMENT")
    speaker = w.get("speaker")
    if speaker not in SPEAKERS:
        raise Forbidden("speaker must be jiaming or qiaosheng",
                        code="INVALID_ARGUMENT", speaker=speaker)
    text = w.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Forbidden("word text required", code="INVALID_ARGUMENT")
    kind = w.get("expression_kind", "unspecified")
    if kind not in EXPRESSION_KINDS:
        raise Forbidden("expression_kind must be verbatim/paraphrase/unspecified",
                        code="INVALID_ARGUMENT", expression_kind=kind)
    return {"speaker": speaker, "text": text.strip(), "expression_kind": kind,
            "source_ref": w.get("source_ref")}


def append(principal_id: str, memory_id: str, words: list[dict]) -> dict:
    """追加话语（hold 内联写入或事后追加；顺序按本次传入相对顺序续排）。"""
    if not isinstance(words, list) or not words:
        raise Forbidden("words must be a non-empty list", code="INVALID_ARGUMENT")
    validated = [_validate_word(w) for w in words]
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # CB-037：目标存在性检查在写锁内——事务外旧快照检查会让
            # 并发删除竞态变成未结构化 FK IntegrityError；写锁内重查
            # 得到结构化 NotFound
            if not conn.execute(
                    "SELECT 1 FROM memories WHERE memory_id=?",
                    (memory_id,)).fetchone():
                raise NotFound("memory not found", memory_id=memory_id)
            row = conn.execute(
                "SELECT COALESCE(MAX(ordinal),0) AS m FROM memory_our_words"
                " WHERE memory_id=?", (memory_id,)).fetchone()
            start = row["m"]
            ids = []
            for i, w in enumerate(validated, start=1):
                wid = f"ow_{uuid.uuid4().hex[:12]}"
                ids.append(wid)
                conn.execute(
                    "INSERT INTO memory_our_words(word_id, memory_id, ordinal,"
                    " speaker, text, expression_kind, source_ref, created_by,"
                    " created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (wid, memory_id, start + i, w["speaker"], w["text"],
                     w["expression_kind"], w["source_ref"], principal_id, now))
            audit.record(conn, "memory.our_words.appended", principal_id,
                         resource_id=memory_id, payload={"count": len(ids)})
            # v1.7 F5：话语变更后同事务刷新分字段投影
            from ..retrieval import field_projection as _fp
            _fp.build_for_memory(conn, memory_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "word_ids": ids,
            "next_ordinal": start + len(validated)}


def list_for(memory_id: str) -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT word_id, ordinal, speaker, text, expression_kind,"
            " source_ref, created_by, created_at FROM memory_our_words"
            " WHERE memory_id=? ORDER BY ordinal", (memory_id,)).fetchall()
    # 话语正文属检索内容：content_role/instruction_authority（v1.3 §10）
    return [dict(r, content_role="retrieved_memory",
                 instruction_authority="none") for r in rows]


def has_shared_expression(rows: list[dict]) -> list[dict]:
    """共同话语线索（R14/D06）：同一句双方都说（规范化后相同）。

    只标疑似（needs_jiaming_decision），终局由周家明决定。
    "一方说、另一方明确接住/回应"的语义判定需要真实语义匹配评测，
    当前只做字面同一句检测（见 DECISIONS：暂不做接应推断）。
    """
    import re

    def norm(t: str) -> str:
        return re.sub(r"[\s，。！？.!?,~～]", "", t).lower()

    by_norm: dict[str, set[str]] = {}
    for r in rows:
        by_norm.setdefault(norm(r["text"]), set()).add(r["speaker"])
    hints = []
    for text, speakers in by_norm.items():
        if len(speakers) > 1 and text:
            hints.append({"kind": "shared_expression",
                          "normalized": text[:40],
                          "speakers": sorted(speakers)})
    return hints


def source_of(word_id: str) -> str | None:
    """话语当前来源引用（routing 反查用）。"""
    from .. import db
    with db.formal() as conn:
        row = conn.execute(
            "SELECT source_ref FROM memory_our_words WHERE word_id=?",
            (word_id,)).fetchone()
    return (row["source_ref"] or None) if row else None


def correct_source(principal_id: str, word_id: str,
                   expected_source_ref: str | None,
                   correction_action: str, replacement: dict | None = None,
                   note: str | None = None, conn=None,
                   expected_source_version: int | None = None) -> dict:
    """话语来源纠错（§P-R02/§5.5 Word）。

    预期来源指纹（expected_source_ref）与换代计数
    （expected_source_version）必须与当前值一致——防并发改错对象；
    撤销=置空来源（旧 verified 证据/指纹随之失效），改绑=新来源经
    现行 Source 身份校验。不可经普通正文更新绕开。

    CB-007（2026-10-02 审计 P1）：word_id 标识话语而非来源关系实例，
    仅比 source_ref 无法识别 A→(撤销)→A 的实例换代——旧请求会删掉
    新绑定。版本必填（None 拒绝）：CAS 精确指向一代绑定，每次纠错
    版本+1 并记入纠错历史。
    """
    from .. import db as _db
    from ..errors import Forbidden, NotFound
    from ..relations.corrections import record_correction

    if correction_action not in ("remove_wrong_binding",
                                 "replace_wrong_binding"):
        raise Forbidden("correction_action must be remove_wrong_binding/"
                        "replace_wrong_binding")
    # CB-006：与另四域一致——remove 禁带 replacement，replace 必带
    if correction_action == "remove_wrong_binding" and replacement:
        raise Forbidden("remove_wrong_binding 不接受 replacement（撤销"
                        "语义；改绑请用 replace_wrong_binding）",
                        code="INVALID_ARGUMENT")
    if correction_action == "replace_wrong_binding" and not (
            replacement and replacement.get("source_ref")):
        raise Forbidden("replace_wrong_binding 必须携带 replacement"
                        ".source_ref——缺新来源的替换即撤销",
                        code="INVALID_ARGUMENT")
    if expected_source_version is None:
        raise Forbidden("expected_source_version 必填（来源绑定换代"
                        "计数，随纠错返回值递增）——仅比 source_ref 无法"
                        "识别换代实例", code="INVALID_ARGUMENT")

    def _do(conn):
        row = conn.execute(
            "SELECT * FROM memory_our_words WHERE word_id=?",
            (word_id,)).fetchone()
        if row is None:
            raise NotFound("word not found", word_id=word_id)
        cur = row["source_ref"] or None
        cur_version = int(row["source_binding_version"] or 0)
        if ((expected_source_ref or None) != cur
                or int(expected_source_version) != cur_version):
            raise Forbidden(
                "expected_source_ref/expected_source_version 与当前"
                "来源不一致（并发/换代保护）",
                code="CONFLICT", current=cur,
                current_source_binding_version=cur_version)
        new_ref = None
        if replacement and replacement.get("source_ref"):
            new_ref = str(replacement["source_ref"])
            if not new_ref.startswith("source_msg:"):
                raise Forbidden(
                    "来源必须是现行 source_msg:<id> 身份（旧 raw 前缀"
                    "已退役）", code="INVALID_ARGUMENT")
            msg_id = new_ref[len("source_msg:"):]
            if not conn.execute(
                "SELECT 1 FROM source_messages WHERE id=? OR"
                " provider_message_id=?", (msg_id, msg_id)).fetchone():
                raise NotFound("source message not found", ref=new_ref)
        new_version = cur_version + 1
        cid = record_correction(
            conn, domain="word_source",
            original_instance_id=f"word:{word_id}",
            endpoint_a=row["memory_id"], endpoint_b=cur or "",
            original_meta={"source_ref": cur,
                           "expression_kind": row["expression_kind"],
                           "source_binding_version": cur_version},
            original_created_by=row["created_by"],
            original_created_at=row["created_at"],
            corrected_by=principal_id, note=note,
            replacement_instance_id=(f"word:{word_id}:{new_ref}"
                                     if new_ref else None))
        conn.execute(
            "UPDATE memory_our_words SET source_ref=?,"
            " source_binding_version=? WHERE word_id=?",
            (new_ref, new_version, word_id))
        # 证据指纹失效：word 向量/证据按指纹重算（retrieval 侧消费）
        return {"correction_id": cid, "word_id": word_id,
                "removed_source_ref": cur, "new_source_ref": new_ref,
                "source_binding_version": new_version,
                "action": correction_action}

    if conn is not None:
        return _do(conn)
    with _db.formal() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            out = _do(c)
            c.execute("COMMIT")
            return out
        except Exception:
            c.execute("ROLLBACK")
            raise
