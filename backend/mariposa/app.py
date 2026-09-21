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
from .errors import MariposaError
from .identity import service as identity

WEB_DIR = config.PROJECT_ROOT / "backend" / "mariposa" / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    schema.migrate()
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
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


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


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))
