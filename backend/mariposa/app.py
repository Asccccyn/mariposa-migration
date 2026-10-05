"""ASGI 装配：无领域决策。

HTTP 适配器只做三件事：解析绑定凭据 -> 调 Registry -> 统一响应包。
MCP / CC 适配器（后续 Phase）复用同一 Registry。
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse,
                                JSONResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles

from . import config, db, gate, schema
from . import oauth as _oauth
from .capabilities import mcp_adapter
from .capabilities import registry
from .capabilities import v1_compat
from .errors import MariposaError
from .identity import Principal, service as identity

# 静态资源相对代码树定位（而非 MARIPOSA_ROOT 数据根）：
# 测试/隔离根只隔离数据库，代码与构建产物位置不变
from pathlib import Path as _Path
_SOURCE_ROOT = _Path(__file__).resolve().parents[2]
WEB_DIR = _SOURCE_ROOT / "backend" / "mariposa" / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    schema.migrate()
    schema.migrate_runtime()
    v1_compat.register_v1_compat()
    # AF-GATE-03：门禁维护流程的到期事件消费端（隔离事务写审计）
    def _gate_expiry_sink(events):
        """AF-GATE-03 到期事件消费端。

        单条失败 stderr 留痕、**不阻断批次**（自查 B，2026-10-05）：
        若失败即 raise，gate 会保留整批 pending 下轮重试——同批已
        成功的条目将被重复写审计，破坏"到期事件一次性"语义。单条
        record_isolated 自身两表原子；放弃的是该条的审计记录，换
        取不重复、不积压。"""
        import sys
        from .audit import service as _audit
        for ip, payload in events:
            try:
                _audit.record_isolated(
                    "auth.lock.expired", "system", resource_id=ip,
                    payload={**payload, "consumer": "maintain"})
            except Exception as e:
                sys.stderr.write(
                    f"[gate] auth.lock.expired (maintain) audit write "
                    f"failed for {ip}: {e!r}\n")
    gate.set_expiry_sink(_gate_expiry_sink)
    yield


app = FastAPI(title="mariposa", version="0.1.0", lifespan=lifespan)


def _locked_response(seconds: int) -> JSONResponse:
    return JSONResponse(
        status_code=423,
        content={"ok": False, "error": {"code": "AUTH_LOCKED",
                                        "message": "来源已临时锁定"
                                        f"（约 {seconds} 秒后解除）",
                                        "detail": {"retry_after":
                                                   seconds}}},
        headers={"Retry-After": str(seconds)})


def _rate_limited_response(seconds: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"ok": False, "error": {"code": "RATE_LIMITED",
                                        "message": "请求过于频繁，"
                                        f"约 {seconds} 秒后重试",
                                        "detail": {"retry_after":
                                                   seconds}}},
        headers={"Retry-After": str(seconds)})


def _surface_managed(path: str) -> bool:
    """有真实 handler 的路径（其门禁语义由端点/适配器承担）——
    精确匹配，宽 startswith 会把 /apiX、/mcpjunk 等未注册路径错误
    豁免（AF-GATE-02）。"""
    return (path.startswith("/api/")
            or path == "/mcp" or path == "/mcp/maintenance")


@app.middleware("http")
async def _surface_gate(request: Request, call_next):
    """全路径 IP 级门禁（自查① + AF-GATE-02，2026-10-05）。

    - 锁定检查对**一切路径**生效（含 /api、/mcp 与未注册路径）：
      IP 封禁不该有绕行面；
    - 匿名档预检覆盖无 handler 的面（静态/未知路径）；
    - 404/405 事后补计匿名档：请求未被任何端点处理，不存在双扣
      （/api/unknown、/apiX、错误 HTTP 方法等不再免费通行）。
    """
    ip = gate.client_ip(request)
    remain = gate.assert_not_locked(ip)
    if remain > 0:
        return _locked_response(max(1, math.ceil(remain)))
    # AF-GATE-02（五轮复审）：请求级"已计档"标记——每条请求至多
    # 计一次匿名档（预检计过则 404/405/307 不再补计，杜绝双扣）
    request.state._gate_metered = False
    path = request.url.path
    if not _surface_managed(path):
        wait = gate.check_rate("anon", ip)
        request.state._gate_metered = True
        if wait > 0:
            return _rate_limited_response(max(1, math.ceil(wait)))
    response = await call_next(request)
    if (response.status_code in (404, 405, 307)
            and not getattr(request.state, "_gate_metered", False)):
        # 未被任何端点处理/只有重定向的请求：事后补计匿名档；
        # 计数即满时以 429 顶替（本请求不再免费通行）。307 含
        # /api/ 前缀的尾斜杠重定向——无业务正文但同样是打点
        wait = gate.check_rate("anon", ip)
        if wait > 0:
            return _rate_limited_response(max(1, math.ceil(wait)))
    return response


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "mariposa",
        "contract_version": config.CONTRACT_VERSION,
        "projection_revision": config.PROJECTION_REVISION,
    }


# ---------------------------------------------------------------- OAuth
# 动态授权（2026-10-05 江乔生裁定）：连接后输密码换临时 token。
# /oauth/* 与 /.well-known/* 不在 _surface_managed 内 → middleware 的
# IP 匿名档 + 锁定全链生效；密码错误另计失败锁定（防在线爆破）。

def _oauth_base_url(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


@app.get("/.well-known/oauth-authorization-server")
def oauth_discovery(request: Request):
    base = _oauth_base_url(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "grant_types_supported": list(_oauth.GRANT_TYPES),
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(_oauth.SCOPES),
    }


@app.post("/oauth/register")
async def oauth_register(request: Request):
    raw = await request.body()
    try:
        meta = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={
            "error": "invalid_client_metadata"})
    if not isinstance(meta, dict):
        return JSONResponse(status_code=400, content={
            "error": "invalid_client_metadata"})
    uris = meta.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        return JSONResponse(status_code=400, content={
            "error": "invalid_redirect_uris"})
    try:
        out = _oauth.register_client(meta.get("client_name"), uris)
    except MariposaError as e:
        return JSONResponse(status_code=e.http_status, content={
            "error": e.code})
    return JSONResponse(status_code=201, content=out)


def _esc(v: str) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&#39;"))


def _oauth_login_page(fields: dict, error: str | None) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{_esc(k)}" value="{_esc(v)}">'
        for k, v in fields.items() if v is not None)
    err = (f'<p class="error">{_esc(error)}</p>' if error else "")
    return f"""<!doctype html>
