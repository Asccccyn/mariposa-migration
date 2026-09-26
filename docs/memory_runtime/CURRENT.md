# Mariposa 召回运行时·当前语义（CURRENT）

**性质：仓库内唯一现行召回说明（v1.4 §0：不保留多套并列有效语义）**
生效：2026-09-26｜上游正本：`Mariposa_记忆运行语义正本_v1.3.md` +
`Mariposa_分层混合召回_GLM实现路径_v1.4.md`

旧召回说明（v2.0.1 文档中的召回章节）凡与本文件冲突的句子已被本文件
覆盖；Git 历史保留旧版。v2.0.1 未被改变的存储、遗忘、审批、身份、
自然日规则继续有效。

## 1. 一句话

Chat（MCP）与 CC（estómago）等权调用同一 Recall Session 运行时：
查询分层校验 → 授权范围内作用域隔离的词法+向量粗召回 → 去重/RRF →
可选 Jev 精排（默认关闭）→ 代码门控 → 0—3 条带证据分级的候选卡 →
周家明判断；纠正只改本 session 临时状态，永不修改正式记忆。

## 2. 分层规则（现行）

- **Recall Session 正本只在 Mariposa**（`runtime/recall/recall.sqlite3`）。
  estómago 只携带 session ref / revision / version receipts 换窗重取，
  不建第二份正本。
- 检索限制与补查预算由 Mariposa session 层维护：一个 burst =
  原始+≤2 替代表达、初次+≤2 轮补查；每 session ≤3 burst、累计 9 轮。
  新 burst 必须绑定真实用户继续请求（`continue_request_ref`）。
- 查询分层：`explicit_constraints`（白名单字段可硬过滤）/
  `explicit_negative_constraints`（硬排除）/
  `resolved_references`（带来源与作用域）/
  `inferred_hints`（软提示，永不硬过滤，QUERY-01）/
  `alternate_queries`（不新增事实）/`unknowns`（不自动填成事实）。
  白名单外过滤字段报错不静默忽略；两个明确条件冲突返回 CONFLICT。
- 普通事件召回（未遗忘=事件正文，遗忘=批准摘要+forget_tags）与
  **独立 words 通道**互不混排（WORD-01）；mixed 返回时各通道证据
  角色独立（WORD-04）。raw 不是直接通道，只在 words 证据不足且
  显式开启授权时做专项补查（RAWX-01..04）。
- 证据分级八类：`authored_event / approved_summary / structured_fact /
  word_verbatim / word_paraphrase / word_unverified / raw_verbatim /
  relation_reference`。authored_event 不冒充原话（EVID-01）；
  paraphrase/unverified 不满足 verbatim_required（EVID-02/PACK-07）；
  来源失效的 verbatim 降级 word_unverified（EVID-03）。
- 全链路 `content_role=retrieved_memory`、`instruction_authority=none`
  （SAFE-01/02）：记忆正文里的指令只是历史数据。
- 遗忘后的 words 显式检索 = **disabled / PENDING_OWNER_DECISION**
  （唯一未决业务项；确认后直接改本文件对应章节，不新增第二套语义）。
- 检索/预览/关系浏览/版本校验不触发普通记忆续期（WORD-05）；
  plan 固定自然日期限不变。
- 无 raw 绑定的 verbatim 话语视为正式逐字记录（word_verbatim）；
  有绑定时按来源有效性校验。

## 3. 能力清单（Registry 现名）

`memory.recall.start/refine/reject/accept/navigate/status/close`、
`memory.words.recall/get`、`memory.context.validate`；
`memory.relations.read` 的现名等价物为 `memory.relations.list/trace`。
旧 `memory.recall`/`memory.search` 保持 event-only 兼容（返回补数据
角色标注）。MCP 名 = `mariposa_` + 点换下划线。

## 4. 开关（生产默认全关；隔离验收后分项启用）

```text
MARIPOSA_RECALL_ENABLED / MARIPOSA_WORDS_RECALL_ENABLED /
MARIPOSA_RAW_FALLBACK_ENABLED（默认 false）
MARIPOSA_RECALL_JUDGE_PROVIDER（默认 disabled）
MARIPOSA_ROOT（非 Windows 必须显式）+ MARIPOSA_ALLOW_CREATE（首建）
```

## 5. 状态分层（§12 + session 九状态）

检索结果状态（FOUND/AMBIGUOUS/CONFLICT/NO_MATCH_OBSERVED/DEGRADED/
BUDGET_EXHAUSTED/STALE_RETRY_REQUIRED/UNAVAILABLE）与 session 生命周期
状态分开；`degraded_reasons` 表达并存的降级因素（如
semantic_unavailable 与 judge_timeout 可共存）而不都升为 DEGRADED。

## 6. 失效与恢复

正式修订/遗忘/restore/撤权 → 读取时重校验回执（receipts vs 当前库），
失效引用不重放旧正文，受影响 session 转 STALE_RETRY_REQUIRED；
容器/进程重启后未过期 session 从宿主 runtime 库恢复（SESSION-04）；
TTL（24h，可配）只是运行状态清理，不影响正式记忆自然日期限。
