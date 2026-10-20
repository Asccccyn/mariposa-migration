"""事件锚点（anchors-v1；她 2026-10-20 二批复修订一）。

**召回词项**与**确认依据**严格分开：
- `recall_terms(span)`：原话去日期/回忆动词/疑问/代词/时间指代/虚词后，剩余
  中文段切 2-gram+3-gram（≤12，去重），拉丁整词。**只用于 BM25 找候选**；
  命中只表示"候选相关"，永不作为事件身份证明——"一起" 命中不证明
  "在一起" 与 "一起去了海边" 是同一件事。
- `strong_identity_match(span, candidate)`：三种可核验的强事件身份之一，
  返回 {kind, text, field} 记录或 None：
  (A) 候选 original_title 规范化后连续出现在原话中（标题是作者写的事件身份）；
  (B) 原话残余中 ≥4 个连续中文字的核心段，连续出现在候选标题或正文中；
  (C) 原话显式引用该候选（memory:<id> 或五位桶号）。
  不做同义（做成≠建成）、不扩词表、不引入模型；证明不了即 None → UNKNOWN。
"""
from __future__ import annotations

import re

from . import facts as _facts

RULES_VERSION = "anchors-v1"
MAX_RECALL_TERMS = 12
CORE_PHRASE_MIN_CJK = 4

_CJK = r"\u4e00-\u9fff"
_FUNCTION_WORDS = re.compile(
    r"还记得|记不记得|记得|想起|回想|回忆|有没有印象|印象|"
    r"为什么|什么时候|什么|怎么样|怎么|多少|哪里|哪|谁|啥|几|"
    r"我们|你们|他们|她们|咱们|大家|自己|我|你|他|她|它|"
    r"那件事|那时候|那阵子|那段|那回|那次|那天|那场|那件|那两|当时|"
    r"去年|前年|今年|明年|以前|以后|上次|下次|第一次|后来|然后|现在|"
    r"今天|明天|昨天|刚才|最近|"
    r"的|了|过|着|吗|呢|吧|啊|呀|哦|嘛|呗|"
    r"是不是|是|在|和|跟|与|同|把|被|给|对|也|都|就|还|又|再|很|太|"
    r"不|没有|没|有|这|那|个|们|来着|一下|一些|一点")
_PUNCT = re.compile(r"[\s,.，。！？!?;；:：、“”\"'‘’()（）\[\]【】<>《》—\-–/|\\~～·…]+")
_CJK_RUN = re.compile(rf"[{_CJK}]+")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")
_EXPLICIT_REF = re.compile(r"memory:([A-Za-z0-9_-]+)|(?<!\d)(\d{5})(?!\d)")


def _strip_dates(text: str) -> str:
    for pat in (_facts._FULL_CN, _facts._FULL_NUM, _facts._MD_CN, _facts._MD_NUM):
        text = pat.sub(" ", text)
    return text


def residual_runs(span: str) -> list[str]:
    """去日期、去虚词/代词/时间指代/回忆动词后的中文连续段（保持原序）。"""
    if not span:
        return []
    t = _strip_dates(span)
    t = _FUNCTION_WORDS.sub(" ", t)
    t = _PUNCT.sub(" ", t)
    return [m.group(0) for m in _CJK_RUN.finditer(t)]


def normalize(text: str) -> str:
    """身份比对用规范化：小写、去空白与标点；只保留中文/字母/数字。"""
    if not text:
        return ""
    return re.sub(rf"[^{_CJK}A-Za-z0-9]+", "", text).lower()


def recall_terms(span: str) -> list[str]:
    """BM25 召回词项：残余段的 3-gram 优先、再 2-gram，拉丁整词；≤12。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(t: str):
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    runs = residual_runs(span)
    for n in (3, 2):
        for run in runs:
            for i in range(len(run) - n + 1):
                add(run[i:i + n])
    for m in _LATIN_WORD.finditer(_strip_dates(span)):
        add(m.group(0).lower())
    return out[:MAX_RECALL_TERMS]


def strong_identity_match(span: str, candidate: dict) -> dict | None:
    """强事件身份：(C) 显式引用 > (A) 标题连续出现 > (B) ≥4 字核心段。"""
    if not span:
        return None
    mid = str(candidate.get("memory_id") or "")
    ref = str(candidate.get("resource_ref") or "")
    for m in _EXPLICIT_REF.finditer(span):
        tok = m.group(1) or m.group(2)
        if tok and (tok == mid or f"memory:{tok}" == ref):
            return {"kind": "binding_ref", "text": tok, "field": "memory_id"}

    span_norm = normalize(span)
    title_norm = normalize(candidate.get("original_title") or "")
    title_ok = (len(re.findall(rf"[{_CJK}]", title_norm)) >= 2
                or bool(_LATIN_WORD.search(title_norm)))
    if title_norm and title_ok and title_norm in span_norm:
        return {"kind": "title", "text": candidate.get("original_title"),
                "field": "original_title"}

    body_norm = normalize(candidate.get("event_text") or "")
    for run in residual_runs(span):
        if len(run) < CORE_PHRASE_MIN_CJK:
            continue
        if title_norm and run in title_norm:
            return {"kind": "core_phrase", "text": run, "field": "original_title"}
        if body_norm and run in body_norm:
            return {"kind": "core_phrase", "text": run, "field": "event_text"}
    return None
