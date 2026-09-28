"""明确打开与查看回执（v1.7：明开回温事实，无遗忘续期）。

`memory.open` 准备当前内容及查看票据；`memory.view.confirm` 确认这次
明确查看并记录 last_explicit_open_at（服务端确认时刻，取 max 防乱序
旧确认倒退）。阶段由查询时以该事实现算（v1.7 §5.4）：
命中/Jev/hydrate/bootstrap/预览/accept 均不算打开；同一票据幂等重放
不刷新时间。票据不跨桶/身份/表示版本复用。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, ViewReceiptInvalid
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
        content = memory.get(conn, memory_id)
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
    """确认这次明确查看：核验回执 → 记录明开事实（v1.7 回温）→ 幂等审计。

    同一票据幂等重放返回首次成功确认的时间，不刷新 last_explicit_open_at。
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
                # 幂等重放：同票据重复确认返回同一结果，不刷新明开时间
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
            # v1.7 明开回温：记录服务端确认事实时刻（取 max，防乱序旧确认
            # 让 basis 倒退）；不做任何遗忘续期（已退役）。
            conn.execute(
                "UPDATE memories SET last_explicit_open_at="
                "CASE WHEN last_explicit_open_at IS NULL OR"
                " last_explicit_open_at < ? THEN ? ELSE last_explicit_open_at"
                " END, updated_at=updated_at WHERE memory_id=?",
                (now, now, memory_id))
            conn.execute(
                "UPDATE memory_view_receipts SET confirmed_at=?"
                " WHERE receipt_id=? AND confirmed_at IS NULL",
                (now, receipt_id))
            audit.record(conn, "memory.view.confirmed",
                         principal.principal_id, resource_id=memory_id,
                         payload={"receipt_id": receipt_id,
                                  "explicit_open_at": now})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "receipt_id": receipt_id,
            "confirmed_at": now, "explicit_open_at": now}


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
