"""明确打开与查看回执（spec_v2 §6 / R09 / R10）。

`memory.open` 准备当前内容及查看票据；`memory.view.confirm` 确认这次
明确查看。票据只证明一次内容交付确认，不代表心理理解；不跨桶、不跨
身份、不跨 binding、不跨表示版本复用。确认事务对普通桶按自然日续期；
plan 阅读不在此路径内（计划阅读永不续期，见 plans/service.py）。

预加载、搜索 preview、自动刷新、bootstrap 不得调用 confirm。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound, ViewReceiptInvalid
from . import retention as retention_mod
from . import service as memory

_CONFIRM_TTL_SECONDS = 15 * 60  # 打开后未确认的票据有效期


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_memory(principal, memory_id: str) -> dict:
    """准备当前表示内容 + 签发一次性查看票据（仅双方本人）。"""
    if principal.principal_id not in ("jiaming", "qiaosheng"):
        raise Forbidden("only the two owners may open a memory",
                        principal=principal.principal_id)
    with db.formal() as conn:
        content = memory.get(conn, memory_id)  # 尊重当前表示：遗忘桶只给摘要
        version = memory.representation_version(conn, memory_id)
        receipt_id = f"vr_{uuid.uuid4().hex[:16]}"
        now = _now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO memory_view_receipts(receipt_id, principal_id,"
                " binding_id, memory_id, representation_version, confirm_key,"
                " issued_at, confirmed_at) VALUES(?,?,?,?,?,?,?,NULL)",
                (receipt_id, principal.principal_id, principal.binding_id,
                 memory_id, version, f"ck_{uuid.uuid4().hex[:16]}", now))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    content["view_receipt"] = receipt_id
    content["representation_version"] = version
    content["note"] = ("确认内容已显示后调用 memory.view.confirm；"
                       "票据不跨桶/身份/版本复用")
    return content


def confirm_view(principal, memory_id: str, receipt_id: str,
                 confirm_key: str | None = None) -> dict:
    """确认这次明确查看：核验回执 → 续期（普通桶）→ 幂等落审计。

    同一 confirm_key 只产生一次续期事件（RET-06）；同自然日多次真实打开
    due_date 相同（RET-15）。
    """
    if principal.principal_id not in ("jiaming", "qiaosheng"):
        raise Forbidden("only the two owners may confirm a view",
                        principal=principal.principal_id)
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            r = conn.execute(
                "SELECT * FROM memory_view_receipts WHERE receipt_id=?",
                (receipt_id,)).fetchone()
            if r is None:
                raise ViewReceiptInvalid("view receipt not found",
                                         receipt_id=receipt_id)
            if (r["principal_id"] != principal.principal_id
                    or r["binding_id"] != principal.binding_id):
                raise ViewReceiptInvalid(
                    "view receipt belongs to another principal/binding",
                    receipt_id=receipt_id)
            if r["memory_id"] != memory_id:
                raise ViewReceiptInvalid(
                    "view receipt cannot be used across memories",
                    receipt_id=receipt_id)
            if r["confirmed_at"] is not None:
                # 幂等重放：同票据重复确认返回同一结果，不二次续期
                conn.execute("COMMIT")
                return {"memory_id": memory_id, "receipt_id": receipt_id,
                        "confirmed_at": r["confirmed_at"],
                        "idempotent_replay": True}
            current_version = memory.representation_version(conn, memory_id)
            if r["representation_version"] != current_version:
                raise ViewReceiptInvalid(
                    "representation moved; re-open to get a fresh receipt",
                    receipt_id=receipt_id,
                    issued_for=r["representation_version"],
                    current=current_version)
            now = _now()
            _check_ttl(r["issued_at"], now)
            # 续期只作用于 v2 普通记忆桶（retention.status='active'）；
            # 旧 v1 桶无 retention 行：确认本身仍成立，只是不产生 v2 续期
            try:
                renewal = retention_mod.register_explicit_open(
                    conn, memory_id, now)
                retention_mod.bump_view_revision(conn, memory_id)
            except NotFound:
                renewal = {"renewed": False,
                           "reason": "legacy_memory_without_retention_row"}
            conn.execute(
                "UPDATE memory_view_receipts SET confirmed_at=?"
                " WHERE receipt_id=? AND confirmed_at IS NULL",
                (now, receipt_id))
            audit.record(conn, "memory.view.confirmed",
                         principal.principal_id, resource_id=memory_id,
                         payload={"receipt_id": receipt_id,
                                  "renewed": bool(renewal.get("renewed")),
                                  "due_date": renewal.get("due_date")})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "receipt_id": receipt_id,
            "confirmed_at": now, "renewed": bool(renewal.get("renewed")),
            "due_date": renewal.get("due_date"),
            "same_day": renewal.get("same_day")}


def _check_ttl(issued_at: str, now: str) -> None:
    issued = datetime.fromisoformat(issued_at)
    now_dt = datetime.fromisoformat(now)
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    if (now_dt - issued).total_seconds() > _CONFIRM_TTL_SECONDS:
        raise ViewReceiptInvalid("view receipt expired; re-open",
                                 issued_at=issued_at)
