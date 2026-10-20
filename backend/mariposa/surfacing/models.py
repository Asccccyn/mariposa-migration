"""surface 内部模型：错误闭集、判断决策结构与政策快照。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import config
from ..errors import Forbidden


# 错误闭集（docs/06）——不整串发给主模型：自动层空交付+私有运行状态。
ERROR_CODES = frozenset({
    "SURFACE_DISABLED", "SURFACE_JUDGE_OFF", "SURFACE_PROFILE_UNSUPPORTED",
    "SURFACE_OUTBOUND_NOT_GRANTED", "SURFACE_BUDGET_UNCONFIGURED",
    "SURFACE_BUDGET_EXHAUSTED", "SURFACE_IDEMPOTENCY_CONFLICT",
    "SURFACE_PREPARE_NOT_READY", "SURFACE_TARGET_MISMATCH",
    "SURFACE_TARGET_CHANGED", "SURFACE_TARGET_MISSED",
    "SURFACE_NOT_RELEVANT_NOW", "SURFACE_UNCERTAIN",
    "SURFACE_EVENT_AMBIGUOUS", "SURFACE_VERSION_STALE",
    "SURFACE_ALREADY_COVERED", "SURFACE_BODY_TOO_LARGE",
    "SURFACE_TIMEOUT", "SURFACE_POLICY_CHANGED", "SURFACE_SCOPE_DENIED",
    "SURFACE_UNKNOWN_OUTCOME", "DETAIL_REF_NOT_SEEN", "DETAIL_NOT_FOUND",
    "DETAIL_SNAPSHOT_STALE", "DETAIL_SNAPSHOT_UNSTABLE",
    "DETAIL_PARTIAL_UNAVAILABLE",
})


class SurfaceBlocked(Exception):
    """自动路径阻断（空交付给调用方，不冒充"没有找到"）。

    code ∈ ERROR_CODES；details 仅含非秘密事实（ID/计数/版本）。
    """

    def __init__(self, code: str, **details):
        assert code in ERROR_CODES, code
        super().__init__(code)
        self.code = code
        self.details = details


def forbid_surface(code: str, **details):
    raise Forbidden(f"surface blocked: {code}", code=code, **details)


@dataclass
class EventHypothesis:
    hypothesis_id: str
    start_cp: int
    end_cp: int
    inherited_date_span: dict | None = None


@dataclass
class CandidateAssessment:
    """单个 (event, candidate) 的归一化判断结果（provider 无关）。

    v2：`outcome` 是 contract.admit 给出的五终态之一，是唯一准入依据；
    `verdict` 只是其三值投影（兼容旧消费者）。`checks` 保留旧键名供
    诊断，值从 facts/语义映射：pass→True、conflict→False、unknown/
    not_required→None。
    """

    event_id: str
    candidate_ref: str
    candidate_version: str
    verdict: str                      # confirmed | rejected | uncertain
    outcome: str = "unknown"          # contract.OUTCOMES
    semantic: str = "unknown"         # contract.SEM_*（JEV 三段结果）
    checks: dict = field(default_factory=dict)
    # checks: same_occurrence / participants_consistent / time_status_consistent
    #         / evidence_sufficient / relevant_now（delivery 阶段才有）
    facts: dict = field(default_factory=dict)   # facts.hard_checks 原样
    rank_signal: float | None = None  # [0,1]，仅排序用，不是确认
    reason_code: str = "INSUFFICIENT_EVIDENCE"
    resolved_model: str | None = None  # 本次判断实际应答的模型版本

    def strictly_confirmed(self) -> bool:
        """可交付 = outcome ∈ CONFIRMED_*，且 delivery 阶段 relevant_now 为
        True。与 selection / service 消费同一份 contract，不另列清单。"""
        from . import contract
        if self.outcome not in contract.CONFIRMED_OUTCOMES:
            return False
        if "relevant_now" in self.checks:
            return self.checks.get("relevant_now") is True
        return True


@dataclass
class EventGroup:
    """v2：分组降级为**分区**——每个假设一个事件、grouping_confirmed 恒
    True；它只是竞争域（同分区 UNKNOWN 才互相竞争），不证明是独立事件。
    跨分区同一 resource_ref 的赢家由 selection 合并为一条。"""

    event_id: str
    hypothesis_ids: list[str]
    grouping_confirmed: bool
    #: 该分区用户原话里的事实约束（facts.extract_constraints），进 run 快照
    constraints: dict = field(default_factory=dict)


@dataclass
class JudgeDecision:
    """两阶段判断的归一化内部结果（结构化 provider 与 TypeSafe 归一
    后同一形状；对应 judge-decision.schema.json）。"""

    schema_version: str
    phase: str
    event_groups: list[EventGroup]
    assessments: list[CandidateAssessment]
    resolved_model: str | None = None
    contract_fingerprint: str | None = None


def surface_policy_snapshot() -> dict:
    """政策快照：版本/阈值/上限一次取齐（prepare/validate 各自固定一份，
    提交前核对同一快照——沿 recall 政策纪元 fencing 先例）。

    v2 新增 same_occurrence_threshold / facts_rules_version /
    judge_schema_version：全部进 contract_fingerprint，任何一项漂移都
    使判断缓存换键、使 run 在 validate 处 SURFACE_POLICY_CHANGED。
    """
    return {
        "policy_revision": config.SURFACE_POLICY_REVISION,
        "policy_version": config.SURFACE_POLICY_VERSION,
        "prompt_prepare": config.SURFACE_PROMPT_PREPARE,
        "prompt_delivery": config.SURFACE_PROMPT_DELIVERY,
        "judge_schema_version": config.SURFACE_JUDGE_SCHEMA_VERSION,
        "facts_rules_version": config.SURFACE_FACTS_RULES_VERSION,
        "anchors_rules_version": config.SURFACE_ANCHORS_RULES_VERSION,
        "deterministic_rules_version": config.SURFACE_DETERMINISTIC_RULES_VERSION,
        "deterministic_max_date_set": config.SURFACE_DETERMINISTIC_MAX_DATE_SET,
        "dense_queries_per_job": config.SURFACE_DENSE_QUERIES_PER_JOB,
        "positive_threshold": config.SURFACE_POSITIVE_NOUL_THRESHOLD,
        "risk_threshold": config.SURFACE_RISK_NOUL_THRESHOLD,
        "same_occurrence_threshold": config.SURFACE_SAME_OCCURRENCE_THRESHOLD,
        "candidate_union_cap": config.SURFACE_CANDIDATE_UNION_CAP,
        "per_hypothesis_cap": config.SURFACE_CANDIDATES_PER_HYPOTHESIS_CAP,
        "max_hypotheses": config.SURFACE_MAX_EVENT_HYPOTHESES,
        "max_queries": config.SURFACE_MAX_RETRIEVAL_QUERIES_PER_JOB,
        "judge_batch_size": config.SURFACE_JUDGE_BATCH_SIZE,
        "max_judge_request_bytes": config.SURFACE_MAX_JUDGE_REQUEST_BYTES,
        "max_judge_job_input_bytes": config.SURFACE_MAX_JUDGE_JOB_INPUT_BYTES,
        "max_event_body_bytes": config.SURFACE_MAX_EVENT_BODY_BYTES,
        "max_source_context_bytes": config.SURFACE_MAX_SOURCE_CONTEXT_BYTES,
        "daily_judge_request_cap": config.SURFACE_DAILY_JUDGE_REQUEST_CAP,
        "daily_judge_cost_cap_usd": config.SURFACE_DAILY_JUDGE_COST_CAP_USD,
    }
