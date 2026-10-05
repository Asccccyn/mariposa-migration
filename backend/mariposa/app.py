"""ASGI 装配：无领域决策。

HTTP 适配器只做三件事：解析绑定凭据 -> 调 Registry -> 统一响应包。
MCP / CC 适配器（后续 Phase）复用同一 Registry。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, gate, schema
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
    yield


app = FastAPI(title="mariposa", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "mariposa",
        "contract_version": config.CONTRACT_VERSION,
        "projection_revision": config.PROJECTION_REVISION,
    }


@app.get("/api/capabilities")
def capabilities(request: Request):
    # ROOT-02（2026-10-04 二批）：缺 token/坏 token 是结构化 401
    # UNAUTHENTICATED，不是 500——客户端据此识别需重新认证
    try:
        principal = _authenticate_tracked(request)
    except MariposaError as e:
        return JSONResponse(
            status_code=e.http_status,
            content={"ok": False, "error": {"code": e.code,
                                            "message": str(e),
                                            "detail": e.detail}})
    return {"ok": True, "data": registry.list_capabilities(principal)}


@app.post("/api/capability/{name}")
async def invoke(name: str, request: Request):
    # 复审（2026-10-01）：鉴权只用 header——先于读体；未鉴权的大
    # body（含 chunked）不再被读进内存
    try:
        principal = _authenticate_tracked(request)
    except MariposaError as e:
        return JSONResponse(
            status_code=e.http_status,
            content={"ok": False, "error": {"code": e.code,
                                            "message": str(e),
                                            "detail": e.detail}})
    # 门禁三件套（2026-10-04）：写能力单独更紧的限速档——读写同档
    # 会让导入/召回写风暴挤占读，或读高频放行写滥用
    try:
        _rate_limit_capability(principal, name)
        body = await _json_body(request)
    except MariposaError as e:
        # P1-07 复审：坏 JSON / BODY_TOO_LARGE 走结构化错误，不是 500
        return JSONResponse(
            status_code=e.http_status,
            content={"ok": False, "error": {"code": e.code,
                                            "message": str(e)}})
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
        return JSONResponse(
            status_code=e.http_status,
            content={"ok": False, "error": {"code": e.code, "message": str(e),
                                            "detail": e.detail}},
        )
    return result


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _authenticate_tracked(request: Request):
    """认证 + 门禁三件套（2026-10-04）：

    - 锁定来源直接 423 AUTH_LOCKED（Retry-After 在 detail）——正确
      token 也不放行，防在线枚举；
    - 认证失败按来源 IP 计数并触发指数锁定（audit 留痕）；
    - 未认证请求同时计匿名限速档（IP 维度）；
    - 认证成功清零失败计数，主体记入读档限速窗口。
    """
    ip = gate.client_ip(request)
    remain = gate.assert_not_locked(ip)
    if remain > 0:
        raise MariposaError(
            f"认证失败次数过多，来源已临时锁定（约 {int(remain)} 秒后"
            "自动解除）", code="AUTH_LOCKED", http_status=423,
            retry_after=int(remain))
    wait = gate.check_rate("anon", ip)
    if wait > 0:
        raise MariposaError("匿名请求过于频繁，请稍后重试",
                            code="RATE_LIMITED", http_status=429,
                            retry_after=int(wait) + 1)
    try:
        principal = identity.authenticate(_bearer(request))
    except MariposaError:
        def _audit_lock(level: int, seconds: int) -> None:
            from . import audit, db as _db
            with _db.formal() as conn:
                audit.record(
                    conn, "auth.locked", "system", resource_id=ip,
                    payload={"lock_level": level, "seconds": seconds})
        gate.note_auth_failure(ip, record_audit=_audit_lock)
        raise
    gate.note_auth_success(ip)
    wait = gate.check_rate("read", principal.principal_id)
    if wait > 0:
        raise MariposaError("请求过于频繁（读档），请稍后重试",
                            code="RATE_LIMITED", http_status=429,
                            retry_after=int(wait) + 1)
    return principal


def _rate_limit_write(principal: Principal) -> None:
    wait = gate.check_rate("write", principal.principal_id)
    if wait > 0:
        raise MariposaError("写入操作过于频繁，请稍后重试",
                            code="RATE_LIMITED", http_status=429,
                            retry_after=int(wait) + 1)


def _rate_limit_capability(principal: Principal, name: str) -> None:
    """写能力限速档（读写分档；读档已在 _authenticate_tracked 计）。"""
    cap = registry.REGISTRY.get(name)
    if cap is None or not cap.write:
        return
    _rate_limit_write(principal)


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
        return JSONResponse(status_code=e.http_status,
                            content={"ok": False, "error": {"code": e.code,
                                                            "message": str(e)}})
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
    except MariposaError as e:
        return JSONResponse(status_code=e.http_status,
                            content={"ok": False, "error": {"code": e.code,
                                                            "message": str(e)}})
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
        return JSONResponse(status_code=e.http_status,
                            content={"ok": False, "error": {"code": e.code,
                                                            "message": str(e)}})
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
