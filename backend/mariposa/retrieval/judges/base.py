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


def required_excerpt_roles(candidate: dict) -> frozenset:
    """候选**必要证据角色**集合（无副作用共享判定，CR-01-R1/R2/R3）。

    语义以 2026-10-05 三轮复审的六场景 × 32 许可子集实测矩阵为准
    （fresh 交付 ⟺ 必要角色齐备）：
    - raw/source 通道 → source_excerpt；
    - words 专项通道 → word_excerpt；
    - **其余全部 event 卡——包括 our_words 命中（word-target）与
      event_text+our_words 双命中——恒为 event_excerpt**：fresh 对
      word-target 在 event 许可下交付（match 段可由事件正文回退
      支撑），缺 event 许可时不交付。matched_fields 里出现
      our_words **不改变通道归属**，不得当成 words 专项（CR-01-R3
      上下两版的教训：双必要误杀 event-only 格、单凭 word 许可
      又漏正文——正解是通道单一真源）。
    """
    channel = candidate.get("channel") or "event"
    # 导航卡（temporal_navigate）：结构事实卡——fresh 的 navigate
    # 不经 judge、无文本段外发（evidence snippet 恒空），任何
    # profile 下都交付；重放不得按 event 通道误杀（自查②，2026-10-05）
    if "temporal_navigate" in (candidate.get("matched_by") or []):
        return frozenset()
    if channel in ("raw", "source"):
        return frozenset({"source_excerpt"})
    if channel in ("word", "words"):
        return frozenset({"word_excerpt"})
    return frozenset({"event_excerpt"})
