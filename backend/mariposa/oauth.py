"""OAuth 动态授权层（2026-10-05 江乔生裁定）。

连接后输密码换临时 token——两个密码各映射一个身份：网页登录密码
→ qiaosheng、MCP 密码 → jiaming。架构约束：

- **只发币，不管权限**：access token 的 SHA256 写入既有
  client_bindings（entry_source='oauth'，带 expires_at）——下游
  identity/registry 权限矩阵/门禁/撤销全套零改动；
- 密码 PBKDF2-SHA256 慢哈希（600k 迭代）落 principal_credentials，
  明文永不落库/落日志；验证恒时比较，且两个身份都跑满（不因早停
  泄露匹配到谁）；
- MCP 客户端走标准流（RFC 9728 发现 + RFC 7591 动态注册 +
  authorization code + PKCE S256 + refresh 轮换），登录页与
  DevSpace 同款体验（输密码 → 授权 → 302 回调）；
- 网页走 password grant 直兑（fetch POST 密码 → token 存
  sessionStorage，替代手填静态 token）；
- 密码错误由调用方（app 层）计入门禁失败锁定——本模块不碰网络。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from . import db
from .errors import MariposaError

#: OWASP 2023（PBKDF2-SHA256）
PBKDF2_ITERATIONS = 600_000
CODE_TTL_S = 300
ACCESS_TTL_S = 12 * 3600
REFRESH_TTL_S = 30 * 24 * 3600

SCOPES = ("mariposa",)
GRANT_TYPES = ("authorization_code", "refresh_token", "password")


# ---------------------------------------------------------------- 密码

def _pbkdf2(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations).hex()


def set_password(principal_id: str, password: str) -> None:
    """设置/更新某身份的登录密码（脚本经 getpass 调用，明文不落屏）。"""
    if not password or len(password) < 8:
        raise MariposaError("密码至少 8 个字符", code="WEAK_PASSWORD")
    salt = secrets.token_bytes(16)
    now = time.time()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO principal_credentials(principal_id,"
                " password_hash, salt, iterations, updated_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(principal_id)"
                " DO UPDATE SET password_hash=excluded.password_hash,"
                " salt=excluded.salt, iterations=excluded.iterations,"
                " updated_at=excluded.updated_at",
                (principal_id, _pbkdf2(password, salt,
                                       PBKDF2_ITERATIONS),
                 salt.hex(), PBKDF2_ITERATIONS,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def verify_password(password: str) -> str | None:
    """验证密码 → 命中身份的 principal_id；不命中返回 None。

    两个身份都执行完整 PBKDF2（不因早停泄露匹配对象；缺失凭证的
    身份用同迭代数的假哈希跑满）。
    """
    with db.formal() as conn:
        rows = {r["principal_id"]: r for r in conn.execute(
            "SELECT * FROM principal_credentials").fetchall()}
    matched: str | None = None
    for pid in ("qiaosheng", "jiaming"):
        row = rows.get(pid)
        if row is not None:
            digest = _pbkdf2(password, bytes.fromhex(row["salt"]),
                             row["iterations"])
            ok = hmac.compare_digest(digest, row["password_hash"])
        else:
            # 跑满等量计算，不泄露"该身份尚未设密码"
            _pbkdf2(password, b"\x00" * 16, PBKDF2_ITERATIONS)
            ok = False
        if ok and matched is None:
            matched = pid
    return matched


def has_password(principal_id: str) -> bool:
    with db.formal() as conn:
        row = conn.execute(
            "SELECT 1 FROM principal_credentials WHERE principal_id=?",
            (principal_id,)).fetchone()
    return row is not None


# ---------------------------------------------------------------- 发币

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_access_token(principal_id: str, ttl_s: int = ACCESS_TTL_S,
                       entry_source: str = "oauth") -> dict:
    """发 access token：哈希入 client_bindings（带过期），返回明文。

    下游 authenticate 校验 expires_at（过期即拒），撤销走既有
    revoked 位——OAuth token 与静态 token 同一套生命周期管理。
    """
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO client_bindings(binding_id, token_hash,"
                " principal_id, entry_source, revoked, created_at,"
                " expires_at) VALUES(?,?,?,?,0,?,?)",
                (f"bdg_{secrets.token_hex(10)}", _hash_token(token),
                 principal_id, entry_source,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                 now + ttl_s))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"access_token": token, "token_type": "bearer",
            "expires_in": ttl_s}


def issue_refresh_token(principal_id: str, client_id: str,
                        scopes: list[str], ttl_s: int = REFRESH_TTL_S,
                        rotated_from: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db.formal() as conn:
        conn.execute(
            "INSERT INTO oauth_refresh_tokens(refresh_hash, client_id,"
            " principal_id, scopes, expires_at, rotated_from)"
            " VALUES(?,?,?,?,?,?)",
            (_hash_token(token), client_id, principal_id,
             json.dumps(scopes), now + ttl_s, rotated_from))
    return token


def rotate_refresh(refresh_token: str, client_id: str) -> dict:
    """刷新令牌轮换：旧的当场作废、发新对；scope 不得扩大。"""
    rh = _hash_token(refresh_token)
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_refresh_tokens WHERE refresh_hash=?",
            (rh,)).fetchone()
    if row is None or row["client_id"] != client_id:
        raise MariposaError("invalid refresh token",
                            code="INVALID_GRANT", http_status=400)
    if row["expires_at"] < time.time():
        with db.formal() as conn:
            conn.execute("DELETE FROM oauth_refresh_tokens"
                         " WHERE refresh_hash=?", (rh,))
        raise MariposaError("refresh token expired",
                            code="INVALID_GRANT", http_status=400)
    scopes = json.loads(row["scopes"])
    # 旧 refresh 一次性（轮换即作废）
    with db.formal() as conn:
        deleted = conn.execute(
            "DELETE FROM oauth_refresh_tokens WHERE refresh_hash=?",
            (rh,)).rowcount
    if not deleted:
        raise MariposaError("invalid refresh token",
                            code="INVALID_GRANT", http_status=400)
    access = issue_access_token(row["principal_id"])
    new_refresh = issue_refresh_token(
        row["principal_id"], client_id, scopes, rotated_from=rh)
    return {**access, "refresh_token": new_refresh,
            "scope": " ".join(scopes)}


# ---------------------------------------------------------------- code 流

def save_authorization_code(principal_id: str, client_id: str,
                            redirect_uri: str, code_challenge: str | None,
                            scopes: list[str],
                            resource: str | None) -> str:
    """登录页验证密码后签发 authorization code（记住命中的身份）。"""
    code = f"code-{secrets.token_urlsafe(24)}"
    with db.formal() as conn:
        conn.execute(
            "INSERT INTO oauth_codes(code, client_id, principal_id,"
            " redirect_uri, code_challenge, scopes, resource,"
            " expires_at, consumed) VALUES(?,?,?,?,?,?,?,?,0)",
            (code, client_id, principal_id, redirect_uri, code_challenge,
             json.dumps(scopes), resource, time.time() + CODE_TTL_S))
    return code


def exchange_code(code: str, client_id: str, redirect_uri: str,
                  code_verifier: str | None) -> dict:
    """authorization code + PKCE 换币（code 一次性、redirect 一致、
    S256 校验）；成功返回 token 响应。"""
    invalid = MariposaError("invalid authorization code",
                            code="INVALID_GRANT", http_status=400)
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_codes WHERE code=? AND consumed=0",
            (code,)).fetchone()
    if (row is None or row["client_id"] != client_id
            or row["expires_at"] < time.time()
            or redirect_uri != row["redirect_uri"]):
        raise invalid
    if row["code_challenge"]:
        if not code_verifier:
            raise invalid
        digest = hashlib.sha256(code_verifier.encode()).digest()
        expect = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if not hmac.compare_digest(expect, row["code_challenge"]):
            raise invalid
    with db.formal() as conn:
        claimed = conn.execute(
            "UPDATE oauth_codes SET consumed=1 WHERE code=? AND"
            " consumed=0", (code,)).rowcount
    if not claimed:
        raise invalid
    scopes = json.loads(row["scopes"])
    access = issue_access_token(row["principal_id"])
    refresh = issue_refresh_token(row["principal_id"], client_id, scopes)
    _purge_expired_codes()
    return {**access, "refresh_token": refresh,
            "scope": " ".join(scopes)}


def _purge_expired_codes() -> None:
    with db.formal() as conn:
        conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?",
                     (time.time(),))


# ---------------------------------------------------------------- 客户端注册

def register_client(client_name: str | None,
                    redirect_uris: list[str]) -> dict:
    """RFC 7591 动态客户端注册（MCP 客户端连接时自注册）。"""
    if not redirect_uris or not all(
            isinstance(u, str) and u.startswith(("http://", "https://"))
            for u in redirect_uris):
        raise MariposaError("redirect_uris 必须是 http(s) URL 数组",
                            code="INVALID_CLIENT_METADATA",
                            http_status=400)
    client_id = f"mcp_{secrets.token_hex(10)}"
    with db.formal() as conn:
        conn.execute(
            "INSERT INTO oauth_clients(client_id, client_name,"
            " redirect_uris, created_at) VALUES(?,?,?,?)",
            (client_id, client_name or "mcp-client",
             json.dumps(redirect_uris),
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    return {"client_id": client_id,
            "client_name": client_name or "mcp-client",
            "redirect_uris": redirect_uris}


def get_client(client_id: str):
    with db.formal() as conn:
        return conn.execute(
            "SELECT * FROM oauth_clients WHERE client_id=?",
            (client_id,)).fetchone()


def client_redirect_allowed(client_id: str, redirect_uri: str) -> bool:
    row = get_client(client_id)
    if row is None:
        return False
    return redirect_uri in json.loads(row["redirect_uris"])
