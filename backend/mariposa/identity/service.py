"""身份与绑定。

服务器从 Authorization 凭据解析可信主体；工具参数不能自报身份（§3.1）。
第一版：principals 固定四种（qiaosheng / jiaming / worker / system），
token 以 sha256 哈希存 client_bindings，可撤销。
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from .. import db
from ..errors import Forbidden, NotFound, Unauthenticated

PRINCIPALS = [
    ("qiaosheng", "江乔生", "human"),
    ("jiaming", "周家明", "agent"),
    ("worker", "维护工具人", "agent"),
    ("system", "系统", "system"),
]

# §3.2 授权矩阵第一版子集：capability -> 允许的 principal 集合在 registry 中声明。
# 这里只回答“这个绑定是谁、是否还能用”。


@dataclass(frozen=True)
class Principal:
    principal_id: str
    display_name: str
    kind: str
    entry_source: str
    binding_id: str
    # 受限服务凭据（迁移 30）：binding 级 capability 白名单（frozenset）；
    # None = 不受限（既有 owner/editorial token 全部如此）。estómago 归档
    # worker 等自动凭据即使 principal 映射同主体，也只放行白名单能力
    capabilities_allowlist: frozenset | None = None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def seed(tokens: dict[str, str]) -> dict[str, str]:
    """按 principal_id -> 明文 token 建立绑定（A09：不复活、不覆盖）。

    只在绑定**不存在**时插入；已存在的绑定（含已撤销 revoked=1 或已
    更换 token）一律不动——重复 seed 不能覆盖撤销状态或替换 owner
    token。token 明文不落库。返回 {pid: "inserted"|"kept"}。
    """
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            known = {p[0] for p in PRINCIPALS}
            for pid, name, kind in PRINCIPALS:
                conn.execute(
                    "INSERT OR IGNORE INTO principals(principal_id, display_name, kind)"
                    " VALUES(?,?,?)",
                    (pid, name, kind),
                )
            result: dict[str, str] = {}
            for pid, token in tokens.items():
                if pid not in known:
                    raise ValueError(f"unknown principal: {pid}")
                cur = conn.execute(
                    "INSERT OR IGNORE INTO client_bindings"
                    "(binding_id, token_hash, principal_id, entry_source, created_at)"
                    " VALUES(?,?,?,?,datetime('now'))",
                    (f"binding_{pid}", _hash_token(token), pid,
                     _entry_source(pid)),
                )
                result[pid] = "inserted" if cur.rowcount else "kept"
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _entry_source(pid: str) -> str:
    return {
        "qiaosheng": "web",
        "jiaming": "claude_chat",
        "worker": "gpt_chat",
        "system": "scheduler",
    }[pid]


def authenticate(token: str | None) -> Principal:
    if not token:
        raise Unauthenticated("missing bearer token")
    import json as _json
    import time as _time
    row = None
    with db.formal() as conn:
        row = conn.execute(
            "SELECT b.binding_id, b.entry_source, b.revoked, b.expires_at,"
            " b.capabilities_allowlist,"
            " p.principal_id, p.display_name, p.kind"
            " FROM client_bindings b JOIN principals p USING(principal_id)"
            " WHERE b.token_hash=?",
            (_hash_token(token),),
        ).fetchone()
    if row is None or row["revoked"]:
        raise Unauthenticated("invalid or revoked token")
    # OAuth（2026-10-05）：动态授权 token 带过期——过期即拒（静态
    # token expires_at 为 NULL = 永久，不受影响）
    if row["expires_at"] is not None and row["expires_at"] < _time.time():
        raise Unauthenticated("token expired")
    # 受限白名单解析（自审②）：非 NULL 一律按受限对待——空串/坏 JSON
    # fail-closed 全拒，不把"写坏了"当成"不限"
    allowlist = None
    if row["capabilities_allowlist"] is not None:
        try:
            names = _json.loads(row["capabilities_allowlist"])
        except (ValueError, TypeError):
            names = None
        allowlist = frozenset(n for n in names if isinstance(n, str)) \
            if isinstance(names, list) else frozenset()
    return Principal(
        principal_id=row["principal_id"],
        display_name=row["display_name"],
        kind=row["kind"],
        entry_source=row["entry_source"],
        binding_id=row["binding_id"],
        capabilities_allowlist=allowlist,
    )


def require_any(principal: Principal, allowed: set[str]) -> None:
    if principal.principal_id not in allowed:
        raise Forbidden(
            "principal not allowed for this capability",
            principal=principal.principal_id,
        )


def generate_dev_tokens() -> dict[str, str]:
    """doctor/start 为本地开发生成随机 token（写入 runtime/.env，不入 git）。"""
    return {pid: secrets.token_urlsafe(24) for pid, _, _ in PRINCIPALS if pid != "system"}


def revoke_binding(principal_id: str, binding_id: str) -> dict:
    """撤销客户端绑定；旧 token/session 立即失效（T-ID-06）。"""
    if principal_id != "qiaosheng":
        raise Forbidden("only qiaosheng manages bindings", principal=principal_id)
    # P1-04（2026-10-05 审计）：撤销凭证是安全敏感动作，同事务留审计
    # （局部 import 避免 identity→audit 在模块加载期的潜在环）
    from ..audit import service as _audit
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE client_bindings SET revoked=1 WHERE binding_id=?"
                " AND revoked=0",
                (binding_id,))
            if cur.rowcount == 0:
                raise NotFound("active binding not found", binding_id=binding_id)
            owner = conn.execute(
                "SELECT principal_id FROM client_bindings WHERE binding_id=?",
                (binding_id,)).fetchone()
            _audit.record(conn, "binding.revoked", principal_id,
                          resource_id=binding_id,
                          payload={"binding_principal":
                                   owner["principal_id"] if owner else None})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"binding_id": binding_id, "revoked": True}
