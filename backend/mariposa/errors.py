"""统一业务错误码（§17.5 的第一版子集）。"""
from __future__ import annotations


class MariposaError(Exception):
    code = "INTERNAL"
    http_status = 400

    def __init__(self, message: str, **detail):
        super().__init__(message)
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
