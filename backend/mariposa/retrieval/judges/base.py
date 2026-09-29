"""JudgeProvider 协议与降级语义（v1.4 §6）。

Jev 是可替换的候选判断器，不是第二个自由行动的主模型：它只评价送到
面前的候选，救不回粗召回没找到的资源；判断不替代服务端权限、日期、
版本门控（这些由代码确定）。默认 disabled——不联网、不装 SDK、不下
载权重、不读新密钥（JEV-05）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class JudgeItem:
    candidate_ref: str
    candidate_version: str | None
    relevance_signal: float | None = None
    support_signal: float | None = None
    contradiction_signal: float | None = None
    provider_confidence: float | None = None
    confidence_kind: str = "unavailable"   # provider|not_applicable|unavailable
    evaluation_status: str = "unavailable"  # evaluated|partial|unavailable|invalid
    model_id: str = ""
    prompt_version: str = ""
    input_projection_version: str = ""
    receipt_id: str = ""

    def to_dict(self) -> dict:
        return {
            "candidate_ref": self.candidate_ref,
            "candidate_version": self.candidate_version,
            "relevance_signal": self.relevance_signal,
            "support_signal": self.support_signal,
            "contradiction_signal": self.contradiction_signal,
            "provider_confidence": self.provider_confidence,
            "confidence_kind": self.confidence_kind,
            "evaluation_status": self.evaluation_status,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "input_projection_version": self.input_projection_version,
            "receipt_id": self.receipt_id,
        }


@dataclass
class JudgeBatchResult:
    items: list[JudgeItem] = field(default_factory=list)
    provider_status: str = "unavailable"  # evaluated|partial|unavailable
    degraded_reason: str | None = None
    cache_hits: int = 0
    cache_misses: int = 0
    request_count: int = 0

    def to_dict(self) -> dict:
        return {"provider_status": self.provider_status,
                "degraded_reason": self.degraded_reason,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "request_count": self.request_count,
                "items": [i.to_dict() for i in self.items]}


class JudgeProvider:
    """精排 provider 接口；实现必须：缺项=None（不是 0 分）；越界/NaN/
    版本不一致 → invalid；不因低分自动淘汰（低分进 deferred）。"""

    name = "base"

    def judge(self, query_plan: dict, candidates: list[dict],
              execution_context: dict) -> JudgeBatchResult:
        raise NotImplementedError


class DisabledJudge(JudgeProvider):
    """默认 provider：不尝试任何网络/SDK/密钥访问。"""

    name = "disabled"

    def judge(self, query_plan: dict, candidates: list[dict],
              execution_context: dict) -> JudgeBatchResult:
        return JudgeBatchResult(provider_status="unavailable",
                                degraded_reason=None)


def sanitize_signal(value) -> float | None:
    """信号消毒（JEV-03）：非数值/NaN/越界 → None（unknown），不是低分。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    v = float(value)
    if v != v or v < -1.0 or v > 1.0:  # NaN 或越界
        return None
    return v


#: 测试/评测注入点（不进生产配置；离线对照用同一接口）
_INJECTED: dict[str, JudgeProvider] = {}


def register_for_tests(name: str, provider: JudgeProvider) -> None:
    _INJECTED[name] = provider


def clear_injected() -> None:
    _INJECTED.clear()


def get_provider():
    """按配置构造 provider；disabled/未配置 → DisabledJudge（无副作用）。"""
    from ... import config
    configured = config.RECALL_JUDGE_PROVIDER
    if configured in _INJECTED:
        return _INJECTED[configured]
    if configured == "disabled" or not configured:
        return DisabledJudge()
    if configured == "typesafe_jev":
        from . import typesafe_jev
        return typesafe_jev.TypeSafeJevJudge()
    return DisabledJudge()
