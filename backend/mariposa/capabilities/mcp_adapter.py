"""MCP 适配层（Streamable HTTP / JSON-RPC 2.0 骨架）。

与 HTTP 适配器共用同一 Capability Registry；传输名
`mariposa_memory_search` <-> canonical `memory.search` 唯一映射。
路径 /mcp（周家明业务）与 /mcp/maintenance（工具人）只是分流约定：
**路径不赋予角色**，每条请求按 token binding 决定实际权限。

远程 OAuth、平台真实联调 = blocked（见 docs/platform_preflight.md）；
本层为协议实现，可本地验证，不冒充已接通平台。
"""
from __future__ import annotations

import copy
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
PROFILE_PRINCIPALS = {
    "business": {"jiaming", "qiaosheng"},
    # v1.7：林石见应用内审查角色已退役；maintenance 仅通用工程工具
    "maintenance": {"worker"},
}


from .registry import REGISTRY  # noqa: E402  （延迟导入避免循环依赖）

_T_PREFIX = "mariposa_"


def _transport_name(canonical: str) -> str:
    return _T_PREFIX + canonical.replace(".", "_")


# 反解必须查表（字符串变换不可逆：memory.context.validate 的下划线归属有歧义）。
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
        # 受限服务凭据（迁移 30）：工具清单与 HTTP invoke 同源过滤——
        # 受限 binding 的 tools/list 不得披露/放行白名单外能力
        if principal.capabilities_allowlist is not None and \
                cap.name not in principal.capabilities_allowlist:
            continue
        # 瘦身（审计 1005B）：blocked/reserved 占位（v1 兼容面的诚实
        # 声明）对 MCP 消费者是纯发现噪声——调了也只会拿到 blocked
        # 状态。仅从 tools/list 隐藏；HTTP capabilities.list 与 invoke
        # 照旧（能力仍注册，负测/审计可验证）
        if cap.description.startswith(("[blocked]", "[reserved]")):
            continue
        from . import input_schemas
        schema = input_schemas.schema_for(cap.name) or {
            "type": "object", "properties": {}, "additionalProperties": True}
        # CR-03-R1（2026-10-04 复审 P2）：支持 compact_v1 的能力在
        # **传输层拷贝**的 schema 上声明 output_profile 枚举——标准
        # 客户端按 tools/list 的 inputSchema 构造参数，严格 schema
        # （additionalProperties=false）此前不允许该参数，发现/协商
        # 链路断了（手工绕过 schema 的调用能通不算修好）。正源业务
        # schema 与幂等 payload 哈希不变（registry.invoke 出站前剥离）
        from . import compact
        if compact.supports(cap.name):
            schema = copy.deepcopy(schema)
            schema.setdefault("properties", {})
            schema["properties"]["output_profile"] = {
                "type": "string", "enum": ["legacy", "compact_v1"],
                "description": "出站 JSON 瘦身 profile（传输层参数，"
                               "业务执行前剥离；MCP 面默认 compact_v1，"
                               "传 legacy 取全量）"}
        tools.append({
            "name": _transport_name(cap.name),
            "description": cap.description,
            "inputSchema": schema,
            "annotations": {"readOnlyHint": not cap.write,
                            "idempotentHint": cap.idempotent},
        })
    return tools


_BODY_MAX_BYTES = 2 * 1024 * 1024  # P1-07：与 /api 通道同额


