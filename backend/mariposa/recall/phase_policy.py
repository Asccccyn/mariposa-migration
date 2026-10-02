"""v1.7 阶段策略（§5）：整数 H / k / 明开回温 / 永久分支 / 6-5-4 字段。

移植自 v1.7 包 reference/phase_policy_reference.py（纯策略参考），
改为仓库事实适配：
- 分类使用仓库 ID（daily/sad/sweet/date/sex/reloplay/milestone/anniversary/
  plan），中文别名映射；reference 的显示标签一一对应。
- compute_phase 为纯函数：调用方提供**服务端核实过的**事实
  （recall/phase_facts.facts_of 负责从 DB 装载），本层不做授权、不查库。
- 阶段永不持久化为状态列；每次查询按当前事实计算（§5.1）。
- reloplay=7 天为工程初值（k/env 可调），不声称用户终值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from math import exp2
from typing import Iterable
from zoneinfo import ZoneInfo

from .. import config

POLICY_VERSION = "recall-phase-v1.7"

#: 仓库分类 ID -> 整数 H（v1.7 §5.1 冻结值；reloplay 为工程初值）
BASE_DAYS = {"daily": 20, "sex": 20, "sad": 30, "sweet": 30, "date": 60,
             "reloplay": 7}
#: 永久类别：不进数值公式（§5.2）
PERMANENT_CATEGORIES = frozenset({"milestone", "anniversary"})
#: 中文写法 -> 仓库 ID（输入容忍，库里只存 ID）
ALIASES = {"伤心": "sad", "伤心的事": "sad", "日常": "daily",
           "做爱": "sex", "甜蜜": "sweet", "约会": "date",
           "重大转折": "milestone", "纪念": "anniversary", "剧本": "reloplay"}
ALL_CATEGORIES = frozenset(BASE_DAYS) | PERMANENT_CATEGORIES | {"plan"}

#: 普通召回第一轮字段（§5.3 矩阵：WIDE 6 / MID 5 / CORE 4）
CORE_FIELDS = frozenset({"event_date", "categories", "mood_tags", "event_text"})
MID_FIELDS = CORE_FIELDS | {"original_title"}
WIDE_FIELDS = MID_FIELDS | {"our_words"}
FIELD_SETS = {"WIDE": WIDE_FIELDS, "MID": MID_FIELDS, "CORE": CORE_FIELDS}

PLAN_OPEN = frozenset({"planned", "active", "waiting", "blocked"})
PLAN_CLOSED = frozenset({"done", "cancelled"})
OWNERS = frozenset({"qiaosheng", "jiaming"})

ROUND2_REASONS = frozenset({
    "NO_DELIVERABLE_CANDIDATE", "EVIDENCE_INSUFFICIENT",
    "VERBATIM_REQUIRED_NOT_MET", "SOURCE_DISAMBIGUATION_NEEDED",
    "EXPLICIT_REJECT_AFTER_DELIVERY",
})


class PolicyError(ValueError):
    """坏参数不得变成隐式类别、日期或阶段。"""


class DataGap(PolicyError):
    """历史数据缺口：需显式修复，不用合成 30 天或今天兜底。"""


@dataclass(frozen=True)
class PhaseResult:
    stage: str                       # WIDE / MID / CORE
    age_days: int                    # 整数自然日差（上海）
    basis_date: str
    h_eff_days: int | None           # 永久/keep/plan 分支为 None
    reason: str
    first_round_fields: tuple[str, ...]
    business_date: str
    policy_version: str = POLICY_VERSION


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None \
            or value.utcoffset() is None:
        raise PolicyError(f"{label} must be timezone-aware")
    return value


def normalize_categories(categories: Iterable[str]) -> frozenset[str]:
    """九分类必填校验（§3.2）：非空、合法、多选去重；未知值拒绝。"""
    if isinstance(categories, (str, bytes)) or categories is None:
        raise PolicyError(
            "CATEGORY_REQUIRED: expected a nonempty category collection")
    values = list(categories)
    if not values or any(not isinstance(x, str) or not x.strip()
                         for x in values):
        raise PolicyError("CATEGORY_REQUIRED")
    result = frozenset(ALIASES.get(x, x) for x in values)
    unknown = result - ALL_CATEGORIES
    if unknown:
        raise PolicyError("UNKNOWN_CATEGORY: " + ",".join(sorted(unknown)))
    return result


def effective_days(base_days: int, k_percent: int = 100) -> int:
    """H_eff = ceil(H*k/100)，只做整数运算（bool/float 拒绝）。"""
    if type(base_days) is not int or base_days <= 0:
        raise PolicyError("base_days must be a positive integer")
    if type(k_percent) is not int or k_percent <= 0:
        raise PolicyError("k_percent must be a positive integer")
    return (base_days * k_percent + 99) // 100


def default_k_percent() -> int:
    """部署 k 默认 100（§5.1；改 k 需两位主体同意，不在此层授权）。"""
    try:
        return int(os.environ.get("MARIPOSA_RECALL_K_PERCENT", "100"))
    except ValueError:
        return 100


def default_reloplay_days() -> int:
    try:
        return int(os.environ.get("MARIPOSA_RELOPLAY_DAYS", "7"))
    except ValueError:
        return 7


def _tz() -> ZoneInfo:
    return ZoneInfo(config.RELATIONSHIP_TIMEZONE)


def compute_phase(*, now: datetime, first_held_at: datetime | None,
                  categories: Iterable[str],
                  last_explicit_open_at: datetime | None = None,
                  active_keep_owners: Iterable[str] = (),
                  resource_kind: str = "memory",
                  plan_status: str | None = None,
                  k_percent: int | None = None,
                  reloplay_days: int | None = None) -> PhaseResult:
    """按当前核实事实计算阶段；永久保护优先于 plan 默认（§5.2 顺序）。

    - keep（任一作者有效标记）→ WIDE（EXPLICIT_KEEP）
    - 重大转折/纪念 → WIDE（PERMANENT_CATEGORY）
    - plan 资源：未终态 WIDE；done/cancelled 状态变化即 CORE（无 20 天）
    - 普通桶：H_eff = max(有限类别H)（多选取最大，不累加）；
      D < H → WIDE；H ≤ D < 2H → MID；D ≥ 2H → CORE
    - basis = max(首次 hold 上海日, 最近有效明开上海日)（§5.4）
    """
    _aware(now, "now")
    k = default_k_percent() if k_percent is None else k_percent
    relo = default_reloplay_days() if reloplay_days is None else reloplay_days
    effective_days(1, k)
    effective_days(relo, 100)
    cats = normalize_categories(categories)
    if resource_kind not in {"memory", "plan"}:
        raise PolicyError("unknown resource_kind")
    if resource_kind == "plan":
        if "plan" not in cats or plan_status not in PLAN_OPEN | PLAN_CLOSED:
            raise DataGap(
                "PLAN_STATE_GAP: a plan needs a trusted state and plan"
                " category")
    elif plan_status is not None:
        raise PolicyError("a related plan state must not override an event"
                          " bucket")
    elif cats == {"plan"}:
        raise DataGap(
            "PLAN_MAPPING_GAP: a plan-only bucket needs an explicit resource"
            " mapping")
    if isinstance(active_keep_owners, (str, bytes)):
        raise PolicyError(
            "active_keep_owners must be a collection of verified owners")
    keep_owners = frozenset(active_keep_owners)
    if not keep_owners <= OWNERS:
        raise PolicyError("unknown keep owner")
    if first_held_at is None:
        raise DataGap("HELD_DATE_GAP")
    _aware(first_held_at, "first_held_at")
    if first_held_at > now:
        raise DataGap("FUTURE_HELD_AT")
    tz = _tz()
    dates = [first_held_at.astimezone(tz).date()]
    if last_explicit_open_at is not None:
        _aware(last_explicit_open_at, "last_explicit_open_at")
        if last_explicit_open_at > now:
            raise DataGap("FUTURE_OPEN_AT")
        dates.append(last_explicit_open_at.astimezone(tz).date())
    today = now.astimezone(tz).date()
    basis = max(dates)
    age = (today - basis).days

    def result(stage: str, reason: str, h: int | None = None) -> PhaseResult:
        return PhaseResult(stage, age, basis.isoformat(), h, reason,
                           tuple(sorted(FIELD_SETS[stage])),
                           today.isoformat())

    # 显式分支，不用超大 H 或 infinity（§5.2）
    if keep_owners:
        return result("WIDE", "EXPLICIT_KEEP")
    if cats & PERMANENT_CATEGORIES:
        return result("WIDE", "PERMANENT_CATEGORY")
    if resource_kind == "plan":
        if plan_status in PLAN_OPEN:
            return result("WIDE", "OPEN_PLAN")
        return result("CORE", "TERMINAL_PLAN")
    days = dict(BASE_DAYS)
    days["reloplay"] = relo
    timed = cats & set(days)
    if not timed:
        raise DataGap("NO_TIMED_CATEGORY")
    h = effective_days(max(days[c] for c in timed), k)
    stage = "WIDE" if age < h else "MID" if age < 2 * h else "CORE"
    return result(stage, "INTEGER_AGE", h)


def display_availability(result: PhaseResult) -> str | None:
    """可选展示 A=2^(-D/H)；不参与任何阶段/排序/过滤判断（§5.1）。"""
    if result.h_eff_days is None:
        return None
    return f"{exp2(-result.age_days / result.h_eff_days):.2f}"


def eligible_fields(result: PhaseResult) -> frozenset[str]:
    """普通 Round1 字段；raw 与 find_words 是独立域（§5.3.1）。"""
    return frozenset(result.first_round_fields)


def allow_raw_round2(*, same_session: bool, same_query_revision: bool,
                     same_scope: bool, round1_complete_receipt: bool,
                     judge_complete: bool, raw_search_authorized: bool,
                     budget_available: bool, reason: str) -> bool:
    """Round 2 gate（§6.4）：第二次调用≠Round 2；全部服务端条件为真才放行。"""
    return (same_session and same_query_revision and same_scope and
            round1_complete_receipt and judge_complete and
            raw_search_authorized and budget_available and
            reason in ROUND2_REASONS)


def find_words_stage_restricted() -> bool:
    """find_words 跨阶段全量 our_words 专项（§6.5）。"""
    return False


# ---------------------------------------------------------------- 事实装载

def facts_of(memory_id: str) -> dict:
    """从正式库装载 compute_phase 所需的**服务端核实事实**。

    keep 标记来自 memory_keeps（revoked_at IS NULL）；plan 状态只在
    resource_kind='plan' 时由调用方传入对应 plan 行，不在此做桶-plan 映射
    （PLAN_MAPPING_GAP 语义保留给迁移清单）。
    """
    from .. import db
    with db.formal() as conn:
        m = conn.execute(
            "SELECT held_at, last_explicit_open_at FROM memories"
            " WHERE memory_id=?", (memory_id,)).fetchone()
        if m is None:
            raise PolicyError(f"memory not found: {memory_id}")
        cats = [r["category"] for r in conn.execute(
            "SELECT category FROM memory_categories WHERE memory_id=?",
            (memory_id,))]
        keeps = [r["owner"] for r in conn.execute(
            "SELECT owner FROM memory_keeps WHERE memory_id=?"
            " AND revoked_at IS NULL", (memory_id,))]
        plan_states = [r["state"] for r in conn.execute(
            "SELECT p.state FROM plan_memory_links l JOIN plans p"
            " ON p.id=l.plan_id WHERE l.memory_id=?", (memory_id,))]
    held = m["held_at"]
    opened = m["last_explicit_open_at"]

    def _dt(raw):
        if not raw:
            return None
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt

    # 全量审计 P1-08：plan 分类的桶由链接的 plan 资源状态自管——
    # 事实装载带出 plan 绑定与开合状态（任一链接 plan 开着 = open）
    return {"first_held_at": _dt(held),
            "last_explicit_open_at": _dt(opened),
            "categories": cats,
            "active_keep_owners": keeps,
            "plan_states": plan_states}


def phase_of(memory_id: str, *, now: datetime | None = None,
             plan_status: str | None = None,
             resource_kind: str = "memory") -> PhaseResult:
    """便捷入口：装载事实并计算阶段。"""
    from datetime import timezone as _tzmod
    facts = facts_of(memory_id)
    # P1-08：有 plan 绑定的桶按 plan 资源计算（open→WIDE / 终态→CORE），
    # 事件 H 不参与；显式 resource_kind="plan" 的调用保持原语义
    plan_states = facts.get("plan_states") or []
    if plan_states and resource_kind == "memory":
        resource_kind = "plan"
        plan_status = ("active" if any(st in PLAN_OPEN
                                       for st in plan_states) else "done")
    return compute_phase(
        now=now or datetime.now(_tzmod.utc),
        first_held_at=facts["first_held_at"],
        categories=facts["categories"],
        last_explicit_open_at=facts["last_explicit_open_at"],
        active_keep_owners=facts["active_keep_owners"],
        resource_kind=resource_kind, plan_status=plan_status)


def facts_for_many(conn, memory_ids: list[str]) -> dict[str, dict]:
    """批量装载 compute_phase 事实（单连接三次查询，替代逐桶 phase_of
    的 N+1；F12）。返回 {memory_id: facts}。"""
    if not memory_ids:
        return {}
    marks = ",".join("?" * len(memory_ids))
    facts: dict[str, dict] = {mid: {
        "first_held_at": None, "last_explicit_open_at": None,
        "categories": [], "active_keep_owners": [],
        "plan_states": []} for mid in memory_ids}

    def _dt(raw):
        if not raw:
            return None
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt

    for r in conn.execute(
            f"SELECT memory_id, held_at, last_explicit_open_at FROM"
            f" memories WHERE memory_id IN ({marks})", memory_ids):
        f = facts.get(r["memory_id"])
        if f:
            f["first_held_at"] = _dt(r["held_at"])
            f["last_explicit_open_at"] = _dt(r["last_explicit_open_at"])
    for r in conn.execute(
            f"SELECT memory_id, category FROM memory_categories WHERE"
            f" memory_id IN ({marks})", memory_ids):
        f = facts.get(r["memory_id"])
        if f:
            f["categories"].append(r["category"])
    for r in conn.execute(
            f"SELECT memory_id, owner FROM memory_keeps WHERE"
            f" memory_id IN ({marks}) AND revoked_at IS NULL", memory_ids):
        f = facts.get(r["memory_id"])
        if f:
            f["active_keep_owners"].append(r["owner"])
    # P1-08：批量装载 plan 绑定状态（与 facts_of 同语义）
    for r in conn.execute(
            f"SELECT l.memory_id, p.state FROM plan_memory_links l"
            f" JOIN plans p ON p.id=l.plan_id"
            f" WHERE l.memory_id IN ({marks})", memory_ids):
        f = facts.get(r["memory_id"])
        if f:
            f["plan_states"].append(r["state"])
    return facts


def phase_from_facts(facts: dict, *, now: datetime,
                     **kw) -> PhaseResult:
    """用 facts_for_many 的结果直接计算阶段。"""
    plan_states = facts.get("plan_states") or []
    if plan_states:
        # P1-08：plan 绑定桶按 plan 资源自管（open→WIDE / 终态→CORE）
        return compute_phase(now=now,
                             first_held_at=facts["first_held_at"],
                             categories=facts["categories"],
                             last_explicit_open_at=facts[
                                 "last_explicit_open_at"],
                             active_keep_owners=facts["active_keep_owners"],
                             resource_kind="plan",
                             plan_status=("active" if any(
                                 st in PLAN_OPEN for st in plan_states)
                                 else "done"),
                             **kw)
    return compute_phase(now=now,
                         first_held_at=facts["first_held_at"],
                         categories=facts["categories"],
                         last_explicit_open_at=facts[
                             "last_explicit_open_at"],
                         active_keep_owners=facts["active_keep_owners"],
                         **kw)
