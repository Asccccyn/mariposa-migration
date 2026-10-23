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
- CONFIRMED_DETERMINISTIC P2 免 JEV 准备路径：显式日期、强事件身份、
                          事实证据与完整竞争集通过后才成立；交付仍复核语义

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
        "delivery_same_occurrence_threshold": policy.get("delivery_same_occurrence_threshold"),
        "positive_threshold": policy.get("positive_threshold"),
        "risk_threshold": policy.get("risk_threshold"),
        "facts_rules_version": policy.get("facts_rules_version"),
        "anchors_rules_version": policy.get("anchors_rules_version"),
        "deterministic_rules_version": policy.get("deterministic_rules_version"),
        "deterministic_max_date_set": policy.get("deterministic_max_date_set"),
        "requested_model": requested_model,
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ------------------------------------------------------- 确定性准入（P2）

#: 确定性路径的失败原因（全部落 UNKNOWN；它们是"证据不足正确弃权"的分类）
DET_NO_DATE_CONSTRAINT = "NO_DATE_CONSTRAINT"
DET_DATE_SET_TOO_LARGE = "DATE_SET_TOO_LARGE"
DET_LEXICAL_TRUNCATED = "LEXICAL_TRUNCATED"
DET_NO_CANDIDATE = "NO_CANDIDATE"
DET_NO_STRONG_IDENTITY = "NO_STRONG_IDENTITY"
DET_DATE_NOT_EXACT = "DATE_NOT_EXACT"
DET_EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
DET_PARTICIPANTS_UNVERIFIED = "PARTICIPANTS_UNVERIFIED"
DET_NOT_UNIQUE = "NOT_UNIQUE"


@dataclass(frozen=True)
class DeterministicAdmission:
    """确定性准入的**可核验记录**（她 1020：不能用一个布尔 confirmed=true
    取代判定依据）。outcome ∈ {CONFIRMED_DETERMINISTIC, UNKNOWN}。"""

    outcome: str
    reason: str
    rules: dict
    date_constraint: list
    candidate_ref: str | None
    candidate_version: str | None
    strong_match: dict | None
    competitors_checked: int
    non_competitors: int
    coverage: dict
    evidence_ref: str | None = None

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome, "reason": self.reason, "rules": self.rules,
            "date_constraint": self.date_constraint,
            "candidate_ref": self.candidate_ref,
            "candidate_version": self.candidate_version,
            "strong_match": self.strong_match,
            "competitors_checked": self.competitors_checked,
            "non_competitors": self.non_competitors,
            "coverage": self.coverage, "evidence_ref": self.evidence_ref,
        }


def admit_deterministic(candidates: list[dict], constraints: dict,
                        completeness: dict, policy: dict) -> DeterministicAdmission:
    """免 JEV 的确定性准入（她 1020 六处修订的合同实现）。

    candidates 每项：{resource_ref, content_version, date_confidence,
    facts (facts.hard_checks), strong_match (anchors.strong_identity_match
    的记录或 None), recall_hit (bool), evidence_ref}。candidates 必须是
    **未截断**的日期集 ∪ 全作用域词法命中（含日期集之外、无日期但强身份
    命中的候选——它们是竞争者）。

    全部条件：显式日期约束 / 完整性（日期集未截断且 ≤ 上限，词法
    complete_within_scope）/ 候选 date_confidence=exact 且 time pass /
    强事件身份（召回 n-gram 不算）/ 证据 pass / 人物非冲突且非"必需却
    未核验" / 竞争者为零。竞争者 = {time pass ∧ 有锚点关系} ∪
    {time unknown ∧ 强身份}；噪声 = {time conflict} ∪ {无锚点关系}，
    噪声不阻断、不当作 NOT_SAME，只计数。
    """
    rules = {"deterministic": policy.get("deterministic_rules_version"),
             "anchors": policy.get("anchors_rules_version"),
             "facts": policy.get("facts_rules_version")}
    dates = list(constraints.get("dates") or [])
    cov = {"date_set_size": int(completeness.get("date_set_size") or 0),
           "date_set_truncated": bool(completeness.get("date_set_truncated")),
           "lexical": completeness.get("lexical_coverage")}

    def unknown(reason: str, competitors=0, noise=0) -> DeterministicAdmission:
        return DeterministicAdmission(
            UNKNOWN, reason, rules, dates, None, None, None,
            competitors, noise, cov)

    if not dates:
        return unknown(DET_NO_DATE_CONSTRAINT)
    if (cov["date_set_truncated"]
            or cov["date_set_size"] > int(policy.get("deterministic_max_date_set") or 0)):
        return unknown(DET_DATE_SET_TOO_LARGE)
    if cov["lexical"] != "complete_within_scope":
        return unknown(DET_LEXICAL_TRUNCATED)

    identity: list[dict] = []
    competitors: list[dict] = []
    noise = 0
    blockers: list[str] = []
    for c in candidates:
        f = c.get("facts") or {}
        t = (f.get("time") or {}).get("status")
        p = f.get("participants") or {}
        e = (f.get("evidence") or {}).get("status")
        strong = c.get("strong_match")
        exact = c.get("date_confidence") == "exact"
        related = bool(c.get("recall_hit")) or strong is not None
        if not related:
            noise += 1
            continue
        if t == "conflict":
            if exact or strong is None:
                noise += 1               # 可靠日期明确冲突 / 仅弱召回 → 噪声
            else:
                competitors.append(c)    # 日期不可靠（inferred/unknown，含写入
                # 日回填）却强身份：不能用不可靠日期排除它 → 竞争者
            continue
        if t == "pass":
            if strong is None:
                blockers.append(DET_NO_STRONG_IDENTITY)
                competitors.append(c)
            elif not exact:
                blockers.append(DET_DATE_NOT_EXACT)
                competitors.append(c)
            elif e != "pass":
                blockers.append(DET_EVIDENCE_INSUFFICIENT)
                competitors.append(c)
            elif p.get("status") == "conflict" or (
                    p.get("required") and p.get("status") == "unknown"):
                blockers.append(DET_PARTICIPANTS_UNVERIFIED)
                competitors.append(c)
            else:
                identity.append(c)
        elif t == "unknown" and strong is not None:
            competitors.append(c)        # 无日期但强身份：可能同一事件
        else:
            noise += 1                   # time unknown 且仅召回命中 = 噪声

    if not identity:
        reason = blockers[0] if blockers else DET_NO_CANDIDATE
        return unknown(reason, len(competitors), noise)
    if len(identity) > 1 or competitors:
        return unknown(DET_NOT_UNIQUE, len(identity) - 1 + len(competitors), noise)
    w = identity[0]
    return DeterministicAdmission(
        CONFIRMED_DETERMINISTIC, "EXACT_MATCH", rules, dates,
        w["resource_ref"], str(w.get("content_version") or ""),
        w.get("strong_match"), 0, noise, cov, w.get("evidence_ref"))
