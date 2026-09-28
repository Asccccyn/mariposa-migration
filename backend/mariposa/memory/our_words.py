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
        if not conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                            (memory_id,)).fetchone():
            raise NotFound("memory not found", memory_id=memory_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
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
