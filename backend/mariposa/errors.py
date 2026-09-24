"""统一业务错误码（§17.5 的第一版子集）。"""
from __future__ import annotations


class MariposaError(Exception):
    code = "INTERNAL"
    http_status = 400

    def __init__(self, message: str, **detail):
        super().__init__(message)
        # 结构化错误码（OPS-02）：显式传入的 code 同时提升为实例属性
        # （HTTP/MCP 响应与调用方分支拿具体码）并保留在 detail 里
        # （既有调用方/测试按 detail["code"] 读取）。
        if detail.get("code"):
            self.code = detail["code"]
        self.detail = detail


class Unauthenticated(MariposaError):
    code = "UNAUTHENTICATED"
    http_status = 401


class Forbidden(MariposaError):
    code = "FORBIDDEN"
    http_status = 403


class VersionConflict(MariposaError):
    code = "VERSION_CONFLICT"
    http_status = 409


class ProposalStale(MariposaError):
    code = "PROPOSAL_STALE"
    http_status = 409


class ProposalAlreadyResolved(MariposaError):
    code = "PROPOSAL_ALREADY_RESOLVED"
    http_status = 409


class ProposalHashMismatch(MariposaError):
    code = "PROPOSAL_HASH_MISMATCH"
    http_status = 409


class IdempotencyConflict(MariposaError):
    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class NotFound(MariposaError):
    code = "NOT_FOUND"
    http_status = 404


class LockedResource(MariposaError):
    code = "LOCKED_RESOURCE"
    http_status = 403


class ProviderUnavailable(MariposaError):
    code = "PROVIDER_UNAVAILABLE"
    http_status = 503


class SnapshotStale(MariposaError):
    code = "SNAPSHOT_STALE"
    http_status = 409


class OutcomeUnknown(MariposaError):
    """同幂等键的执行疑似中途崩溃：副作用是否发生不明，需对账后才能重试。"""
    code = "OUTCOME_UNKNOWN"
    http_status = 409


class ViewRequired(MariposaError):
    """需要先明确打开并确认查看（例如写回忆）。"""
    code = "VIEW_REQUIRED"
    http_status = 403


class ViewReceiptInvalid(MariposaError):
    """查看回执无效：跨桶/跨身份/跨版本/过期/不存在。"""
    code = "VIEW_RECEIPT_INVALID"
    http_status = 403


class BindingStale(MariposaError):
    """原文绑定所依赖的源版本/hash已变化。"""
    code = "BINDING_STALE"
    http_status = 409


class RawContextConfirmationRequired(MariposaError):
    """展开隐藏原文需要先确认范围与预计token。"""
    code = "RAW_CONTEXT_CONFIRMATION_REQUIRED"
    http_status = 428
