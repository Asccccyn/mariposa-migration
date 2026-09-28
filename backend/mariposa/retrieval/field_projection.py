"""分字段投影（v1.7 §3.1）：event / title / our_words 各自独立派生索引。

- 一个字段一个投影行（field_search_docs + field_fts）；禁止整桶拼接，
  否则 MID/CORE 无法可靠去掉 title/words，mood_text 也会旁路进入。
- mood_text、回忆、keep 理由永不进入任何投影（字段来源隔离）。
- 生成与重建幂等：同内容 hash 不变不重写。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .. import config
from . import projection

PROJECTION_VERSION = "field_projection_v1_7"
FIELD_KINDS = ("original_title", "event_text", "our_words")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: str) -> str:
    return projection.normalize_search_text(text)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _current_texts(conn, memory_id: str) -> dict[str, str]:
    """当前表示的三类字段原文（空字符串=该字段无内容，投影行不建）。"""
    m = conn.execute(
        "SELECT current_version_no FROM memories WHERE memory_id=?",
        (memory_id,)).fetchone()
    if m is None:
        return {}
    v = conn.execute(
        "SELECT original_title, event_text, hold_text FROM memory_versions"
        " WHERE memory_id=? AND version_no=?",
        (memory_id, m["current_version_no"])).fetchone()
    out: dict[str, str] = {}
    if v is not None:
        out["original_title"] = (v["original_title"] or "").strip()
        # event_text 优先；旧 v1 版本无 event_text 时用 hold_text 作事件正文
        out["event_text"] = (v["event_text"] or v["hold_text"] or "").strip()
    words = [r["text"] for r in conn.execute(
        "SELECT text FROM memory_our_words WHERE memory_id=? ORDER BY ordinal",
        (memory_id,))]
    if words:
        out["our_words"] = "\n".join(words)
    return out


def build_for_memory(conn, memory_id: str) -> int:
    """为单桶重建字段投影（同内容不重写）；返回行数。"""
    texts = _current_texts(conn, memory_id)
    existing = {r["field_kind"]: r["text_hash"] for r in conn.execute(
        "SELECT field_kind, text_hash FROM field_search_docs"
        " WHERE memory_id=?", (memory_id,))}
    wanted = {}
    for kind, text in texts.items():
        if text:
            wanted[kind] = text
    # 删除不再存在/为空的字段投影
    for kind in list(existing):
        if kind not in wanted:
            conn.execute(
                "DELETE FROM field_search_docs WHERE memory_id=?"
                " AND field_kind=?", (memory_id, kind))
            _fts_delete(conn, memory_id, kind)
    n = 0
    for kind, text in wanted.items():
        h = _hash(text)
        if existing.get(kind) == h:
            n += 1
            continue
        _fts_delete(conn, memory_id, kind)
        conn.execute(
            "INSERT OR REPLACE INTO field_search_docs(memory_id, field_kind,"
            " text_norm, text_hash, projection_version, built_at)"
            " VALUES(?,?,?,?,?,?)",
            (memory_id, kind, _norm(text), h, PROJECTION_VERSION, _now()))
        conn.execute(
            "INSERT INTO field_fts(memory_id, field_kind, text_norm)"
            " VALUES(?,?,?)", (memory_id, kind, _norm(text)))
        n += 1
    return n


def _fts_delete(conn, memory_id: str, kind: str) -> None:
    conn.execute(
        "DELETE FROM field_fts WHERE memory_id=? AND field_kind=?",
        (memory_id, kind))


def remove(conn, memory_id: str) -> None:
    conn.execute("DELETE FROM field_search_docs WHERE memory_id=?",
                 (memory_id,))
    conn.execute("DELETE FROM field_fts WHERE memory_id=?", (memory_id,))


def rebuild_all(batch: int = 500) -> dict:
    """全量重建（游标分批，不 fetchall 全表）。"""
    from .. import db
    n = 0
    last = ""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM field_fts")
            conn.execute("DELETE FROM field_search_docs")
            while True:
                rows = conn.execute(
                    "SELECT memory_id FROM memories WHERE memory_id > ?"
                    " ORDER BY memory_id LIMIT ?", (last, batch)).fetchall()
                if not rows:
                    break
                for r in rows:
                    n += build_for_memory(conn, r["memory_id"])
                last = rows[-1]["memory_id"]
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"rebuilt": n, "projection_version": PROJECTION_VERSION}


def stage_filter_kinds(stage_fields) -> list[str]:
    """阶段 AllowedFields（phase_policy FIELD_SETS）→ field_kind 列表。"""
    kinds = []
    if "original_title" in stage_fields:
        kinds.append("original_title")
    if "event_text" in stage_fields:
        kinds.append("event_text")
    if "our_words" in stage_fields:
        kinds.append("our_words")
    return kinds
