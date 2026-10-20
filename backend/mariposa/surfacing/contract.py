"""surface 准入合同 v2（她 2026-10-20 裁定 P0：判断/分组/选择/交付统一）。

本模块是**唯一**的准入判定：`judge._assess`、`selection.choose_winners`、
`models.CandidateAssessment.strictly_confirmed` 与 `service.validate` 都只
消费这里的 `admit()`，不再各自维护一份"五项全 True"清单（1019 审计分支
"合同只改一半"的根因）。

五个终态（每条候选恰一个）：
- REJECT_FACTS            任一事实维度 CONFLICT——先于且无视 JEV 分数
- NOT_SAME                JEV 明确否定（same_occurrence ≤ risk 门）
- UNKNOWN                 语义未确认 / 必需事实无法核验 / 证据不足
- CONFIRMED_SEMANTIC      JEV 确认 + 无冲突 + 必需事实 PASS + 证据 PASS
- CONFIRMED_DETERMINISTIC 预留给 P2 免 JEV 路径；本批**没有**任何代码能
                          产生它，不得据此宣称免 JEV 浮现已实现

语义三段（沿 main 原三段结构，确认门换 same_occurrence_threshold）：
≥ thr → confirmed；≤ risk → not_same；中间 → unknown。低于确认门只是
"未确认"，没有可靠反证不判否定。

竞争语义（selection 消费）：只有 UNKNOWN 构成竞争；REJECT_FACTS 与
NOT_SAME 已确知不是它，不阻断正确记忆。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

REJECT_FACTS = "reject_facts"
NOT_SAME = "not_same"
UNKNOWN = "unknown"
CONFIRMED_SEMANTIC = "confirmed_semantic"
CONFIRMED_DETERMINISTIC = "confirmed_deterministic"

OUTCOMES = (REJECT_FACTS, NOT_SAME, UNKNOWN, CONFIRMED_SEMANTIC,
            CONFIRMED_DETERMINISTIC)
CONFIRMED_OUTCOMES = frozenset({CONFIRMED_SEMANTIC, CONFIRMED_DETERMINISTIC})
#: 已确知不是它——不参与竞争、不阻断同分区赢家
NON_COMPETING_OUTCOMES = frozenset({REJECT_FACTS, NOT_SAME})

SEM_CONFIRMED, SEM_NOT_SAME, SEM_UNKNOWN = "confirmed", "not_same", "unknown"

#: 本合同出站的有限问句集合（prepare 只问 same_occurrence；delivery 加
#: relevant_now）。ambiguity_risk / participants / time_status /
#: sufficient_evidence / distinct / pair_same 全部退役：前四项改由 facts
#: 确定性校验，后两项随分组降级为分区而不再付费。
PREPARE_QUESTIONS = ("same_occurrence",)
DELIVERY_QUESTIONS = ("same_occurrence", "relevant_now")


def _valid_noul(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and float(value) == float(value) and 0.0 <= float(value) <= 1.0)


def semantic_outcome(noul, same_occurrence_threshold: float,
                     risk_threshold: float) -> str:
    if not _valid_noul(noul):
        return SEM_UNKNOWN
    v = float(noul)
    if v >= float(same_occurrence_threshold):
        return SEM_CONFIRMED
    if v <= float(risk_threshold):
        return SEM_NOT_SAME
    return SEM_UNKNOWN


@dataclass(frozen=True)
class Admission:
    outcome: str
    reason_code: str
    #: 本阶段是否可交付：CONFIRMED_* 且（若本阶段要求）relevant_now 为 True
    deliverable: bool


def admit(semantic: str, facts: dict, *, relevant_now: bool | None = None,
          require_relevance: bool = False) -> Admission:
    """单一准入判定。

    facts 形状见 facts.hard_checks：{"time": {"status", "required"},
    "participants": {"status", "required"}, "evidence": {"status"}}。
    status ∈ pass/conflict/unknown/not_required。
    """
    time_ = facts.get("time") or {}
    part = facts.get("participants") or {}
    evid = facts.get("evidence") or {}

    def finish(outcome: str, reason: str) -> Admission:
        deliverable = outcome in CONFIRMED_OUTCOMES
        if deliverable and require_relevance:
            if relevant_now is False:
                return Admission(outcome, "TOPIC_CHANGED", False)
            if relevant_now is not True:
                return Admission(outcome, "UNCLEAR_REFERENCE", False)
        return Admission(outcome, reason, deliverable)

    # 1. 硬冲突先于一切模型分数
    if time_.get("status") == "conflict":
        return finish(REJECT_FACTS, "HARD_CONFLICT_TIME")
    if part.get("status") == "conflict":
        return finish(REJECT_FACTS, "HARD_CONFLICT_PARTICIPANTS")
    # 2. 明确语义否定——无关候选在此退出，不再阻断同分区赢家
    if semantic == SEM_NOT_SAME:
        return finish(NOT_SAME, "NOT_SAME_OCCURRENCE")
    # 3. 当前消息提出了约束、却无法核验 → UNKNOWN（不是通过）
    if time_.get("required") and time_.get("status") == "unknown":
        return finish(UNKNOWN, "FACT_UNVERIFIED_TIME")
    if part.get("required") and part.get("status") == "unknown":
        return finish(UNKNOWN, "FACT_UNVERIFIED_PARTICIPANTS")
    # 4. 证据在语义路径上恒必需
    if evid.get("status") != "pass":
        return finish(UNKNOWN, "INSUFFICIENT_EVIDENCE")
    # 5. 语义
    if semantic == SEM_CONFIRMED:
        return finish(CONFIRMED_SEMANTIC, "EXACT_MATCH")
    return finish(UNKNOWN, "UNCLEAR_REFERENCE")


def verdict_for(outcome: str) -> str:
    """兼容旧消费者的三值 verdict。"""
    if outcome in CONFIRMED_OUTCOMES:
        return "confirmed"
    if outcome in NON_COMPETING_OUTCOMES:
        return "rejected"
    return "uncertain"


def contract_fingerprint(policy: dict, requested_model: str) -> str:
    """实际生效合同的内容指纹——进判断缓存键与 run 快照。

    任一项变化（阈值、prompt 标签、问句集、facts 规则版本、schema、
    模型别名）都换键；旧缓存行保留不删，靠身份隔离失效。
    """
    payload = {
        "schema": policy.get("judge_schema_version"),
        "policy_revision": policy.get("policy_revision"),
        "prompt_prepare": policy.get("prompt_prepare"),
        "prompt_delivery": policy.get("prompt_delivery"),
        "questions": {"prepare": list(PREPARE_QUESTIONS),
                      "delivery": list(DELIVERY_QUESTIONS)},
        "same_occurrence_threshold": policy.get("same_occurrence_threshold"),
        "positive_threshold": policy.get("positive_threshold"),
        "risk_threshold": policy.get("risk_threshold"),
        "facts_rules_version": policy.get("facts_rules_version"),
        "requested_model": requested_model,
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
