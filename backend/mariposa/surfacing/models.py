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
    """单个 (event, candidate) 的归一化判断结果（provider 无关）。"""

    event_id: str
    candidate_ref: str
    candidate_version: str
    verdict: str                      # confirmed | rejected | uncertain
    checks: dict = field(default_factory=dict)
    # checks: same_occurrence / participants_consistent / time_status_consistent
    #         / evidence_sufficient / ambiguity_resolved / relevant_now
    #         每项 True/False/None（None=unknown/缺项）
    rank_signal: float | None = None  # [0,1]，仅排序用，不是确认
    reason_code: str = "INSUFFICIENT_EVIDENCE"

    def strictly_confirmed(self) -> bool:
        """严格通过=verdict confirmed 且基础五项检查全 True。

        relevant_now 仅 delivery 阶段评估（存在时也必须为 True）；
        rank_signal 不参与确认（分数不是确认，R35）。
        """
        if self.verdict != "confirmed":
            return False
        if self.checks.get("relevant_now") is False:
            return False
        return all(self.checks.get(k) is True for k in (
            "same_occurrence", "participants_consistent",
            "time_status_consistent", "evidence_sufficient",
            "ambiguity_resolved"))


@dataclass
class EventGroup:
    event_id: str
    hypothesis_ids: list[str]
    grouping_confirmed: bool


@dataclass
class JudgeDecision:
    """两阶段判断的归一化内部结果（结构化 provider 与 TypeSafe 归一
    后同一形状；对应 judge-decision.schema.json）。"""

    schema_version: str
    phase: str
    event_groups: list[EventGroup]
    assessments: list[CandidateAssessment]


# 判断问句键（prompts/typesafe_questions.json 的实例化模板）
def q_hypothesis_distinct(h: str) -> str:
    return f"hypothesis_{h}_distinct_event"


def q_hypothesis_pair(h: str, j: str) -> str:
    return f"hypothesis_{h}_{j}_same_occurrence"


def q_event_candidate(c: str, suffix: str) -> str:
    return f"{c}_{suffix}"


#: TypeSafe 问句后缀 → 检查名 映射（relevant_now 仅 delivery 阶段）
QUESTION_SUFFIX_CHECKS = (
    ("same_occurrence", "same_occurrence"),
    ("participants", "participants_consistent"),
    ("time_status", "time_status_consistent"),
    ("sufficient_evidence", "evidence_sufficient"),
    ("ambiguity_risk", "ambiguity_resolved"),   # 反向问：risk 高=未消歧
    ("relevant_now", "relevant_now"),
)


def surface_policy_snapshot() -> dict:
    """政策快照：版本/阈值/上限一次取齐（prepare/validate 各自固定一份，
    提交前核对同一快照——沿 recall 政策纪元 fencing 先例）。"""
    return {
        "policy_revision": config.SURFACE_POLICY_REVISION,
        "policy_version": config.SURFACE_POLICY_VERSION,
        "prompt_prepare": config.SURFACE_PROMPT_PREPARE,
        "prompt_delivery": config.SURFACE_PROMPT_DELIVERY,
        "positive_threshold": config.SURFACE_POSITIVE_NOUL_THRESHOLD,
        "risk_threshold": config.SURFACE_RISK_NOUL_THRESHOLD,
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
