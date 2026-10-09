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

    def outbound_grants(self) -> frozenset:
        """该 provider 自身获准外发的数据许可集合（provider 无关接口，
        MANUAL_HANDOFF_JUDGE_SWITCH_V1 §4.4）。

        原文 Round2 的 source_excerpt 许可按**当前所选 provider 自己的**
        许可集核验——不再 isinstance(TypeSafeJevJudge)；许可分立配置，
        批准给 Jev 的数据许可不自动转授其他 provider。默认空集=无任何
        外发许可（fail-closed）。
        """
        return frozenset()


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
    """按配置构造 provider；disabled/未配置 → DisabledJudge（无副作用）。

    MANUAL_HANDOFF_JUDGE_SWITCH_V1（2026-10-08）后本函数只服务
    env 兼容路径与旧测试；生产判断层入口是 recall.judge_policy
    政策表（get_provider_by_name）——政策 enabled=false 时上层根本
    不构造任何 provider（零判断调用）。
    """
    from ... import config
    configured = config.RECALL_JUDGE_PROVIDER
    return get_provider_by_name(configured)


def get_provider_by_name(name: str | None):
    """按政策所选 provider 名构造（未配置/未安装 → DisabledJudge）。

    调用方负责先读政策：enabled=false 时不进入本函数。
    WP-02（JFA-002 接线）：codex_sdk 真构造（与 readiness 同源；SDK
    未装 → DisabledJudge，细因由 provider_readiness 的 sdk_not_installed
    披露——不静默换 provider）。
    """
    if name in _INJECTED:
        return _INJECTED[name]
    if not name or name in ("disabled",):
        return DisabledJudge()
    if name == "typesafe_jev":
        from . import typesafe_jev
        return typesafe_jev.TypeSafeJevJudge()
    if name == "codex_sdk":
        try:
            from . import codex_sdk
        except ImportError:
            return DisabledJudge()
        if not codex_sdk.sdk_available():
            # SDK 未装 → DisabledJudge（阻断语义）；细因由
            # provider_readiness 的 sdk_not_installed 披露
            return DisabledJudge()
        return codex_sdk.CodexSdkJudge()
    return DisabledJudge()


def apply_policy_overrides(provider, *, model_id: str | None = None,
                           allowed_data=None):
    """WP-02（CX-03）：政策 model_id/allowed_data 注入已构造的 provider
    （政策传入即胜出——政策唯一正本，env 仅首导/回落；未传不动）。

    独立于构造器参数的注入通道：测试注入的 fake 实例（无 apply_policy）
    原样返回不受影响。
    """
    ap = getattr(provider, "apply_policy", None)
    if callable(ap):
        ap(model_id=model_id, allowed_data=allowed_data)
    return provider
