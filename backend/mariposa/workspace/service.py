"""工作区：候选扫描、提案草稿/提交、跨库审批协调（§6、§7）。

草稿正文只存工作区；提交 = 正式库登记仅含 ID/hash/版本的 proposal_envelope。
审批针对具体 proposal_id + revision + hash + base_memory_version；
审批前正式库毫无变化。
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .. import audit as audit_mod
from .. import config, db
from ..errors import Forbidden, NotFound, ProposalAlreadyResolved
from ..identity import service as identity
from ..memory import service as memory

_SUBMITTERS = {"worker", "jiaming", "qiaosheng"}
_APPROVERS = {"qiaosheng", "jiaming"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def scan_candidates(started_by, min_idle_days: int | None = None) -> dict:
    """只读正式库筛候选，在工作区落 forget_proposal 草稿；不改正式桶。

    候选规则（§7.2 保守默认）：active+full、未 pinned/protected/anchor、
    memory_date 已过冷却期；日期 unknown 暂不自动选。
    """
    idle = min_idle_days if min_idle_days is not None else config.FORGET_IDLE_DAYS
    today_local = datetime.now(ZoneInfo(config.RELATIONSHIP_TIMEZONE)).date()
    created: list[dict] = []
    skipped: list[dict] = []
    with db.formal() as fconn:
        rows = fconn.execute(
            "SELECT memory_id, current_version_no, memory_date FROM memories"
            " WHERE visibility='active' AND compression_state='full'"
            " AND pinned=0 AND protected=0 AND anchor=0"
            " ORDER BY memory_date LIMIT ?",
            (config.FORGET_SCAN_BATCH_SIZE,),
        ).fetchall()
        for r in rows:
            if r["memory_date"] is None:
                skipped.append({"memory_id": r["memory_id"], "reason": "date_unknown"})
                continue
            try:
                d = date.fromisoformat(r["memory_date"])
            except ValueError:
                skipped.append({"memory_id": r["memory_id"], "reason": "bad_date"})
                continue
            if (today_local - d).days <= idle:
                skipped.append({"memory_id": r["memory_id"], "reason": "too_recent"})
                continue
            with db.workspace() as wconn:
                exists = wconn.execute(
                    "SELECT 1 FROM work_items WHERE target_memory_id=? AND state IN"
                    " ('draft','submitted','deferred')",
                    (r["memory_id"],),
                ).fetchone()
            if exists:
                skipped.append({"memory_id": r["memory_id"], "reason": "open_item"})
                continue
            created.append(_create_draft(started_by.principal_id, r["memory_id"]))

    run_id = f"run_{uuid.uuid4().hex[:12]}"
    with db.workspace() as wconn:
        wconn.execute(
            "INSERT INTO worker_runs(run_id, run_type, started_by, started_at, finished_at, stats)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, "forgetting_scan", started_by.principal_id, _now(), _now(),
             json.dumps({"created": len(created), "skipped": len(skipped)}, ensure_ascii=False)),
        )
    return {"run_id": run_id, "created": created, "skipped": skipped,
            "policy": {"idle_days": idle, "version": config.POLICY_VERSION}}


def _create_draft(created_by: str, target_memory_id: str) -> dict:
    proposal_id = f"prop_{uuid.uuid4().hex[:12]}"
    now = _now()
    payload = {
        "proposal_type": "forget_proposal",
        "target_memory_id": target_memory_id,
        "compressed_summary": "",
        "reason": "",
    }
    with db.workspace() as wconn:
        wconn.execute("BEGIN IMMEDIATE")
        try:
            wconn.execute(
                "INSERT INTO work_items(item_id, item_type, target_memory_id, state,"
                " current_revision, created_by, created_at, updated_at)"
                " VALUES(?, 'forget_proposal', ?, 'draft', 1, ?, ?, ?)",
                (proposal_id, target_memory_id, created_by, now, now),
            )
            wconn.execute(
                "INSERT INTO proposal_versions(proposal_id, revision, payload,"
                " payload_hash, created_by, submitted_at) VALUES(?,1,?,?,?,NULL)",
                (proposal_id, json.dumps(payload, ensure_ascii=False),
                 memory.canonical_hash(payload), created_by),
            )
            wconn.execute(
                "INSERT INTO workspace_audit(event_id, occurred_at, actor, action, item_id, detail)"
                " VALUES(?,?,?,?,?,?)",
                (f"evt_{uuid.uuid4().hex[:16]}", now, created_by, "proposal.created",
                 proposal_id, target_memory_id),
            )
            wconn.execute("COMMIT")
        except Exception:
            wconn.execute("ROLLBACK")
            raise
    return {"proposal_id": proposal_id, "revision": 1, "target_memory_id": target_memory_id}


def revise_draft(principal, proposal_id: str, compressed_summary: str,
                 reason: str) -> dict:
    """只有未提交草稿可修订；submitted 版本不可变（§6.1）。"""
    pid = getattr(principal, "principal_id", principal)
    with db.workspace() as wconn:
        item = wconn.execute(
            "SELECT * FROM work_items WHERE item_id=?", (proposal_id,)
        ).fetchone()
        if item is None:
            raise NotFound("work item not found", proposal_id=proposal_id)
        if item["state"] != "draft":
            raise Forbidden("only draft proposals can be revised", state=item["state"])
        creator_ok = pid
        if item["created_by"] != creator_ok and creator_ok not in _APPROVERS:
            raise Forbidden("only the creator or an approver may revise a draft")
        with db.formal() as fconn:
            m = fconn.execute(
                "SELECT current_version_no FROM memories WHERE memory_id=?",
                (item["target_memory_id"],),
            ).fetchone()
            if m is None:
                raise NotFound("target memory missing")
            base_version = m["current_version_no"]
        payload = {
            "proposal_type": "forget_proposal",
            "target_memory_id": item["target_memory_id"],
            "compressed_summary": compressed_summary,
            "reason": reason,
            "base_memory_version": base_version,
        }
        revision = item["current_revision"] + 1
        now = _now()
        wconn.execute("BEGIN IMMEDIATE")
        try:
            wconn.execute(
                "INSERT INTO proposal_versions(proposal_id, revision, payload,"
                " payload_hash, created_by, submitted_at) VALUES(?,?,?,?,?,NULL)",
                (proposal_id, revision, json.dumps(payload, ensure_ascii=False),
                 memory.canonical_hash(payload), pid),
            )
            wconn.execute(
                "UPDATE work_items SET current_revision=?, updated_at=? WHERE item_id=?",
                (revision, now, proposal_id),
            )
            wconn.execute("COMMIT")
        except Exception:
            wconn.execute("ROLLBACK")
            raise
    return {"proposal_id": proposal_id, "revision": revision,
            "payload_hash": memory.canonical_hash(payload),
            "base_memory_version": base_version}


def submit(principal, proposal_id: str, revision: int) -> dict:
    """提交：工作区版本冻结 + 正式库登记 envelope。登记失败则仍未正式提交。"""
    pid = getattr(principal, "principal_id", principal)
    if pid not in _SUBMITTERS:
        raise Forbidden("principal cannot submit proposals", principal=pid)
    with db.workspace() as wconn:
        v = wconn.execute(
            "SELECT * FROM proposal_versions WHERE proposal_id=? AND revision=?",
            (proposal_id, revision),
        ).fetchone()
        item = wconn.execute(
            "SELECT * FROM work_items WHERE item_id=?", (proposal_id,)
        ).fetchone()
        if v is None or item is None:
            raise NotFound("proposal revision not found", proposal_id=proposal_id)
        if item["state"] != "draft":
            raise Forbidden("item is not a draft", state=item["state"])
        payload = json.loads(v["payload"])
        if not payload.get("compressed_summary"):
            raise Forbidden("compressed_summary required before submit")
        base = payload.get("base_memory_version")
        if base is None:
            with db.formal() as fconn:
                m = fconn.execute(
                    "SELECT current_version_no FROM memories WHERE memory_id=?",
                    (item["target_memory_id"],),
                ).fetchone()
                base = m["current_version_no"] if m else None
            if base is None:
                raise NotFound("target memory missing")

        now = _now()
        with db.formal() as fconn:
            fconn.execute("BEGIN IMMEDIATE")
            try:
                fconn.execute(
                    "INSERT INTO proposal_envelopes(proposal_id, proposal_revision,"
                    " proposal_hash, proposal_type, target_memory_id, base_memory_version,"
                    " submitted_by, submitted_at) VALUES(?,?,?,?,?,?,?,?)",
                    (proposal_id, revision, v["payload_hash"], "forget_proposal",
                     item["target_memory_id"], base, pid, now),
                )
                audit_mod.record(
                    fconn, "workspace.proposal.submitted", pid,
                    resource_id=proposal_id,
                    payload={"revision": revision, "target": item["target_memory_id"],
                             "base_memory_version": base},
                )
                fconn.execute("COMMIT")
            except Exception:
                fconn.execute("ROLLBACK")
                raise
        wconn.execute("BEGIN IMMEDIATE")
        try:
            wconn.execute(
                "UPDATE proposal_versions SET submitted_at=? WHERE proposal_id=? AND revision=?",
                (now, proposal_id, revision),
            )
            wconn.execute(
                "UPDATE work_items SET state='submitted', updated_at=? WHERE item_id=?",
                (now, proposal_id),
            )
            wconn.execute(
                "INSERT INTO workspace_audit(event_id, occurred_at, actor, action, item_id, detail)"
                " VALUES(?,?,?,?,?,?)",
                (f"evt_{uuid.uuid4().hex[:16]}", now, pid, "proposal.submitted",
                 proposal_id, f"r{revision}"),
            )
            wconn.execute("COMMIT")
        except Exception:
            wconn.execute("ROLLBACK")
            raise
    return {
        "proposal_id": proposal_id,
        "revision": revision,
        "proposal_hash": v["payload_hash"],
        "base_memory_version": base,
        "state": "submitted",
    }


def decide(principal: identity.Principal, proposal_id: str, proposal_revision: int,
           proposal_hash: str, expected_memory_version: int, decision: str) -> dict:
    """审批协调：worker 一律拒绝；审批在一个正式库事务内应用后回填工作区。"""
    if principal.principal_id not in _APPROVERS:
        raise Forbidden("worker principals cannot approve proposals",
                        principal=principal.principal_id)
    if decision not in ("approve", "reject", "defer"):
        raise Forbidden("decision must be approve/reject/defer")

    with db.workspace() as wconn:
        v = wconn.execute(
            "SELECT * FROM proposal_versions WHERE proposal_id=? AND revision=?",
            (proposal_id, proposal_revision),
        ).fetchone()
        if v is None or v["submitted_at"] is None:
            raise NotFound("submitted proposal revision not found",
                           proposal_id=proposal_id, revision=proposal_revision)
        payload = json.loads(v["payload"])

    result: dict
    if decision == "approve":
        with db.formal() as fconn:
            fconn.execute("BEGIN IMMEDIATE")
            try:
                result = memory.apply_forget_approval(
                    fconn,
                    proposal_id=proposal_id,
                    proposal_revision=proposal_revision,
                    proposal_hash=proposal_hash,
                    expected_memory_version=expected_memory_version,
                    decided_by=principal.principal_id,
                    decided_binding=principal.binding_id,
                    payload=payload,
                )
                fconn.execute("COMMIT")
            except Exception:
                fconn.execute("ROLLBACK")
                raise
        new_state = "approved_and_applied"
    else:
        with db.formal() as fconn:
            fconn.execute("BEGIN IMMEDIATE")
            try:
                if decision == "reject":
                    memory.reject_or_withdraw(
                        fconn, proposal_id, "rejected",
                        principal.principal_id, principal.binding_id,
                    )
                else:  # defer：挂起，不产生终局
                    pass
                fconn.execute("COMMIT")
            except Exception:
                fconn.execute("ROLLBACK")
                raise
        result = {"proposal_id": proposal_id, "decision": decision}
        new_state = "rejected" if decision == "reject" else "deferred"

    with db.workspace() as wconn:
        if new_state != "deferred":
            wconn.execute(
                "UPDATE work_items SET state=?, updated_at=?, resolution_note=? WHERE item_id=?",
                (new_state, _now(),
                 json.dumps({"decided_by": principal.principal_id}, ensure_ascii=False),
                 proposal_id),
            )
        wconn.execute(
            "INSERT INTO workspace_audit(event_id, occurred_at, actor, action, item_id, detail)"
            " VALUES(?,?,?,?,?,?)",
            (f"evt_{uuid.uuid4().hex[:16]}", _now(), principal.principal_id,
             f"proposal.{decision}", proposal_id, ""),
        )
    return result


def list_items(states: list[str] | None = None) -> list[dict]:
    q = (
        "SELECT w.item_id, w.item_type, w.target_memory_id, w.state, w.current_revision,"
        " w.created_by, w.updated_at, v.payload, v.payload_hash, v.submitted_at"
        " FROM work_items w JOIN proposal_versions v"
        " ON v.proposal_id = w.item_id AND v.revision = w.current_revision"
    )
    params: tuple = ()
    if states:
        q += " WHERE w.state IN (" + ",".join("?" * len(states)) + ")"
        params = tuple(states)
    q += " ORDER BY w.updated_at DESC"
    with db.workspace() as wconn:
        rows = wconn.execute(q, params).fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["payload"])
        with db.formal() as fconn:
            m = fconn.execute(
                "SELECT current_version_no FROM memories WHERE memory_id=?",
                (r["target_memory_id"],),
            ).fetchone()
        out.append({
            "proposal_id": r["item_id"],
            "target_memory_id": r["target_memory_id"],
            "state": r["state"],
            "revision": r["current_revision"],
            "created_by": r["created_by"],
            "proposal_hash": r["payload_hash"],
            "base_memory_version": payload.get("base_memory_version")
            or (m["current_version_no"] if m else None),
            "current_memory_version": m["current_version_no"] if m else None,
            "compressed_summary": payload.get("compressed_summary", ""),
            "reason": payload.get("reason", ""),
            "submitted_at": r["submitted_at"],
        })
    return out