async def handle(request: Request, profile: str) -> JSONResponse:
    """单条 JSON-RPC 请求处理（batch 不在第一版范围）。"""
    # 全量审计 P1-07 复审：鉴权与 body 上限都先于读体——大 JSON 不再
    # 能在未鉴权时整包进内存，媒体字节只能走专用 stage 端点
    # 门禁三件套（2026-10-04；GATE-01/02/05/06 修订）：与 HTTP 同一
    # 套失败锁定/限速语义——认证失败才计匿名档；成功后读/写档按
    # method/能力分类扣减；423/429 带 Retry-After 头且非 HTTP 200；
    # 锁定触发/到期审计走隔离事务
    from .. import gate as _gate
    from ..errors import MariposaError as _ME

    def _audit_gate(event_type: str, payload: dict) -> None:
        from ..audit import service as _audit
        _audit.record_isolated(event_type, "system", resource_id=_ip,
                               payload={**payload, "transport": "mcp"})

    _meta_metered = False

    def _proto_rate_guard(msg_id=None):
        """AF-GATE-02（四轮复审）：协议/信封错误返回前统一计读档
        ——错误路径不是免费通行；超限时以 429 顶替原协议错误。
        五轮修订：method 级 meta 计档（initialize/notifications/未知
        method）已扣过读档时共享，不再二次扣（wrong_profile 双扣）。"""
        nonlocal _meta_metered
        if _meta_metered:
            return None
        import math as _math
        _w = _gate.check_rate("read", principal.principal_id)
        _meta_metered = True
        if _w > 0:
            _s = max(1, _math.ceil(_w))
            return _gate_rpc_error(
                msg_id, "RATE_LIMITED",
                f"请求过于频繁（读档），{_s}s 后重试", 429, _s)
        return None

    _ip = _gate.client_ip(request)
    _expired = _gate.take_lock_expired_event(_ip)
    if _expired is not None:
        try:
            _audit_gate("auth.lock.expired", _expired)
        except Exception as audit_err:
            import sys
            sys.stderr.write(
                f"[gate] auth.lock.expired audit write failed: "
                f"{audit_err!r}\n")
    try:
        _remain = _gate.assert_not_locked(_ip)
        if _remain > 0:
            # RE-GATE-04：ceil 且至少 1，message/data/header 同值
            import math as _math
            _lock_s = max(1, _math.ceil(_remain))
            raise _ME(
                f"认证失败次数过多，来源已临时锁定（约 {_lock_s} 秒"
                "后自动解除）", code="AUTH_LOCKED", http_status=423,
                retry_after=_lock_s)
        token = _bearer(request)
        principal = identity.authenticate(token)
    except _ME as e:
        if e.code == "UNAUTHENTICATED":
            # 认证失败才占匿名档（GATE-01：成功请求不占）
            _wait = _gate.check_rate("anon", _ip)
            _gate.note_auth_failure(
                _ip, record_audit=lambda level, seconds: _audit_gate(
                    "auth.locked", {"lock_level": level,
                                    "seconds": seconds}))
            if _wait > 0:
                import math as _math
                _ws = max(1, _math.ceil(_wait))
                return _gate_rpc_error(
                    None, "RATE_LIMITED",
                    f"认证失败请求过于频繁，{_ws}s 后重试", 429, _ws)
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32001, "message": f"{e.code}: {e}",
                       "data": {"code": e.code,
                                "retry_after": e.detail.get("retry_after"),
                                "detail": e.detail}}},
            status_code=e.http_status,
            headers=({"Retry-After": str(max(1, int(
                e.detail.get("retry_after") or 1)))}
                if e.http_status in (423, 429) else None))
    _gate.note_auth_success(_ip)
    # GATE-01：主体限速在读体/分发处按 method 分类扣减，认证段不扣
    clen = request.headers.get("content-length")
    # RA-008（2026-10-02 复审 P2）：坏 Content-Length 是 -32600 不是 500
    try:
        clen_n = int(clen) if clen else None
    except ValueError:
        _r = _proto_rate_guard()
        return _r or _rpc_error(None, -32600,
                                "invalid Content-Length header")
    if clen_n is not None and clen_n > _BODY_MAX_BYTES:
        _r = _proto_rate_guard()
        return _r or _rpc_error(None, -32600,
                                f"request body too large"
                                f" (> {_BODY_MAX_BYTES})")
    # 复审（2026-10-01）：流式累计 + 即时截停（chunked 无 CL 同样护住）
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > _BODY_MAX_BYTES:
            _r = _proto_rate_guard()
            return _r or _rpc_error(None, -32600,
                                    f"request body too large"
                                    f" (> {_BODY_MAX_BYTES})")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        _r = _proto_rate_guard()
        return _r or _rpc_error(None, -32700, "Parse error")
    # CB-048（2026-10-02 审计 P2）：合法 JSON 不等于合法 RPC envelope
    # ——body=[]/null/数值、method 非字符串、params/arguments 非
    # object 都是结构化 -32600，不是未处理 500
    if not isinstance(body, dict):
        _r = _proto_rate_guard()
        return _r or _rpc_error(None, -32600,
                                "invalid request: body must be an object")
    msg_id = body.get("id")
    method = body.get("method", "")
    if not isinstance(method, str):
        _r = _proto_rate_guard(msg_id)
        return _r or _rpc_error(msg_id, -32600,
                                "invalid request: method must be a string")

    # RE-GATE-03（三轮复审）：元方法（initialize/notifications/未知
    # method）与 tools/list 统一计读档——认证入口不存在不限速分支；
    # tools/call 的能力级读/写档在 call 分支单独计，不在此重复扣
    if method != "tools/call":
        import math as _math
        _meta_wait = _gate.check_rate("read", principal.principal_id)
        _meta_metered = True  # 本请求读档已计（协议错误分支共享）
        if _meta_wait > 0:
            _ws = max(1, _math.ceil(_meta_wait))
            return _gate_rpc_error(
                msg_id, "RATE_LIMITED",
                f"请求过于频繁（读档），{_ws}s 后重试", 429, _ws)

    allowed = PROFILE_PRINCIPALS[profile]
    if principal.principal_id not in allowed:
        _r = _proto_rate_guard(msg_id)
        return _r or _rpc_error(
            msg_id, -32002,
            f"binding principal not allowed on this profile")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "mariposa MCP；工具与 HTTP API 共用同一业务注册表。"
                "出站信封协商（1005B）：tools/call 请求 params._meta."
                'content_envelope="single" 时响应省略 content 里的完整'
                "JSON 副本（structuredContent 为唯一负载，content 仅留"
                "一行指针文本）；缺省保持 content+structuredContent 双份"
                "（MCP 规范的向后兼容通道，不赌客户端读取面）"),
        }
        return _rpc_result(msg_id, result)

    if method == "notifications/initialized" or method.startswith("notifications/"):
        return JSONResponse(status_code=202, content=None)

    if method == "tools/list":
        # 读档已在 method 级统一计（RE-GATE-03 调整）
        return _rpc_result(msg_id, {"tools": _tools_for(principal)})

    if method == "tools/call":
        params = body.get("params") or {}
        if not isinstance(params, dict):
            _r = _proto_rate_guard(msg_id)
            return _r or _rpc_error(msg_id, -32600,
                                    "invalid params: must be an object")
        name = str(params.get("name", ""))
        # RA-008：缺省与 false/[]/0 区分——显式非 object 一律拒绝，
        # false 不得经 or {} 变空参执行成写请求
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            _r = _proto_rate_guard(msg_id)
            return _r or _rpc_error(msg_id, -32600,
                                    "invalid params: arguments must be"
                                    " an object")
        canonical = _canonical_name(name)
        # GATE-01：成功调用按能力分类计档（读扣读档、写扣写档，
        # 与 HTTP /api 通道同一档位语义）
        _cap = registry.REGISTRY.get(canonical)
        _kind = "write" if (_cap is not None and _cap.write) else "read"
        _wait = _gate.check_rate(_kind, principal.principal_id)
        if _wait > 0:
            import math as _math
            _ws = max(1, _math.ceil(_wait))
            return _gate_rpc_error(
                msg_id, "RATE_LIMITED",
                f"{'写入操作' if _kind == 'write' else '请求'}过于频繁，"
                f"{_ws}s 后重试", 429, _ws)
        try:
            # P2-03（2026-10-05 审计）：同 HTTP invoke——同步 handler 让出
            # 事件循环（幂等待/DB busy 不再冻结整个服务）
            # default_output_profile="compact_v1"（审计 1005B）：MCP 面
            # 默认瘦身投影——白名单外能力在 invoke 内静默回退 legacy，
            # 显式 output_profile=legacy 永远可退回全量
            import asyncio as _asyncio
            out = await _asyncio.to_thread(
                registry.invoke, principal, canonical, arguments,
                params.get("_client_idempotency_key"),
                default_output_profile="compact_v1")
        except MariposaError as e:
            # 业务错误走 MCP tool error 路径，不是协议错误
            return _rpc_result(msg_id, {
                "content": [{"type": "text",
                             "text": json.dumps(
                                 {"ok": False, "error": {"code": e.code,
                                                         "message": str(e)}},
                                 ensure_ascii=False, default=str,
                                 separators=(",", ":"))}],
                "isError": True,
            })
        # 出站信封协商（1005B）：content 的完整 JSON 副本默认保留
        # （MCP 规范向后兼容通道——不赌客户端只读 structuredContent）；
        # 客户端显式 params._meta.content_envelope="single"（见
        # initialize instructions）时省略副本，content 仅留指针行。
        # 序列化统一紧凑分隔符（默认 ", "/": " 每键值对白送 2 字符）。
        _meta = params.get("_meta")
        _single = (isinstance(_meta, dict)
                   and _meta.get("content_envelope") == "single")
        _content_text = ("see structuredContent" if _single else
                         json.dumps(out, ensure_ascii=False,
                                    separators=(",", ":")))
        return _rpc_result(msg_id, {
            "content": [{"type": "text", "text": _content_text}],
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


def _gate_rpc_error(msg_id, machine_code: str, message: str,
                    status: int, retry_after: int) -> JSONResponse:
    """门禁类错误的 RPC 形态（GATE-02）：结构化 machine code +
    retry_after 数据 + Retry-After 头，HTTP 状态不再是默认 200。"""
    return JSONResponse(
        {"jsonrpc": "2.0", "id": msg_id,
         "error": {"code": -32001, "message": f"{machine_code}: {message}",
                   "data": {"code": machine_code,
                            "retry_after": retry_after}}},
        status_code=status,
        headers={"Retry-After": str(max(1, retry_after))})
