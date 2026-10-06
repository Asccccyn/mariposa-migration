"""compact_v1 出站投影层（JSON 瘦身，2026-10-04 五——review4 方案）。

架构约束（方案原文）：
- 纯展示投影：业务操作、权限/开关/版本/Judge 重校验全部完成后才投影；
  canonical 结果、operation 回执、业务存储保留完整信息——精简包
  不写幂等回执、不喂检索/状态机；
- profile 与业务载荷隔离：output_profile 在 schema 校验与 payload
  哈希之前剥离——同 operation 切换 profile 不重做业务、不触发
  REF_REUSE_MISMATCH；
- 不做：短键缩写、位置数组、正文改写、机械删 null/false、absence
  冒充未知；安全信封（content_role/instruction_authority）逐页保留。

白名单式只删诊断字段：
- Bootstrap：固定 policy 块（工具说明承担）、state_hash、估算值、
  可由当前页推导的 count；
- Recall：query_fingerprint/token_count/coverage 内部统计（_ 前缀
  与 lexical_scorer/stage_filter/event_pool/judge_cache/dense_
  pending_vectors）、候选 scores/rrf_score、judge 的 model/prompt
  版本；excerpt 仅在与首个 evidence.snippet 逐值相同时删除（正文
  唯一完整副本保留在 evidence）；
- Source：同会话列表的 provider/会话 id 页级上提（≥3 条试点）。
"""
from __future__ import annotations

import copy

#: 支持 compact_v1 的能力（白名单——不猜形状）
_COMPACT_CAPS = frozenset({
    "bootstrap.get", "bootstrap.next",
    "memory.recall.start", "memory.recall.refine",
    "memory.recall.status", "memory.recall.navigate",
    "memory.recall.round2", "memory.words.recall",
    "memory.find_words",
    "source.search",
})

_RECALL_PACKET_DROP = ("query_fingerprint", "token_count", "created")
_COVERAGE_DROP_KEYS = ("lexical_scorer", "stage_filter", "event_pool",
                       "judge_cache", "dense_pending_vectors")
_CANDIDATE_DROP = ("scores", "rrf_score")
# judge 字段名以 judges/base.py 的 to_dict() 为准（审计 1005B 曾把
# confidence_kind 误写成不存在的 provider_confidence_kind——删除变
# 空操作；provider_confidence 未评估时恒 null，一并删除）
_JUDGE_DROP = ("model_id", "prompt_version", "confidence_kind",
               "provider_confidence")

_BOOTSTRAP_DROP = ("state_hash", "estimated_tokens", "policy")


def supports(capability: str) -> bool:
    return capability in _COMPACT_CAPS


def project(capability: str, payload):
    """出站投影：深拷贝后删诊断字段；不认识的形状原样返回。"""
    if not supports(capability) or not isinstance(payload, dict):
        return payload
    out = copy.deepcopy(payload)
    if capability.startswith("bootstrap."):
        _project_bootstrap(out)
    elif capability.startswith("memory."):
        _project_recall(out)
    elif capability == "source.search":
        _project_source_search(out)
    return out


def _project_bootstrap(out: dict) -> None:
    for k in _BOOTSTRAP_DROP:
        out.pop(k, None)
    md = out.get("memory_days")
    if isinstance(md, dict):
        md.pop("mode", None)
        md.pop("section_limit", None)
        md.pop("fields", None)
        md.pop("note", None)
        # count 可由当前页 items 长度推导（remaining/total 窗口总量保留）
        md.pop("count", None)
    pl = out.get("plans")
    if isinstance(pl, dict):
        pl.pop("mode", None)
        pl.pop("section_limit", None)
        pl.pop("upcoming_days", None)  # 工具说明承担
        pl.pop("count", None)          # 当前页可推导
    ann = out.get("anniversaries")
    if isinstance(ann, dict):
        ann.pop("upcoming_days", None)
    out.pop("cursor", None)  # 顶层恒为 None 的占位（续页游标在各段内）


def _project_recall(out: dict) -> None:
    # 包可能再包一层 {"data": packet}（runtime operation 形态）
    target = out.get("data") if isinstance(out.get("data"), dict) else out
    for k in _RECALL_PACKET_DROP:
        target.pop(k, None)
        out.pop(k, None)
    # budget.output_limits 是配置常量回显（~200B/包，消费者无需）；
    # 动态标志（budget_truncated 等）保留
    budget = target.get("budget")
    if isinstance(budget, dict):
        budget.pop("output_limits", None)
    cov = target.get("coverage")
    if isinstance(cov, dict):
        for k in _COVERAGE_DROP_KEYS:
            cov.pop(k, None)
        for k in list(cov.keys()):
            if k.startswith("_"):
                cov.pop(k)
    for c in target.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        for k in _CANDIDATE_DROP:
            c.pop(k, None)
        j = c.get("judge")
        if isinstance(j, dict):
            for k in _JUDGE_DROP:
                j.pop(k, None)
        # excerpt 与首个 evidence.snippet 逐值相同才删（唯一完整正文
        # 保留在 evidence；不同值说明 excerpt 另有语义，不动）
        evs = c.get("evidence") or []
        if (evs and isinstance(evs[0], dict)
                and c.get("excerpt") == evs[0].get("snippet")):
            c.pop("excerpt", None)


def _project_source_search(out: dict) -> None:
    hits = out.get("hits")
    if not isinstance(hits, list) or len(hits) < 3:
        return  # 小列表保持原形（试点下限）
    convs = {(h.get("provider"), h.get("provider_conversation_id"))
             for h in hits if isinstance(h, dict)}
    if len(convs) != 1:
        return  # 跨会话结果不上提
    provider, conv_id = next(iter(convs))
    for h in hits:
        h.pop("provider", None)
        h.pop("provider_conversation_id", None)
    out["conversation"] = {"provider": provider,
                           "provider_conversation_id": conv_id}