<html lang="zh">
<head><meta charset="utf-8"/><meta name="viewport"
 content="width=device-width, initial-scale=1"/>
<title>连接 mariposa</title>
<style>
body {{ font-family: system-ui,-apple-system,sans-serif; margin:0;
 background:#0f172a; color:#e2e8f0; }}
main {{ max-width:420px; margin:12vh auto; padding:32px;
 background:#111827; border:1px solid #334155; border-radius:18px; }}
h1 {{ margin:0 0 10px; font-size:24px; }}
p {{ color:#cbd5e1; line-height:1.5; }}
label {{ display:block; margin:16px 0 6px; font-weight:600; }}
input {{ box-sizing:border-box; width:100%; padding:12px 14px;
 border-radius:10px; border:1px solid #475569; background:#020617;
 color:#e2e8f0; font-size:16px; }}
button {{ margin-top:16px; width:100%; border:0; border-radius:10px;
 padding:12px; font-weight:700; color:#020617; background:#38bdf8; }}
.error {{ color:#fecaca; background:#7f1d1d; border-radius:10px;
 padding:10px 12px; }}
</style></head>
<body><main>
<h1>连接 mariposa</h1>
<p>输入密码完成授权。只有你自己主动连接时才确认。</p>
{err}
<form method="post">
{hidden}
<label for="password">密码</label>
<input id="password" name="password" type="password"
 autocomplete="current-password" autofocus required/>
<button type="submit">授权</button>
</form></main></body></html>"""


async def _oauth_form_body(request: Request) -> dict:
    raw = await request.body()
    from urllib.parse import parse_qs
    parsed = parse_qs((raw or b"").decode("utf-8", "replace"),
                      keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


@app.get("/oauth/authorize")
async def oauth_authorize_get(request: Request):
    # GET：参数在 query string（无 body）
    return _oauth_authorize_handle(
        request, dict(request.query_params))


@app.post("/oauth/authorize")
async def oauth_authorize_post(request: Request):
    return _oauth_authorize_handle(request, await _oauth_form_body(request))


def _oauth_authorize_handle(request: Request, form: dict):
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    state = form.get("state")
    challenge = form.get("code_challenge") or None
    method = form.get("code_challenge_method") or "S256"
    if method != "S256":
        return JSONResponse(status_code=400, content={
            "error": "unsupported_code_challenge_method"})
    if not _oauth.client_redirect_allowed(client_id, redirect_uri):
        return JSONResponse(status_code=400, content={
            "error": "unauthorized_client"})
    fields = {"client_id": client_id, "redirect_uri": redirect_uri,
              "state": state, "code_challenge": challenge,
              "code_challenge_method": "S256"}
    password = form.get("password")
    if not password:
        return HTMLResponse(_oauth_login_page(fields, None))
    ip = gate.client_ip(request)
    principal_id = _oauth.verify_password(password)
    if principal_id is None:
        # 密码错 → 计入门禁失败锁定（与 token 暴破同权）
        def _audit(level, seconds):
            _audit_gate_event("auth.locked", ip,
                              {"lock_level": level, "seconds": seconds,
                               "surface": "oauth"})
        gate.note_auth_failure(ip, record_audit=_audit)
        return HTMLResponse(_oauth_login_page(
            fields, "密码不正确。"), status_code=401)
    gate.note_auth_success(ip)
    code = _oauth.save_authorization_code(
        principal_id, client_id, redirect_uri, challenge,
        list(_oauth.SCOPES), form.get("resource"))
    from urllib.parse import urlencode
    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}",
                            status_code=302)


@app.post("/oauth/token")
async def oauth_token(request: Request):
    form = await _oauth_form_body(request)
    grant = form.get("grant_type", "")
    bad = JSONResponse(status_code=400, content={"error": "invalid_grant"})
    if grant == "password":
        ip = gate.client_ip(request)
        principal_id = _oauth.verify_password(form.get("password", ""))
        if principal_id is None:
            def _audit(level, seconds):
                _audit_gate_event("auth.locked", ip,
                                  {"lock_level": level,
                                   "seconds": seconds,
                                   "surface": "oauth"})
            gate.note_auth_failure(ip, record_audit=_audit)
            return JSONResponse(status_code=401, content={
                "error": "invalid_grant",
                "error_description": "密码不正确"})
        gate.note_auth_success(ip)
        return _oauth.issue_access_token(principal_id)
    if grant == "authorization_code":
        try:
            return _oauth.exchange_code(
                form.get("code", ""), form.get("client_id", ""),
                form.get("redirect_uri", ""),
                form.get("code_verifier") or None)
        except MariposaError:
            return bad
    if grant == "refresh_token":
        try:
            return _oauth.rotate_refresh(form.get("refresh_token", ""),
                                         form.get("client_id", ""))
        except MariposaError:
            return bad
    return JSONResponse(status_code=400, content={
        "error": "unsupported_grant_type"})


@app.post("/oauth/revoke")
async def oauth_revoke(request: Request):
    form = await _oauth_form_body(request)
    token = form.get("token", "")
    if token:
        th = _oauth._hash_token(token)
        with db.formal() as conn:
            conn.execute(
                "UPDATE client_bindings SET revoked=1 WHERE token_hash=?",
                (th,))
            conn.execute("DELETE FROM oauth_refresh_tokens"
                         " WHERE refresh_hash=?", (th,))
    return JSONResponse(status_code=200, content={})


@app.get("/api/capabilities")
def capabilities(request: Request):
    # ROOT-02（2026-10-04 二批）：缺 token/坏 token 是结构化 401
    # UNAUTHENTICATED，不是 500——客户端据此识别需重新认证
    try:
        principal = _authenticate_tracked(request)
        _rate_limit_read(principal)  # GATE-01：按能力分类计档
    except MariposaError as e:
        return _gate_error_response(e)
    return {"ok": True, "data": registry.list_capabilities(principal)}


@app.post("/api/capability/{name}")
async def invoke(name: str, request: Request):
    # 复审（2026-10-01）：鉴权只用 header——先于读体；未鉴权的大
    # body（含 chunked）不再被读进内存
    try:
        principal = _authenticate_tracked(request)
    except MariposaError as e:
        return _gate_error_response(e)
    # 门禁三件套（2026-10-04）：成功请求按能力分类计档——读能力
    # 扣读档、写能力扣更紧的写档（GATE-01：认证函数不隐式扣读，
    # 三档互不挤占）
    try:
        _rate_limit_capability(principal, name)
        body = await _json_body(request)
    except MariposaError as e:
        # P1-07 复审：坏 JSON / BODY_TOO_LARGE 走结构化错误，不是 500
        return _gate_error_response(e)
    # compact_v1 输出 profile（JSON 瘦身 2026-10-04 五）：HTTP 以
    # query param 协商（与 MCP arguments 内参数同语义，出站前剥离）
    # CR-02-R1（2026-10-04 复审 P2）：query 参数必须并入**传给
    # registry.invoke 的 arguments**——此前并进 body 顶层（与
    # arguments 平级）被整个丢弃，query 协商形同虚设。冲突优先级：
    # 显式 query 覆盖 arguments 内同名值（URL 是显式协商通道），
    # 未知值由 registry.invoke 统一结构化拒绝
    _prof = request.query_params.get("output_profile")
    _args = body.get("arguments", {})
    if not isinstance(_args, dict):
        _args = {}
    if _prof:
        _args = {**_args, "output_profile": _prof}
    idem = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
    try:
        result = registry.invoke(principal, name, _args, idem)
    except MariposaError as e:
        return _gate_error_response(e)
    return result


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _gate_error_response(e: MariposaError) -> JSONResponse:
    """门禁/业务错误统一序列化（GATE-02，2026-10-04 复审 P2）：

    - 结构化 code/message + 完整 detail（retry_after 不丢）；
    - 423/429 附 Retry-After 响应头（ceil 秒，至少 1）；
    - HTTP 与 MCP（mcp_adapter 同名 helper）保持同一字段集。
    """
    headers = None
    ra = e.detail.get("retry_after")
    if e.http_status in (423, 429):
        seconds = max(1, int(ra) if isinstance(ra, (int, float))
                      else 1)
        headers = {"Retry-After": str(seconds)}
    return JSONResponse(
        status_code=e.http_status,
        content={"ok": False, "error": {"code": e.code,
                                        "message": str(e),
                                        "detail": e.detail}},
        headers=headers)


def _audit_gate_event(event_type: str, ip: str, payload: dict) -> None:
    """门禁事件审计（GATE-06）：隔离事务写入——两表同事务，失败整体
    回滚且 stderr 留痕，不留已提交但无法发布的孤立事件。"""
    from .audit import service as _audit
    _audit.record_isolated(event_type, "system", resource_id=ip,
                           payload=payload)


def _authenticate_tracked(request: Request):
    """认证 + 门禁三件套（2026-10-04；GATE-01/05 修订）：

    - 锁定到期先做一次性识别并审计（auth.lock.expired）；
    - 锁定来源直接 423 AUTH_LOCKED——正确 token 也不放行，防在线
      枚举；
    - **认证失败**才计匿名档（IP 维度）与失败计数（触发指数锁定，
      auth.locked 留痕）；成功请求不占匿名档（GATE-01）；
    - 认证成功清零失败计数即返回——读/写档由调用方按能力分类扣减
      （_rate_limit_capability / _rate_limit_read），认证函数不隐式
      扣读。
    """
    ip = gate.client_ip(request)
    expired = gate.take_lock_expired_event(ip)
    if expired is not None:
        try:
            _audit_gate_event("auth.lock.expired", ip, expired)
        except Exception as audit_err:
            import sys
            sys.stderr.write(
                f"[gate] auth.lock.expired audit write failed: "
                f"{audit_err!r}\n")
    remain = gate.assert_not_locked(ip)
    if remain > 0:
        # RE-GATE-04：等待秒数 ceil 且至少 1——message/detail/header
        # 复用同一值，亚秒锁定不再出现 body=0 / header=1 的分裂
        seconds = max(1, math.ceil(remain))
        raise MariposaError(
            f"认证失败次数过多，来源已临时锁定（约 {seconds} 秒后"
            "自动解除）", code="AUTH_LOCKED", http_status=423,
            retry_after=seconds)
    try:
        principal = identity.authenticate(_bearer(request))
    except MariposaError:
        wait = gate.check_rate("anon", ip)
        gate.note_auth_failure(ip, record_audit=lambda level, seconds:
                               _audit_gate_event(
                                   "auth.locked", ip,
                                   {"lock_level": level,
                                    "seconds": seconds}))
        if wait > 0:
            raise MariposaError(
                f"认证失败请求过于频繁，约 {max(1, math.ceil(wait))} 秒"
                "后重试", code="RATE_LIMITED", http_status=429,
                retry_after=max(1, math.ceil(wait)))
        raise
    gate.note_auth_success(ip)
    return principal


def _gate_wait_seconds(wait: float) -> int:
    """RE-GATE-04：门禁等待秒数统一 ceil 且至少 1——message、
    detail 与 Retry-After 头复用同一值，不再出现亚秒分裂。"""
    return max(1, math.ceil(wait))


def _rate_limit_read(principal: Principal) -> None:
    wait = gate.check_rate("read", principal.principal_id)
    if wait > 0:
        s = _gate_wait_seconds(wait)
        raise MariposaError(f"请求过于频繁（读档），约 {s} 秒后重试",
                            code="RATE_LIMITED", http_status=429,
                            retry_after=s)


def _rate_limit_write(principal: Principal) -> None:
    wait = gate.check_rate("write", principal.principal_id)
    if wait > 0:
        s = _gate_wait_seconds(wait)
        raise MariposaError(f"写入操作过于频繁，约 {s} 秒后重试",
                            code="RATE_LIMITED", http_status=429,
                            retry_after=s)


def _rate_limit_capability(principal: Principal, name: str) -> None:
    """成功请求按能力分类计档（GATE-01）：读扣读档、写扣写档。"""
    cap = registry.REGISTRY.get(name)
    if cap is not None and cap.write:
        _rate_limit_write(principal)
    else:
        _rate_limit_read(principal)


async def _read_body_capped(request: Request, max_bytes: int) -> bytes:
    """复审（2026-10-01）：流式累计读取，超限**立即截停**——chunked/
    无 Content-Length 的请求不再先整包进内存才判断。"""
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            _e = MariposaError(
                f"request body exceeds {max_bytes}（媒体字节走"
                " /api/media/stage/{token}）", code="BODY_TOO_LARGE")
            _e.http_status = 413
            raise _e
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_body(request: Request) -> dict:
    # 复审（2026-10-01）：流式读取 + 即时截停（含 chunked 无
    # Content-Length 的请求）；Content-Length 预检保留为快路径
    clen = request.headers.get("content-length")
    # CB-048：坏 Content-Length（非数字）是坏请求不是 500
    try:
        clen_n = int(clen) if clen else None
    except ValueError:
        raise MariposaError("Content-Length is not a number",
                            code="INVALID_HEADER", http_status=400)
    if clen_n is not None and clen_n > _JSON_BODY_MAX_BYTES:
        raise MariposaError(
            f"request body {clen_n} > {_JSON_BODY_MAX_BYTES}（媒体字节走"
            " /api/media/stage/{token}）", code="BODY_TOO_LARGE",
                http_status=413)
    raw = await _read_body_capped(request, _JSON_BODY_MAX_BYTES)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    # CB-048：非法 UTF-8 字节同样结构化 400（此前 UnicodeDecodeError
    # 逃逸成 500）
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # 格式错误的请求不得静默当作空参数执行（OPS-02 结构化拒绝）
        raise MariposaError(f"request body is not valid JSON: {e}",
                            code="INVALID_JSON") from e
    if not isinstance(data, dict):
        raise MariposaError("request body must be a JSON object",
                            code="INVALID_JSON_BODY")
    return data


@app.exception_handler(sqlite3.Error)
async def db_error(request: Request, exc: sqlite3.Error):
    return JSONResponse(status_code=500, content={
        "ok": False, "error": {"code": "INTERNAL", "message": "database error"}})


# MCP：/mcp（周家明业务 profile）与 /mcp/maintenance（工具人 profile）。
# 路径只是分流约定；每条请求按 token binding 决定实际权限（§17.1）。
@app.post("/mcp")
async def mcp_business(request: Request):
    return await mcp_adapter.handle(request, "business")


@app.post("/mcp/maintenance")
async def mcp_maintenance(request: Request):
    return await mcp_adapter.handle(request, "maintenance")


# 媒体字节走专用端点：不进 capability JSON 参数（§16.2）
from fastapi import Response  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from . import config as _config  # noqa: E402
from .media import service as _media  # noqa: E402

_JSON_BODY_MAX_BYTES = 2 * 1024 * 1024  # P1-07：JSON 通道 2MB 硬上限


@app.put("/api/media/stage/{token}")
async def media_stage(token: str, request: Request):
    try:
        principal = _authenticate_tracked(request)
        _rate_limit_write(principal)  # 字节上传属写档
    except MariposaError as e:
        return _gate_error_response(e)
    # 复审（2026-10-01）：流式累计 + 即时截停（chunked 同样护住）；
    # 精确 size 校验仍由 stage_bytes 按声明 size 执行
    clen = request.headers.get("content-length")
    # RA-008：坏 Content-Length → 400（此前 ValueError 500）
    try:
        clen_n = int(clen) if clen else None
    except ValueError:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": {"code": "INVALID_HEADER",
                                   "message": "bad Content-Length"}})
    if clen_n is not None and clen_n > _media._MAX_SIZE:
        return JSONResponse(status_code=413,
                            content={"ok": False,
                                     "error": {"code": "BODY_TOO_LARGE"}})
    try:
        data = await _read_body_capped(request, _media._MAX_SIZE)
    except MariposaError:
        return JSONResponse(status_code=413,
                            content={"ok": False,
                                     "error": {"code": "BODY_TOO_LARGE"}})
    try:
        info = _media.stage_bytes(principal.principal_id, token, data)
    except MariposaError as e:
        return JSONResponse(status_code=e.http_status,
                            content={"ok": False, "error": {"code": e.code,
                                                            "message": str(e)}})
    return {"ok": True, "data": {"content_hash": info["content_hash"],
                                 "size": len(data), "staged": True,
                                 "note": "字节已落盘暂存；调"
                                         " media.upload.finalize(token)"}}


@app.get("/api/media/object/{content_hash}")
def media_object(content_hash: str, request: Request):
    try:
        principal = _authenticate_tracked(request)
        _rate_limit_read(principal)  # GATE-01：读档按能力分类
    except MariposaError as e:
        return _gate_error_response(e)
    try:
        meta, path = _media.get_media(principal.principal_id, content_hash)
    except MariposaError as e:
        return JSONResponse(status_code=e.http_status,
                            content={"ok": False, "error": {"code": e.code}})
    return FileResponse(str(path), media_type=meta["mime"],
                        filename=path.name)


# Source Layer：原文导出文件上传走专用字节端点（流式写盘，不进 capability
# JSON 参数，不驻内存）；随后调 source.import {path: upload_path}。
@app.put("/api/source/upload")
async def source_upload(request: Request):
    import uuid as _uuid
    try:
        principal = _authenticate_tracked(request)
        _rate_limit_write(principal)  # 字节上传属写档
    except MariposaError as e:
        return _gate_error_response(e)
    if principal.principal_id not in ("qiaosheng", "jiaming"):
        return JSONResponse(status_code=403,
                            content={"ok": False,
                                     "error": {"code": "FORBIDDEN"}})
    # 大小限额：Content-Length 预检 + 流式累计双保险（复核 v1.1 §6）
    declared = request.headers.get("Content-Length")
    if declared and declared.isdigit() and \
            int(declared) > _config.SOURCE_UPLOAD_MAX_BYTES:
        return JSONResponse(status_code=413, content={
            "ok": False, "error": {
                "code": "SOURCE_UPLOAD_TOO_LARGE",
                "max_bytes": _config.SOURCE_UPLOAD_MAX_BYTES}})
    _config.SOURCE_INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    # SRC-03/08（2026-10-04 全量审计）：保留原始扩展名（.md/.json/
    # .zip）——导入的格式检测靠它分流 md 方言；.part 后缀让既有
    # 48h staging 清理覆盖上传副本（此前 .tmp 永久累积）
    import re as _re
    _orig = request.query_params.get("filename") or ""
    _ext_m = _re.search(r"(\.[A-Za-z0-9]{1,8})$", _orig)
    _ext = _ext_m.group(1).lower() if _ext_m else ".bin"
    dest = _config.SOURCE_INCOMING_DIR / (
        "upload_" + _uuid.uuid4().hex[:12] + _ext + ".part")
    size = 0
    with open(dest, "wb") as f:
        async for chunk in request.stream():
            if not chunk:
                continue
            size += len(chunk)
            if size > _config.SOURCE_UPLOAD_MAX_BYTES:
                f.close()
                dest.unlink(missing_ok=True)
                return JSONResponse(status_code=413, content={
                    "ok": False, "error": {
                        "code": "SOURCE_UPLOAD_TOO_LARGE",
                        "max_bytes": _config.SOURCE_UPLOAD_MAX_BYTES}})
            f.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        return JSONResponse(status_code=400,
                            content={"ok": False,
                                     "error": {"code": "EMPTY_UPLOAD"}})
    return {"ok": True, "data": {"upload_path": str(dest), "bytes": size,
                                 "note": "已流式暂存到宿主数据根 incoming/"
                                         "；调 source.import 导入（成功后"
                                         "母本进入 Raw Archive）"}}


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))

# React/Vite 构建产物（apps/web，npm run build 生成）；与 /api 同源
WEB_DIST = _SOURCE_ROOT / "apps" / "web" / "dist"
if WEB_DIST.exists():
    app.mount("/app", StaticFiles(directory=str(WEB_DIST), html=True), name="webapp")
