"""确定性硬校验 v2（facts-v2；她 2026-10-20 裁定 P0）。

分工：JEV 只判"是否同一次往事"（same_occurrence）；时间 / 人物 / 证据
由代码按**可确定事实**输出 pass / conflict / unknown / not_required，
再交 contract.admit 统一准入。

条件性必需（她的修订）：只有**当前消息提出了该维度的约束**，该维度才
必需——"2025年7月9日我们在一起的事" 必须核验日期（无法核验=unknown
阻断；矛盾=conflict）；"那次旅行你记得吗" 没给日期，时间维度 not_required
交语义与证据判断。人物同理："和某人一起" 是约束；"char，你记得吗" 的
称呼不是。

事实来源（已核实 schema/写入点）：`memory_date` 是作者给出的**事件日期**
（补记可给过去日期，与 created_at 分离），`occurred_start/end` 是事件
发生区间；两者同在时以区间为准，单日落在区间外视为数据不一致 → unknown。
不解析相对日期（"去年"）、不补年份——这些留给 P1/P2 的上下文解析。

已知口径限制：点分/斜杠形式（7.9 / 7/9）按 月.日 解释，与 est 侧
DATE_RE 同族；带字母前后缀（v1.2）不作日期。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PASS, CONFLICT, UNKNOWN, NOT_REQUIRED = (
    "pass", "conflict", "unknown", "not_required")

_CN_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM = "〇零一二两三四五六七八九十"

_FULL_CN = re.compile(
    rf"(\d{{4}}|[{_CN_NUM}]{{4}})\s*年\s*(\d{{1,2}}|[{_CN_NUM}]{{1,3}})\s*月"
    rf"\s*(\d{{1,2}}|[{_CN_NUM}]{{1,3}})\s*[日号]?")
_FULL_NUM = re.compile(
    r"(?<![\dA-Za-z])(\d{4})[-/.．](\d{1,2})[-/.．](\d{1,2})(?![\dA-Za-z])")
_MD_CN = re.compile(
    rf"(?<![\d{_CN_NUM}])(\d{{1,2}}|[{_CN_NUM}]{{1,3}})\s*月"
    rf"\s*(\d{{1,2}}|[{_CN_NUM}]{{1,3}})\s*[日号]?")
_MD_NUM = re.compile(
    r"(?<![\dA-Za-z.．/-])(\d{1,2})[.．/](\d{1,2})(?![\dA-Za-z.．/-])")

_PRONOUNS = frozenset({
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "咱们",
    "咱", "大家", "谁", "别人", "人家", "自己", "彼此", "对方"})
_WITH_SOMEONE = re.compile(
    r"(?:和|跟|与|同)\s*([\u4e00-\u9fa5]{1,4}?|[A-Za-z][A-Za-z.]{0,15}?)\s*"
    r"(?:一起|一块|一同|俩|两个|三个|几个|吃|喝|去|聊|见|玩|做|逛|散步|过|在)")


def _cn_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _CN_DIGIT.get(head, 1) if head else 1
        ones = _CN_DIGIT.get(tail, 0) if tail else 0
        if (head and head not in _CN_DIGIT) or (tail and tail not in _CN_DIGIT):
            return None
        return tens * 10 + ones
    total = 0
    for ch in s:
        d = _CN_DIGIT.get(ch)
        if d is None:
            return None
        total = total * 10 + d
    return total


@dataclass(frozen=True)
class DateConstraint:
    year: int | None
    month: int
    day: int

    def as_dict(self) -> dict:
        return {"year": self.year, "month": self.month, "day": self.day}

    @staticmethod
    def from_dict(d: dict) -> "DateConstraint":
        return DateConstraint(d.get("year"), int(d["month"]), int(d["day"]))


def _plausible(y: int | None, m: int | None, d: int | None) -> bool:
    return (m is not None and d is not None and 1 <= m <= 12
            and 1 <= d <= 31 and (y is None or 1900 <= y <= 2200))


def extract_dates(text: str) -> list[DateConstraint]:
    """从用户原话跨度提取显式日期约束（可多条，保持出现顺序，去重）。"""
    if not text:
        return []
    out: list[DateConstraint] = []
    taken: list[tuple[int, int]] = []

    def free(span):
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    for pat, with_year in ((_FULL_CN, True), (_FULL_NUM, True)):
        for m in pat.finditer(text):
            y, mo, d = _cn_int(m.group(1)), _cn_int(m.group(2)), \
                _cn_int(m.group(3))
            if _plausible(y, mo, d) and free(m.span()):
                taken.append(m.span())
                out.append(DateConstraint(y, mo, d))
    for pat in (_MD_CN, _MD_NUM):
        for m in pat.finditer(text):
            mo, d = _cn_int(m.group(1)), _cn_int(m.group(2))
            if _plausible(None, mo, d) and free(m.span()):
                taken.append(m.span())
                out.append(DateConstraint(None, mo, d))
    seen, dedup = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            dedup.append(c)
    return dedup


def extract_participants(text: str) -> list[str]:
    """从用户原话提取"和/跟/与/同 X 一起…" 具名人物；代词与句首称呼不算。"""
    if not text:
        return []
    names = []
    for m in _WITH_SOMEONE.finditer(text):
        name = m.group(1).strip()
        if name and name not in _PRONOUNS and name not in names:
            names.append(name)
    return names


def extract_constraints(span_text: str) -> dict:
    """可序列化约束（进 run 快照，交付时按当前状态重算）。"""
    return {"dates": [c.as_dict() for c in extract_dates(span_text)],
            "participants": extract_participants(span_text)}


# ---------------------------------------------------------------- 候选侧

def _iso_ymd(value) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    head = value[:10]
    if not (head[:4].isdigit() and head[4] == "-" and head[5:7].isdigit()
            and head[7] == "-" and head[8:10].isdigit()):
        return None
    y, m, d = int(head[:4]), int(head[5:7]), int(head[8:10])
    return (y, m, d) if _plausible(y, m, d) else None


def candidate_date_facts(candidate: dict) -> dict:
    """候选可比日期：区间优先；单日落在区间外 → inconsistent。"""
    single = _iso_ymd(candidate.get("memory_date"))
    start = _iso_ymd(candidate.get("occurred_start"))
    end = _iso_ymd(candidate.get("occurred_end"))
    if start and end and start <= end:
        if single and not (start <= single <= end):
            return {"kind": "inconsistent"}
        return {"kind": "range", "start": start, "end": end}
    if single:
        return {"kind": "single", "date": single}
    return {"kind": "none"}


def _match_single(c: DateConstraint, ymd: tuple[int, int, int]) -> bool:
    y, m, d = ymd
    if (m, d) != (c.month, c.day):
        return False
    return c.year is None or c.year == y


def _match_range(c: DateConstraint, start, end) -> bool | None:
    """True 落在区间；False 区间外；None 无年份且区间跨 ≥3 个日历年
    （月日无法定位，不视为通过）。"""
    if c.year is not None:
        return start <= (c.year, c.month, c.day) <= end
    years = range(start[0], end[0] + 1)
    if len(years) > 2:
        return None
    return any(start <= (y, c.month, c.day) <= end for y in years)


def check_time(candidate: dict, dates: list[DateConstraint]) -> str:
    if not dates:
        return NOT_REQUIRED
    facts = candidate_date_facts(candidate)
    if facts["kind"] in ("none", "inconsistent"):
        return UNKNOWN
    verdicts = []
    for c in dates:
        if facts["kind"] == "single":
            verdicts.append(_match_single(c, facts["date"]))
        else:
            verdicts.append(_match_range(c, facts["start"], facts["end"]))
    if any(v is True for v in verdicts):
        return PASS
    if all(v is False for v in verdicts):
        return CONFLICT
    return UNKNOWN


def check_participants(candidate: dict, names: list[str]) -> str:
    if not names:
        return NOT_REQUIRED
    have = candidate.get("participants")
    if not isinstance(have, (list, tuple, set)) or not have:
        # 候选池当前不携带参与人数据（已核实 candidates.py）——有约束
        # 而无可比物 = 无法核验，不是通过
        return UNKNOWN
    have_set = {str(x) for x in have}
    return PASS if all(n in have_set for n in names) else CONFLICT


_SUFFICIENT_KINDS = frozenset({"authored_event", "approved_summary"})


def check_evidence(candidate: dict) -> str:
    """证据充分 = 存在**可核验**的正式证据条目：

    - evidence_kind ∈ {authored_event, approved_summary}（生产键名是
      evidence_kind；兼容合成/旧形状的 kind）；
    - source_ref 非空；source_version 若携带须等于候选 content_version；
    - approved_summary 带 legacy_content_gap 标记 = 证据缺口，不算充分；
    - structured_fact / word_* / raw_* 不单独构成事件证据。
    证据不产生 conflict，只有 pass / unknown。
    """
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list):
        return UNKNOWN
    cand_ver = str(candidate.get("content_version") or "")
    for e in evidence:
        if not isinstance(e, dict):
            continue
        kind = e.get("evidence_kind") or e.get("kind")
        if kind not in _SUFFICIENT_KINDS:
            continue
        if not e.get("source_ref"):
            continue
        sv = e.get("source_version")
        if sv is not None and cand_ver and str(sv) != cand_ver:
            continue
        sval = e.get("structured_value")
        if (kind == "approved_summary" and isinstance(sval, dict)
                and sval.get("legacy_content_gap")):
            continue
        return PASS
    return UNKNOWN


def hard_checks(candidate: dict, constraints: dict) -> dict:
    """三维度确定性校验（contract.admit 的 facts 输入）。"""
    dates = [DateConstraint.from_dict(d) for d in constraints.get("dates") or []]
    names = list(constraints.get("participants") or [])
    return {
        "time": {"status": check_time(candidate, dates),
                 "required": bool(dates)},
        "participants": {"status": check_participants(candidate, names),
                         "required": bool(names)},
        "evidence": {"status": check_evidence(candidate)},
    }
