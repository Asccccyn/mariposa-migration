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

_APPROVERS = {"qiaosheng", "jiaming"}  # 工具人无审批权（§6.3）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def hold(
    principal,
    text: str,
    why_remember: str | None = None,
    memory_date: str | None = None,
    date_confidence: str = "unknown",
    entry_source: str | None = None,
    raw_refs: list[dict] | None = None,
    raw_pending: bool = True,
) -> dict:
    if not text or not text.strip():
        raise Forbidden("hold text required")
    # §9.3 同源重复 Hold：相同消息范围已绑定 -> 返回已有记录，不新建
    import hashlib as _hl
    if raw_refs:
        with db.formal() as conn:
            for ref in raw_refs:
                src_hash = _hl.sha256(
                    f"{ref.get('conversation_id')}:{ref.get('message_from')}"
                    f":{ref.get('message_to')}".encode()).hexdigest()
                dup = conn.execute(
                    "SELECT memory_id FROM memory_raw_refs WHERE source_hash=?"
                    " AND bind_confidence<>'revoked'", (src_hash,)).fetchone()
                if dup:
                    return {"memory_id": dup["memory_id"],
                            "deduplicated": True}
    memory_id = f"mem_{uuid.uuid4().hex[:12]}"
    payload = {
        "representation": "full",
        "hold_text": text,
        "why_remember": why_remember,
        "authored_by": principal.principal_id,
    }
    initial_source_state = "bound" if (raw_refs and not raw_pending)         else ("raw_pending" if raw_pending else "raw_pending")
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO memories(memory_id, current_version_no, memory_date,"
                " date_confidence, visibility, compression_state, created_at, updated_at)"
                " VALUES(?,?,?,?, 'active','full', ?, ?)",
                (memory_id, 1, memory_date, date_confidence, now, now),
            )
            conn.execute(
                "INSERT INTO memory_versions(memory_id, version_no, representation,"
                " hold_text, compressed_summary, why_remember, authored_by, confirmed_by,"
                " origin_kind, payload_hash, created_at)"
                " VALUES(?,1,'full',?,NULL,?,?,NULL,'initial_hold',?,?)",
                (memory_id, text, why_remember, principal.principal_id, canonical_hash(payload), now),
            )
            projection.upsert(
                conn, memory_id, 1, "full",
                projection.build_full(text, why_remember),
            )
            if raw_refs:
                for ref in raw_refs:
                    src_hash = _hl.sha256(
                        f"{ref.get('conversation_id')}:{ref.get('message_from')}"
                        f":{ref.get('message_to')}".encode()).hexdigest()
                    conn.execute(
                        "INSERT OR REPLACE INTO memory_raw_refs(memory_id,"
                        " conversation_id, message_from, message_to, source_hash,"
                        " bind_confidence, created_at) VALUES(?,?,?,?,?,'exact',?)",
                        (memory_id, ref.get("conversation_id"),
                         ref.get("message_from"), ref.get("message_to"),
                         src_hash, now))
                conn.execute(
                    "UPDATE memories SET source_state='bound' WHERE memory_id=?",
                    (memory_id,))
            audit.record(
                conn, "memory.created", principal.principal_id,
                resource_id=memory_id, resource_version=1,
                payload={"entry_source": entry_source, "memory_date": memory_date},
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
    return {
        "memory_id": memory_id,
        "version": m["current_version_no"],
        "memory_date": m["memory_date"],
        "date_confidence": m["date_confidence"],
        "visibility": m["visibility"],
        "representation": v["representation"],
        "text": (
            v["compressed_summary"] if v["representation"] == "forgotten_summary" else v["hold_text"]
        ),
        "why_remember": v["why_remember"] if v["representation"] == "full" else None,
        "pinned": bool(m["pinned"]),
        "protected": bool(m["protected"]),
    }


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
        " updated_at=? WHERE memory_id=?",
        (new_version, now, memory_id),
    )
    projection.upsert(
        conn, memory_id, new_version, "forgotten_summary",
        projection.build_forgotten(str(summary)),
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
                " updated_at=? WHERE memory_id=?",
                (new_version, now, memory_id),
            )
            projection.upsert(
                conn, memory_id, new_version, "full",
                projection.build_full(source["hold_text"] or "", source["why_remember"]),
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
