"""Capability Registry：一份注册表，多个适配器（§17.1）。

HTTP / MCP / CC 都调这里注册的 handler；权限按 principal 声明，
幂等键统一在本层落库。路径、传输不赋予角色。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .. import db
from ..errors import Forbidden, IdempotencyConflict, MariposaError, NotFound
from ..identity import Principal
from ..identity import service as identity
from ..memory import service as memory
from ..retrieval import search as retrieval_search
from ..workspace import service as workspace


@dataclass(frozen=True)
class Capability:
    name: str
    handler: Callable
    allowed_principals: set[str]
    write: bool
    idempotent: bool = False
    description: str = ""


def _everyone() -> set[str]:
    return {"qiaosheng", "jiaming", "worker"}


def _owners() -> set[str]:
    return {"qiaosheng", "jiaming"}


def _register() -> dict[str, Capability]:
    caps: dict[str, Capability] = {}

    def add(name, handler, allowed, write, idempotent=False, description=""):
        caps[name] = Capability(name, handler, set(allowed), write, idempotent, description)

    add("memory.hold", _hold, {"qiaosheng", "jiaming"}, True,
        description="写入一条正式记忆（full 表示，version 1）")
    add("memory.get", _get, _owners(), False, description="读取当前表示（遗忘桶只返回摘要）")
    add("memory.search", _search, _owners(), False, description="关键词检索有效投影")
    add("memory.versions.read", _versions, _owners(), False,
        description="明确展开历史版本，不自动 restore")
    add("memory.restore", _restore, _owners(), True,
        description="恢复到最近压缩前版本（或指定历史 full 版本）")
    add("workspace.forgetting.scan", _scan, {"worker", "qiaosheng", "jiaming"}, False,
        description="扫描遗忘候选并落工作区草稿；不改正式桶")
    add("workspace.proposals.revise", _revise, {"worker", "qiaosheng", "jiaming"}, True,
        description="修订未提交草稿")
    add("workspace.proposals.submit", _submit, {"worker", "qiaosheng", "jiaming"}, True,
        True, description="提交提案：冻结版本并登记正式 envelope")
    add("workspace.proposals.list", _list_items, _everyone(), False,
        description="列出工作区提案")
    add("memory.forgetting.decide", _decide, _owners(), True, True,
        description="审批遗忘提案（worker 拒绝）")
    return caps


def invoke(principal: Principal, capability: str, arguments: dict,
           idempotency_key: str | None) -> dict:
    cap = REGISTRY.get(capability)
    if cap is None:
        raise NotFound("unknown capability", capability=capability)
    identity.require_any(principal, cap.allowed_principals)

    if cap.idempotent and idempotency_key:
        return _idempotent_invoke(principal, cap, arguments, idempotency_key)
    return {"ok": True, "data": cap.handler(principal, arguments)}


def _payload_hash(arguments: dict) -> str:
    return memory.canonical_hash(arguments)


def _idempotent_invoke(principal: Principal, cap: Capability, arguments: dict,
                       key: str) -> dict:
    ph = _payload_hash(arguments)
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE principal_id=? AND capability=?"
            " AND idempotency_key=?",
            (principal.principal_id, cap.name, key),
        ).fetchone()
        if row:
            if row["payload_hash"] != ph:
                raise IdempotencyConflict(
                    "same key with different payload",
                    capability=cap.name, key=key,
                )
            return {"ok": True, "data": json.loads(row["result_ref"]), "idempotent_replay": True}
    result = cap.handler(principal, arguments)
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO idempotency_records(principal_id, capability,"
                " idempotency_key, payload_hash, status, result_ref, created_at)"
                " VALUES(?,?,?,?, 'completed', ?, datetime('now'))",
                (principal.principal_id, cap.name, key, ph,
                 json.dumps(result, ensure_ascii=False)),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"ok": True, "data": result}


# ---------- handlers：纯领域逻辑，不做传输层判断 ----------

def _hold(principal: Principal, a: dict) -> dict:
    return memory.hold(
        principal,
        text=str(a.get("text", "")),
        why_remember=a.get("why_remember"),
        memory_date=a.get("memory_date"),
        date_confidence=a.get("date_confidence", "unknown"),
        entry_source=principal.entry_source,
    )


def _get(principal: Principal, a: dict) -> dict:
    with db.formal() as conn:
        return memory.get(conn, str(a.get("memory_id", "")))


def _versions(principal: Principal, a: dict) -> dict:
    with db.formal() as conn:
        return {"versions": memory.versions_read(conn, str(a.get("memory_id", "")))}


def _search(principal: Principal, a: dict) -> dict:
    with db.formal() as conn:
        return retrieval_search.search(conn, str(a.get("query", "")),
                                       int(a.get("limit", 20)))


def _restore(principal: Principal, a: dict) -> dict:
    return memory.restore(
        principal,
        memory_id=str(a.get("memory_id", "")),
        expected_current_version=int(a["expected_current_version"]),
        target_history_version=a.get("target_history_version"),
    )


def _scan(principal: Principal, a: dict) -> dict:
    return workspace.scan_candidates(principal, a.get("min_idle_days"))


def _revise(principal: Principal, a: dict) -> dict:
    return workspace.revise_draft(
        principal,
        proposal_id=str(a.get("proposal_id", "")),
        compressed_summary=str(a.get("compressed_summary", "")),
        reason=str(a.get("reason", "")),
    )


def _submit(principal: Principal, a: dict) -> dict:
    return workspace.submit(
        principal,
        proposal_id=str(a.get("proposal_id", "")),
        revision=int(a.get("revision", 1)),
    )


def _decide(principal: Principal, a: dict) -> dict:
    return workspace.decide(
        principal,
        proposal_id=str(a.get("proposal_id", "")),
        proposal_revision=int(a.get("proposal_revision", 0)),
        proposal_hash=str(a.get("proposal_hash", "")),
        expected_memory_version=int(a.get("expected_memory_version", 0)),
        decision=str(a.get("decision", "")),
    )


def _list_items(principal: Principal, a: dict) -> dict:
    states = a.get("states")
    return {"items": workspace.list_items(states)}


REGISTRY = _register()


def list_capabilities(principal: Principal) -> list[dict]:
    return [
        {"name": c.name, "write": c.write, "description": c.description}
        for c in REGISTRY.values()
        if principal.principal_id in c.allowed_principals
    ]
