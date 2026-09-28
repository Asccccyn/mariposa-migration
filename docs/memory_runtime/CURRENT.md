# Mariposa 记忆运行时·当前语义（CURRENT）

**性质：仓库内唯一现行说明（v1.7 §0：不保留多套并列有效语义）**
生效：2026-09-28｜上游正本：《Mariposa_v1.7 最终执行包》
（`LATEST_DECISIONS.json` + 主执行文档）；历史版本（v1.3/v1.4/v2.0.1）
只作代码定位与证据，凡与本文件冲突的旧句已被覆盖。

## 1. 一句话

不遗忘、不压缩、不生成摘要；记忆按九分类整数周期在 WIDE→MID→CORE
间**现算**淡出（可检索字段逐层收窄，不是删除）；找话是全量 our_words
专项；原文（Source 层）是二轮深搜与证据展开层，永不进第一轮普通召回；
所有候选正文出站前必经一层 Jev。

## 2. 分层规则（现行）

- **Recall Session 正本只在 Mariposa**（`runtime/recall/`）；estómago/CC
  只带 session ref 换窗重取。七动作（start/refine/reject/accept/
  navigate/status/close）与预算/继续请求约束沿用 v1.4 §9 机制不变。
- **删除链已退役（v1.7）**：自动遗忘、二次压缩、摘要生产/审查/发布、
  林石见审查角色整链删除（migration 16/19/20 + workspace 4）；历史
  forgotten_summary 表示读侧标 `LEGACY_CONTENT_GAP`，恢复走一次性
  离线迁移（offline 工具），无在线 restore。
- **明开回温**：`memory.open` → `view.confirm` 的服务端确认时刻写入
  `memories.last_explicit_open_at`（取 max 防倒退，同票据幂等不刷新）；
  命中/Jev/hydrate/bootstrap/预览/accept 都不算打开。

## 3. 阶段策略（`recall/phase_policy.py`，每次查询现算、永不持久化）

- 九分类必填：daily=20 / sex=20 / sad=30 / sweet=30 / date=60 /
  **reloplay=7（工程初值）** 天；milestone/anniversary 永久；plan 由
  计划资源状态自管。多选取最大 H，不累加；k=100（改 k 需两位主体同意）。
- 分支顺序：作者 keep > 永久类别 > plan（未终态 WIDE / done|cancelled
  即时 CORE）> 整数年龄（basis = max(首次 hold 上海日, 最近有效明开
  上海日)；D<H→WIDE，H≤D<2H→MID，D≥2H→CORE）。
- 字段矩阵：WIDE 6（标题/日期/分类/心情标签/事件/我们的话）→ MID 5
  （去我们的话）→ CORE 4（再去标题）。mood_text 与回忆全阶段全检索
  禁用；raw 不属于 6/5/4。

## 4. 召回管线（`recall/pipeline.py` 已接主线）

- **Round 1**：结构过滤前置 → 每桶按当前事实现算阶段 → AllowedFields
  内在分字段索引（`field_search_docs`+`field_fts`，title/event/words
  各自独立投影）执行词法检索；dense 路同 where 前置；RRF 融合 →
  一层 Jev（默认关闭，`DisabledJudge` 显式 unavailable）→ 代码门控
  0-3 卡。首轮真实执行完成自动签发 `ROUND1_COMPLETE` 回执。
- **Round 2**（`memory.recall.round2`）：gate 八条件全部服务端事实
  （同 session/revision、ROUND1_COMPLETE 回执、judge 完成、raw 授权
  = RECALL_RUNTIME+RAW_FALLBACK 开关且 owners、预算、理由属五值闭集）；
  通过后在 Source 层（published=1，human/assistant）深搜，候选标
  `raw_verbatim`，仍经同一层 Jev 出站。
- **find_words**（`memory.find_words`）：跨阶段全量 our_words 专项
  （受 `MARIPOSA_WORDS_RECALL_ENABLED` 开关）；verbatim 不足可按
  §6.5 升级 raw 深搜。已知 `source_ref` 的定点展开是证据读取，不是
  检索 round，不需 Jev 重判。
- 生产开关默认全关：`MARIPOSA_RECALL_ENABLED` /
  `MARIPOSA_WORDS_RECALL_ENABLED` / `MARIPOSA_RAW_FALLBACK_ENABLED` /
  judge——**无例外**（v1.7 新能力已全部纳入开关约束）。

## 5. 「留」keep（v1.7 §5.5）

唯一入口：`recollection.append(keep_wide=True)` 同事务绑定本次新写
的回忆；hold 不能留、非本人回忆不能留、后台模型无权。谁留谁撤
（`memory.keep.revoke` 幂等）；双作者独立 OR；撤销不重置年龄。

## 6. Source 原文层（证据层）

Raw Archive 只读母本（`runtime/source/raw/`，chmod 0444）→ 严格 JSON
流式导入 → 发布门禁（published）→ 专项检索（`source.search` 独立于
普通 Recall）。失败批次默认不可见；同 UUID 内容变化留不可变版本。
`memory_source_bindings` 绑定消息区间（parent 路径校验 + code point
半开区间偏移 + content_hash 防漂移）。

## 7. 未决业务项

- forgotten our_words recall：保持 `disabled / PENDING_OWNER_DECISION`
  （删除链退役后仅影响存量遗留表示，等乔生拍板）。

## 8. 与旧文档的关系

v1.4 的 session 机制、证据分级八类（`authored_event/structured_fact/
word_verbatim/word_paraphrase/word_unverified/raw_verbatim/
relation_reference` + 遗留 `approved_summary`）、安全包装
（`content_role=retrieved_memory` / `instruction_authority=none`）、
幂等/审计继续有效；其遗忘续期、retention 表、摘要通道、"raw 非直接
通道"段已由本文件覆盖。`POLICY_VERSION="mariposa_v1_7"`、
`RECALL_POLICY_VERSION="recall-v1.7"`。
