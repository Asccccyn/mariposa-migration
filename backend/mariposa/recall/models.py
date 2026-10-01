"""召回运行时数据模型与输入校验（v1.3 §3—4 / v1.4 §4、§9.1）。

QueryPlan 把「用户明确条件 / 排除条件 / 推测 / 替代表达 / 未知」分层
保存：明确条件可进硬过滤（白名单校验），推测只作软提示永不进过滤；
resolved_references 保留来源与作用域；unknowns 不被自动填成事实。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import Forbidden
from .. import config

CHANNELS = ("event", "words")
INTENTS = ("recall_event", "find_words", "mixed")
TEMPORAL_AXES = (
    "event_time",
    "source_conversation_time",
    "hold_time",
    "plan_time",
    "retention_time",
)
EVIDENCE_REQUIREMENTS = ("any", "summary_ok", "verbatim_required")
RAW_FALLBACK_MODES = ("off", "when_evidence_insufficient")

# 各通道允许的明确条件字段白名单（v1.4 §4.2）：不在白名单内的过滤字段
# 必须报错，不静默忽略。
CONSTRAINT_FIELDS_BY_CHANNEL = {
    "event": {"categories", "mood_tags", "event_date", "category_match",
              "mood_match"},
    "words": {"speaker", "source_date", "event_date"},
}
NEGATIVE_FIELDS_BY_CHANNEL = {
    "event": {"categories_excluded", "event_date_excluded"},
    "words": {"speaker_excluded", "source_date_excluded"},
}

SPEAKERS = ("jiaming", "qiaosheng")


@dataclass
class QueryPlan:
    request_ref: str | None
    original_request: str
    intent: str = "recall_event"
    channels: list[str] = field(default_factory=lambda: ["event"])
    semantic_query: str = ""
    lexical_terms: list[str] = field(default_factory=list)
    exact_phrases: list[str] = field(default_factory=list)
    explicit_constraints: dict = field(default_factory=dict)
    explicit_negative_constraints: dict = field(default_factory=dict)
    resolved_references: list[dict] = field(default_factory=list)
    inferred_hints: list[str] = field(default_factory=list)
    alternate_queries: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    temporal_axis: str = "event_time"
    evidence_requirement: str = "any"
    raw_fallback: str = "off"
    delivery_limit: int = config.RECALL_DELIVERY_LIMIT

    def to_dict(self) -> dict:
        return {
            "request_ref": self.request_ref,
            "original_request": self.original_request,
            "intent": self.intent,
            "channels": list(self.channels),
            "semantic_query": self.semantic_query,
            "lexical_terms": list(self.lexical_terms),
            "exact_phrases": list(self.exact_phrases),
            "explicit_constraints": dict(self.explicit_constraints),
            "explicit_negative_constraints": dict(
                self.explicit_negative_constraints),
            "resolved_references": list(self.resolved_references),
            "inferred_hints": list(self.inferred_hints),
            "alternate_queries": list(self.alternate_queries),
            "unknowns": list(self.unknowns),
            "temporal_axis": self.temporal_axis,
            "evidence_requirement": self.evidence_requirement,
            "raw_fallback": self.raw_fallback,
            "delivery_limit": self.delivery_limit,
        }


def _check_date_range(v, field_name: str) -> None:
    if not isinstance(v, dict):
        raise Forbidden(f"{field_name} 必须是 {{from,to}} 日期范围对象",
                        code="INVALID_ARGUMENT")
    for k in v:
        if k not in ("from", "to"):
            raise Forbidden(f"{field_name} 不认识字段 {k}",
                            code="INVALID_ARGUMENT")


def _validate_constraints(plan: dict, channel: str,
                          negative: bool) -> dict:
    src = plan.get("explicit_negative_constraints" if negative
                   else "explicit_constraints") or {}
    whitelist = (NEGATIVE_FIELDS_BY_CHANNEL if negative
                 else CONSTRAINT_FIELDS_BY_CHANNEL)[channel]
    unknown = sorted(set(src) - whitelist)
    if unknown:
        raise Forbidden(
            f"通道 {channel} 的明确{'负向' if negative else ''}条件含白名单外"
            f"字段 {unknown}；允许字段：{sorted(whitelist)}",
            code="INVALID_ARGUMENT", fields=unknown)
    out: dict = {}
    for k, v in src.items():
        if k in ("event_date", "source_date", "event_date_excluded",
                 "source_date_excluded"):
            if k.endswith("_excluded"):
                # 三轮复审#4：排除条件契约 = {{from,to}} 区间数组——
                # 非 list 此前被静默当单区间存下，检索层按 list 解析时
                # 又被逐项跳过（排除日期两条都漏出来即此路径）
                if not isinstance(v, list):
                    raise Forbidden(f"{k} 必须是 {{from,to}} 区间数组",
                                    code="INVALID_ARGUMENT")
                for item in v:
                    _check_date_range(item, k)
            else:
                _check_date_range(v, k)
        if k == "speaker" and (not isinstance(v, str)
                               or v not in SPEAKERS):
            raise Forbidden("speaker 只能是 jiaming/qiaosheng",
                            code="INVALID_ARGUMENT")
        if k == "speaker_excluded":
            # 三轮复审#4：契约统一为明确数组（检索层三处都按 list
            # 处理；字符串此前被容忍，等于静默失效）
            if (not isinstance(v, list)
                    or any(not isinstance(x, str) or x not in SPEAKERS
                           for x in v)):
                raise Forbidden(
                    "speaker_excluded 必须是 jiaming/qiaosheng 的"
                    "字符串数组", code="INVALID_ARGUMENT")
        out[k] = v
    return out


def validate_query_plan(plan: dict) -> dict:
    """服务端 QueryPlan 校验（v1.4 §4.2）。

    principal_binding / 预算 / 权限版本由服务端写入，不接受客户端声明。
    两个明确条件互相冲突时抛 CONFLICT 类错误，不自动挑一个；明确条件与
    推测冲突时保留明确条件（推测只是软提示，无需在此裁决）。
    """
    if not isinstance(plan, dict):
        raise Forbidden("query_plan 必须是对象", code="INVALID_ARGUMENT")
    original = plan.get("original_request")
    if not isinstance(original, str) or not original.strip():
        raise Forbidden("original_request 必填（原始请求必须保留）",
                        code="INVALID_ARGUMENT")
    channels = plan.get("channels") or ["event"]
    if not isinstance(channels, list) or not channels or any(
            c not in CHANNELS for c in channels):
        raise Forbidden(f"channels 只能包含 {list(CHANNELS)}",
                        code="INVALID_ARGUMENT")
    if "raw" in (plan.get("channels") or []):
        raise Forbidden("raw 不是直接检索通道；只能作为 words 证据不足时的"
                        "授权专项补查（raw_fallback）", code="INVALID_ARGUMENT")
    intent = plan.get("intent") or ("find_words" if channels == ["words"]
                                    else "mixed" if "words" in channels
                                    else "recall_event")
    if intent not in INTENTS:
        raise Forbidden(f"intent 必须是 {list(INTENTS)}", code="INVALID_ARGUMENT")
    axis = plan.get("temporal_axis", "event_time")
    if axis not in TEMPORAL_AXES:
        raise Forbidden(f"temporal_axis 必须是 {list(TEMPORAL_AXES)}",
                        code="INVALID_ARGUMENT")
    ev = plan.get("evidence_requirement", "any")
    if ev not in EVIDENCE_REQUIREMENTS:
        raise Forbidden(f"evidence_requirement 必须是 "
                        f"{list(EVIDENCE_REQUIREMENTS)}",
                        code="INVALID_ARGUMENT")
    rf = plan.get("raw_fallback", "off")
    if rf not in RAW_FALLBACK_MODES:
        raise Forbidden(f"raw_fallback 必须是 {list(RAW_FALLBACK_MODES)}",
                        code="INVALID_ARGUMENT")
    terms = plan.get("lexical_terms")
    if terms is not None and (not isinstance(terms, list) or any(
            not isinstance(t, str) for t in terms)):
        raise Forbidden("lexical_terms 必须是字符串数组", code="INVALID_ARGUMENT")
    alts = plan.get("alternate_queries")
    if alts is not None and (not isinstance(alts, list) or len(alts) >
                             config.RECALL_ALTERNATE_PER_BURST):
        raise Forbidden(f"alternate_queries 至多 "
                        f"{config.RECALL_ALTERNATE_PER_BURST} 条",
                        code="INVALID_ARGUMENT")
    hints = plan.get("inferred_hints")
    if hints is not None and (not isinstance(hints, list) or any(
            not isinstance(h, str) for h in hints)):
        raise Forbidden("inferred_hints 必须是字符串数组",
                        code="INVALID_ARGUMENT")

    for channel in channels:
        _validate_constraints(plan, channel, negative=False)
        _validate_constraints(plan, channel, negative=True)

    # 明确正负条件互相冲突（同字段同值域完全相反）→ CONFLICT
    for channel in channels:
        pos = (plan.get("explicit_constraints") or {})
        neg = (plan.get("explicit_negative_constraints") or {})
        date_pos = pos.get("event_date") or pos.get("source_date")
        date_neg = neg.get("event_date_excluded") or neg.get(
            "source_date_excluded")
        if date_pos and date_neg and isinstance(date_neg, list):
            for ex in date_neg:
                if (date_pos.get("from") and ex.get("from") and
                        date_pos["from"] == ex["from"] and
                        date_pos.get("to") == ex.get("to")):
                    raise Forbidden(
                        "明确条件自相冲突（同一日期区间既要求又排除）；"
                        "请最小澄清", code="CONFLICT")

    qp = QueryPlan(
        request_ref=plan.get("request_ref"),
        original_request=original,
        intent=intent,
        channels=list(dict.fromkeys(channels)),
            # S04/WP03：不静默回填——semantic_query 只认显式值，browse 不伪造语义查询
            semantic_query=plan.get("semantic_query") or "",
        lexical_terms=[t for t in (terms or []) if isinstance(t, str) and t],
        exact_phrases=[p for p in (plan.get("exact_phrases") or [])
                       if isinstance(p, str) and p],
        explicit_constraints={
            k: v for k, v in (plan.get("explicit_constraints") or {}).items()
            if k in CONSTRAINT_FIELDS_BY_CHANNEL.get(channels[0], set()) |
            CONSTRAINT_FIELDS_BY_CHANNEL.get(
                channels[-1] if len(channels) > 1 else "", set())},
        explicit_negative_constraints=dict(
            plan.get("explicit_negative_constraints") or {}),
        resolved_references=[r for r in
                             (plan.get("resolved_references") or [])
                             if isinstance(r, dict)],
        inferred_hints=[h for h in (hints or []) if isinstance(h, str) and h],
        alternate_queries=[a for a in (alts or [])
                           if isinstance(a, str) and a],
        unknowns=[u for u in (plan.get("unknowns") or [])
                  if isinstance(u, str) and u],
        temporal_axis=axis,
        evidence_requirement=ev,
        raw_fallback=rf,
        delivery_limit=max(0, min(int(plan.get("delivery_limit",
                                                config.RECALL_DELIVERY_LIMIT)),
                                  config.RECALL_DELIVERY_LIMIT)),
    )
    return qp.to_dict()


SESSION_STATUSES = (
    "ACTIVE", "AMBIGUOUS", "CONFLICT", "DEGRADED", "BUDGET_EXHAUSTED",
    "RESOLVED", "CANCELLED", "EXPIRED", "STALE_RETRY_REQUIRED",
)
ACTIVE_STATUSES = ("ACTIVE", "AMBIGUOUS", "CONFLICT", "DEGRADED",
                   "BUDGET_EXHAUSTED")

REJECT_TARGETS = ("candidate", "event", "word", "source_selection")
