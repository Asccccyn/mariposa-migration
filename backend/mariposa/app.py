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

from . import config, schema
from .capabilities import mcp_adapter
from .capabilities import registry
from .capabilities import v1_compat
from .errors import MariposaError
from .identity import service as identity

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
    principal = identity.authenticate(_bearer(request))
    return {"ok": True, "data": registry.list_capabilities(principal)}


@app.post("/api/capability/{name}")
async def invoke(name: str, request: Request):
    body = await _json_body(request)
    idem = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
    try:
        principal = identity.authenticate(_bearer(request))
        result = registry.invoke(principal, name, body.get("arguments", {}), idem)
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


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
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


@app.put("/api/media/stage/{token}")
async def media_stage(token: str, request: Request):
    try:
        principal = identity.authenticate(_bearer(request))
    except MariposaError as e:
        return JSONResponse(status_code=401,
                            content={"ok": False, "error": {"code": e.code}})
    data = await request.body()
    try:
        info = _media.stage_bytes(token, data)
    except MariposaError as e:
        return JSONResponse(status_code=e.http_status,
                            content={"ok": False, "error": {"code": e.code,
                                                            "message": str(e)}})
    return {"ok": True, "data": {"content_hash": info["content_hash"],
                                 "size": len(data),
                                 "note": "字节已暂存内存；调 media.upload.finalize 落盘"}}


@app.get("/api/media/object/{content_hash}")
def media_object(content_hash: str, request: Request):
    try:
        principal = identity.authenticate(_bearer(request))
    except MariposaError as e:
        return JSONResponse(status_code=401,
                            content={"ok": False, "error": {"code": e.code}})
    try:
        meta, path = _media.get_media(principal.principal_id, content_hash)
    except MariposaError as e:
        return JSONResponse(status_code=e.http_status,
                            content={"ok": False, "error": {"code": e.code}})
    return FileResponse(str(path), media_type=meta["mime"],
                        filename=path.name)


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))

# React/Vite 构建产物（apps/web，npm run build 生成）；与 /api 同源
WEB_DIST = _SOURCE_ROOT / "apps" / "web" / "dist"
if WEB_DIST.exists():
    app.mount("/app", StaticFiles(directory=str(WEB_DIST), html=True), name="webapp")
