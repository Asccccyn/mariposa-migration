"""raw 原文层（§9）：导入幂等、消息去重、30 条开窗、独立原文查询。

原文不进入 memory 的全文/向量索引；raw.search 是独立能力，结果标明来源。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound
from ..retrieval import projection

VALID_ROLES = {"user", "assistant", "tool", "system"}
BOOT_RAW_MESSAGES = 30  # 计消息，不是轮（§12.1 已定）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def import_payload(principal_id: str, payload: dict) -> dict:
    """合成/导出导入：{source_channel, external_id, messages:[{source_message_id,
    role, body, occurred_at, sequence?, speaker_id?}]}。

    幂等：同 (source_channel, external_id) 已存在时，只增量插入新消息 ID；
    已存在的消息 ID 跳过，不覆盖原文。
    """
    channel = str(payload.get("source_channel", "")).strip()
    external = str(payload.get("external_id", "")).strip()
    messages = payload.get("messages") or []
    if not channel or not external:
        raise Forbidden("source_channel and external_id required")
    if not isinstance(messages, list):
        raise Forbidden("messages must be a list")

    conv_id = f"conv_{uuid.uuid4().hex[:12]}"
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT id FROM raw_conversations WHERE source_channel=? AND external_id=?",
                (channel, external),
            ).fetchone()
            if existing:
                conv_id = existing["id"]
            else:
                bounds = [
                    m.get("occurred_at") for m in messages if m.get("occurred_at")
                ]
                conn.execute(
                    "INSERT INTO raw_conversations(id, source_channel, external_id,"
                    " started_at, ended_at, coverage, created_at) VALUES(?,?,?,?,?,?,?)",
                    (conv_id, channel, external,
                     min(bounds) if bounds else None,
                     max(bounds) if bounds else None,
                     str(payload.get("coverage", "partial")), _now()),
                )
            inserted = 0
            skipped = 0
            for m in messages:
                role = str(m.get("role", ""))
                if role not in VALID_ROLES:
                    raise Forbidden(f"invalid role: {role}")
                smid = str(m.get("source_message_id", "")).strip()
                body = m.get("body")
                occurred = m.get("occurred_at")
                if not smid or body is None or not occurred:
                    raise Forbidden("source_message_id/body/occurred_at required")
                dup = conn.execute(
                    "SELECT 1 FROM raw_messages WHERE conversation_id=? AND source_message_id=?",
                    (conv_id, smid),
                ).fetchone()
                if dup:
                    skipped += 1
                    continue
                conn.execute(
                    "INSERT INTO raw_messages(id, conversation_id, source_message_id, role,"
                    " speaker_id, body, occurred_at, sequence, provenance)"
                    " VALUES(?,?,?,?,?,?,?,?, 'import')",
                    (f"rm_{uuid.uuid4().hex[:12]}", conv_id, smid, role,
                     m.get("speaker_id"), str(body), occurred,
                     int(m.get("sequence", 0))),
                )
                inserted += 1
            audit.record(
                conn, "raw.import.completed", principal_id,
                resource_id=conv_id,
                payload={"inserted": inserted, "skipped_duplicate": skipped},
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"conversation_id": conv_id, "inserted": inserted,
            "skipped_duplicate": skipped}


def list_recent(limit: int = BOOT_RAW_MESSAGES, before: str | None = None) -> list[dict]:
    """最新 N 条真实已收录消息（按时间+sequence 稳定排序）。"""
    limit = max(1, min(int(limit), 200))
    with db.formal() as conn:
        if before:
            rows = conn.execute(
                "SELECT rm.*, rc.source_channel FROM raw_messages rm"
                " JOIN raw_conversations rc ON rc.id = rm.conversation_id"
                " WHERE rm.occurred_at < ? ORDER BY rm.occurred_at DESC, rm.sequence DESC,"
                " rm.id DESC LIMIT ?",
                (before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT rm.*, rc.source_channel FROM raw_messages rm"
                " JOIN raw_conversations rc ON rc.id = rm.conversation_id"
                " ORDER BY rm.occurred_at DESC, rm.sequence DESC, rm.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    out = [dict(r) for r in rows]
    out.reverse()  # 时间正序展示
    return out


def search(query: str, limit: int = 20) -> dict:
    """独立原文查询：不走 memory 投影，命中标 source=raw。"""
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT rm.id, rm.conversation_id, rm.role, rm.body, rm.occurred_at,"
            " rc.source_channel FROM raw_messages rm"
            " JOIN raw_conversations rc ON rc.id = rm.conversation_id"
            " ORDER BY rm.occurred_at DESC, rm.sequence DESC LIMIT 500",
        ).fetchall()
    phrase_tokens = [t.lower() for t in projection.tokenize(query)]
    if not phrase_tokens:
        return {"hits": [], "source": "raw"}
    joined = " ".join(phrase_tokens)
    hits = []
    for r in rows:
        normalized = projection.normalize_search_text(r["body"])
        if joined in normalized:
            hits.append({
                "message_id": r["id"], "conversation_id": r["conversation_id"],
                "role": r["role"], "body": r["body"],
                "occurred_at": r["occurred_at"], "source_channel": r["source_channel"],
                "matched_by": "raw_keyword", "source": "raw",
            })
            if len(hits) >= limit:
                break
    return {"hits": hits, "source": "raw"}


def conversations_list(limit: int = 50) -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT * FROM raw_conversations ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
