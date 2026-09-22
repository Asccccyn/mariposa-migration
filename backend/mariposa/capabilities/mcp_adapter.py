"""MCP 适配层（Streamable HTTP / JSON-RPC 2.0 骨架）。

与 HTTP 适配器共用同一 Capability Registry；传输名
`mariposa_memory_search` <-> canonical `memory.search` 唯一映射。
路径 /mcp（周家明业务）与 /mcp/maintenance（工具人）只是分流约定：
**路径不赋予角色**，每条请求按 token binding 决定实际权限。

远程 OAuth、平台真实联调 = blocked（见 docs/platform_preflight.md）；
本层为协议实现，可本地验证，不冒充已接通平台。
"""
from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import JSONResponse

from ..errors import MariposaError
from ..identity import Principal
from ..identity import service as identity
from . import registry

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "mariposa", "version": "0.1.0"}

# 业务 profile 允许的主体 / 维护 profile 允许的主体
# 林石见经 MCP 维护入口领取/审查（§8.1：受限委托，非 worker 集合升权）
PROFILE_PRINCIPALS = {
    "business": {"jiaming", "qiaosheng"},
    "maintenance": {"worker", "linshijian"},
}


from .registry import REGISTRY  # noqa: E402  （延迟导入避免循环依赖）

_T_PREFIX = "mariposa_"


def _transport_name(canonical: str) -> str:
    return _T_PREFIX + canonical.replace(".", "_")


# 反解必须查表（字符串变换不可逆：memory.versions.read 的下划线归属有歧义）。
# 实时查 REGISTRY：兼容层在运行期注册的能力同样可反解。
def _canonical_name(transport: str) -> str:
    if transport.startswith(_T_PREFIX):
        for name in REGISTRY:
            if _transport_name(name) == transport:
                return name
    return transport


def _tools_for(principal: Principal) -> list[dict]:
    tools = []
    for cap in registry.REGISTRY.values():
        if principal.principal_id not in cap.allowed_principals:
            continue
        from . import input_schemas
        schema = input_schemas.schema_for(cap.name) or {
            "type": "object", "properties": {}, "additionalProperties": True}
        tools.append({
            "name": _transport_name(cap.name),
            "description": cap.description,
            "inputSchema": schema,
            "annotations": {"readOnlyHint": not cap.write,
                            "idempotentHint": cap.idempotent},
        })
    return tools


async def handle(request: Request, profile: str) -> JSONResponse:
    """单条 JSON-RPC 请求处理（batch 不在第一版范围）。"""
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, ValueError):
        return _rpc_error(None, -32700, "Parse error")

    msg_id = body.get("id")
    method = body.get("method", "")

    # initialize 与 notifications 不需要鉴权之外的状态；其余方法都要求有效凭据
    try:
        token = _bearer(request)
        principal = identity.authenticate(token)
    except MariposaError as e:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": msg_id,
             "error": {"code": -32001, "message": f"{e.code}: {e}"}},
            status_code=401)

    allowed = PROFILE_PRINCIPALS[profile]
    if principal.principal_id not in allowed:
        return _rpc_error(msg_id, -32002,
                          f"binding principal not allowed on this profile")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "mariposa MCP；工具与 HTTP API 共用同一业务注册表。",
        }
        return _rpc_result(msg_id, result)

    if method == "notifications/initialized" or method.startswith("notifications/"):
        return JSONResponse(status_code=202, content=None)

    if method == "tools/list":
        return _rpc_result(msg_id, {"tools": _tools_for(principal)})

    if method == "tools/call":
        params = body.get("params") or {}
        name = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        canonical = _canonical_name(name)
        try:
            out = registry.invoke(principal, canonical, arguments,
                                  params.get("_client_idempotency_key"))
        except MariposaError as e:
            # 业务错误走 MCP tool error 路径，不是协议错误
            return _rpc_result(msg_id, {
                "content": [{"type": "text",
                             "text": json.dumps(
                                 {"ok": False, "error": {"code": e.code,
                                                         "message": str(e)}},
                                 ensure_ascii=False)}],
                "isError": True,
            })
        return _rpc_result(msg_id, {
            "content": [{"type": "text",
                         "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out,
            "isError": False,
        })

    return _rpc_error(msg_id, -32601, f"Method not found: {method}")


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _rpc_result(msg_id, result) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _rpc_error(msg_id, code, message) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id,
                         "error": {"code": code, "message": message}})
