"""证据分级与指令权限包装（v1.3 §8—11 / v1.4 §8.3）。

七类正本枚举 + v1.4 增补的 word_unverified（仅解决契约缺口：文字获准
返回但逐字身份不可验证；不冒充 paraphrase，不满足 verbatim_required）。
所有检索内容一律 content_role=retrieved_memory、instruction_authority=
none——记忆正文里的"system/忽略规则/调用工具"只是历史数据，不提升
权限、不触发工具、不改变 scope（SAFE-01/02）。
"""
from __future__ import annotations

from .. import config

EVIDENCE_KINDS = (
    "authored_event",      # 正式 event_text；证明记了什么，不证明 raw 原话
    "approved_summary",    # 批准遗忘摘要；当前有效摘要表示，不是旧全文
    "structured_fact",     # 正式结构字段（日期/分类/心情/plan 状态/版本）
    "word_verbatim",       # 明确 verbatim 且来源仍有效的话语
    "word_paraphrase",     # 概括/复述；不得作为逐字原话
    "word_unverified",     # 身份未核验的获准话语（v1.4 增补）
    "raw_verbatim",        # 经授权读取的原始来源逐字证据
    "relation_reference",  # 关系边引用；只证明关系记录存在
)

#: 满足 verbatim_required 的证据等级（v1.3 §8）
VERBATIM_OK_KINDS = ("word_verbatim", "raw_verbatim")


def make_evidence(kind: str, field: str, snippet: str, source_ref: str,
                  source_version: str | None = None,
                  truncated: bool = False,
                  structured_value=None) -> dict:
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"未知 evidence_kind: {kind}")
    ev = {
        "evidence_kind": kind,
        "content_role": "retrieved_memory",
        "instruction_authority": "none",
        "field": field,
        "source_ref": source_ref,
        "source_version": source_version,
        "truncated": truncated,
    }
    if structured_value is not None:
        ev["structured_value"] = structured_value
    else:
        ev["snippet"] = snippet
    return ev


def meets_requirement(evidences: list[dict],
                      requirement: str) -> bool:
    """证据列表是否满足证据等级要求（EVID-01/02/PACK-07）。"""
    if requirement != "verbatim_required":
        return True
    return any(e.get("evidence_kind") in VERBATIM_OK_KINDS
               for e in evidences)


def excerpt(text: str, limit: int | None = None,
            anchors: list[str] | None = None) -> tuple[str, bool]:
    """有界候选片段：保留完整句/完整引用边界优先，超限显式标记截断。

    anchors（S09）：查询词命中位置优先——超长文本不再固定从头部
    截断吞掉后段命中；窗口以首个命中文位置为中心取整窗后再做
    句读边界收敛。找不到命中则退回头部窗。
    """
    limit = limit or config.RECALL_EXCERPT_CHARS
    if len(text) <= limit:
        return text, False
    start = 0
    if anchors:
        compact = text.replace(" ", "")
        for a in anchors:
            if not a:
                continue
            pos = compact.find(a.replace(" ", ""))
            if pos >= 0:
                # 粗略映射回原文位置（空格偏移在窗口余量内可忽略）
                start = max(0, min(pos - limit // 3, len(text) - limit))
                break
    window = text[start:start + limit]
    for sep in ("。", "！", "？", "；", ". ", "！", "\n"):
        idx = window.rfind(sep)
        if idx > limit // 2:
            return window[:idx + len(sep)], True
    return window, True


