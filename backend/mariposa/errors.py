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
        # ROOT-03（2026-10-04 二批）：显式 http_status 生效——此前
        # 被吞进 detail，声明长度超限等结构化状态码退回类默认 400
        if detail.get("http_status") is not None:
            try:
                self.http_status = int(detail["http_status"])
            except (TypeError, ValueError):
                pass
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


class Busy(MariposaError):
    """同幂等键的执行仍在进行（并发的另一方尚未回填终态）。"""

    code = "IDEMPOTENCY_IN_PROGRESS"
    http_status = 409

    def __init__(self):
        super().__init__("idempotent execution still in progress; retry")


class SnapshotStale(MariposaError):
    code = "SNAPSHOT_STALE"
    http_status = 409


class OutcomeUnknown(MariposaError):
    """同幂等键的执行疑似中途崩溃：副作用是否发生不明，需对账后才能重试。"""
    code = "OUTCOME_UNKNOWN"
    http_status = 409


class ProviderUnavailable(MariposaError):
    """外部 provider（写入/判断）不可用——T-EXT-02 合同预留
    （acceptance test_gate_cases2 锚定其存在；外部写入面接入时启用）。
    1005B 清扫误删后恢复：有验收锚定的预留类不是死代码。"""

    code = "PROVIDER_UNAVAILABLE"
    http_status = 503


class ViewReceiptInvalid(MariposaError):
    """查看回执无效：跨桶/跨身份/跨版本/过期/不存在。"""
    code = "VIEW_RECEIPT_INVALID"
    http_status = 403


class StaleOperation(MariposaError):
    """旧 operation 的保存响应已不满足当前状态（session 失效、候选版本或
    可见性变化），拒绝按原样重放；调用方应基于当前状态发起新操作。"""
    code = "OPERATION_REPLAY_STALE"
    http_status = 409


class DeleteBlocked(MariposaError):
    """目标存在正式跨域关系（五域任一），不允许物理删除；关系完整
    性优先于删除成功。应先解除关系再删（CB-038：canonical code 与
    v2.0 Relation 域用语对齐）。"""
    code = "DELETE_BLOCKED_BY_RELATIONS"
    http_status = 409


class AlreadyDecided(MariposaError):
    """删除申请已被另一个决定（approve/reject）终结；状态机不允许
    approved->rejected 或 rejected->approved。"""
    code = "DELETION_ALREADY_DECIDED"
    http_status = 409
