# Mariposa 记忆库审计报告（v1.7 候选包）

- **日期**：2026-09-28（美西）
- **审计人**：程知行（ZCode 会话）
- **审计基线**：`main @ 2c9417b`「Source Layer（v1.0+v1.1 补修）+ Mariposa v1.7（P0-P3 候选，待审计）」
- **性质**：只读审计 + 隔离临时根行为探测；未触碰正式数据（见 §1.3）
- **交付对象**：江乔生

---

## 0. 结论速览

| # | 结论 | 级别 | 状态 |
|---|---|---|---|
| F1 | 九分类 `reloplay`（剧本）在 HEAD 上**不可写入**：入口校验层只认 8 类 | 高 | 并行会话已在工作区修复（未提交、未回归） |
| F2 | v1.7 召回管线**已建未接线**：主线仍走 v1.4 检索；`memory.recall.round2` 的 gate 恒不可满足，**该能力当前永远被拒** | 高 | 属声明的"剩余工作"，但"不可达"未在任何交付物中明示 |
| F3 | v1.7 两个新能力（`memory.recall.round2`/`memory.find_words`）**绕过"生产默认全关"开关族**；round2 的授权/预算/judge 条件**由调用方自报** | 高（规格偏离） | 未修 |
| F4 | `memory.update` 在 HEAD 上**丢 v2 分层字段**（event_text/original_title/schema_version 置空） | 中（数据完整性） | 并行会话 A01 修复在途 |
| F5 | 分字段投影（v1.7 Round1 索引）**只在建桶时构建**：update/our_words.append/rebuild_index 都不重建，也无注册的全量重建入口 | 中（当前无用户可见影响，切主线前必修） | 未修（在途 A01 也没修） |
| F6 | HEAD 上媒体**字节端点**对 worker 开放（凭已知 hash 可读字节） | 中（安全） | 并行会话 A03 修复在途 |
| F7-F14 | 文档漂移、死表、前端/E2E 与退役语义互斥、声明计数出入、实现质量点 | 低 | 见 §3 |

**总评**：架构方向健康——分层模型、阶段现算、证据分级、幂等与读时重校验的实现质量明显高于一般个人项目，关键不变量大多有测试钉住。当前 HEAD 是一个**诚实的候选包**（提交说明自标"待审计"），但 v1.7 的召回语义（阶段过滤 Round1 / Round2 raw 深搜）**只落地了模块与旁挂能力，尚未成为系统真正的运行逻辑**；实际运行逻辑仍是 v1.4 召回 + v2 存储。另有并行会话正在仓库内落 A01/A03/A08 修复（未提交），详见 §1.2。

---

## 1. 审计基线、方法与现场特殊情况

### 1.1 基线

HEAD `2c9417b`，包含召回运行时 v1.4（0f98098/cb88610）、Source 层 v1.0+v1.1、v1.7 P0-P3 全部工作。测试收集 **498** 条（此前会话声明 497，差 1 属正常增量）；本审计冒烟批次（`test_phase_policy.py + test_v17_pipeline.py + integration/test_api.py`）**82 passed**。全量回归未跑：AGENTS.md 测试硬约束（2026-09-22 内存事故后：前台、最小范围、分批），且审计期间工作区被并行会话改动（§1.2），跑全量会把别人的中间态混进结论。

### 1.2 现场特殊情况：审计期间有并行会话在本仓库改代码

本审计开始时工作区干净（仅一个已知残骸目录）。审计进行中（05:26 PDT 起），**另一个会话**在仓库内落了 6 个未提交修改，内容与 v1.7 剩余清单（A01/A03/A08）吻合：

| 文件 | 在途改动 | 对应本报告 |
|---|---|---|
| `memory/categories.py` | CATEGORIES 8→9（补 reloplay） | 修复 F1 |
| `capabilities/input_schemas.py` | 两处 enum 补 reloplay；`recollections.append` 增 `keep_wide` 参数 | 修复 F1；补 keep 入参缺口 |
| `schema.py` | 新增 migration 19：旧库 `memory_categories` 重建以启用 reloplay CHECK | 修复 F1 的存量库升级路径 |
| `memory/extras.py` | update_text 复制 event_text/original_title/schema_version 到新版本行 | 修复 F4 |
| `media/service.py` | `get_media` 加 owner 校验 | 修复 F6 |
| `plans/service.py` | 终态更新删掉 `anchors["due_date"]` 取值 | 见 F7 |

本报告的发现**以 HEAD 为准**单独成立；已在途修复的逐条标注"在途"。**建议**：该批改动在提交前应跑分批全量回归，且 migration 19 需在 live 根的副本上先演练（涉及 DROP/RENAME 重建表）。

### 1.3 只读纪律与数据面

- 全程未读写正式数据。live 运行根为符号链接 `/Users/zhoujiaming/Data/mariposa → live-20260925/mariposa-runtime`，审计仅确认其存在，未打开任何库文件。
- 行为探测（reloplay 写入、阶段计算）一律在 `mktemp` 隔离根 + `MARIPOSA_ALLOW_CREATE=1` 下进行，结束即删。
- 本报告只描述结构与逻辑，不含任何记忆内容。

---

## 2. 记忆库运行逻辑（正本说明）

以下为**当前 HEAD 实际运行**的逻辑（不是文档宣称的逻辑；两者差异见 §3）。行号均指 `backend/mariposa/`。

### 2.1 总体形态：三本 SQLite + 一个只读归档 + 单一能力入口

```
MARIPOSA_ROOT/                       ← 非 Windows 必须显式设置，否则启动即失败（config.py:26）
└─ runtime/
   ├─ formal/mariposa.sqlite3        ← 正式库：记忆正本、版本、投影、身份、审计
   ├─ workspace/workspace.sqlite3    ← 工作区库：工作项、提案版本、任务租约
   ├─ recall/recall.sqlite3          ← 召回运行库：短期 session 状态（TTL 24h，重启可恢复）
   └─ source/raw/<provider>/<batch>/ ← Source 层只读母本（chmod 0444 + manifest）
```

所有功能走 **Capability Registry**（`capabilities/registry.py`，约 130 个能力）。HTTP（FastAPI，`app.py`）与 MCP（`/mcp`、`/mcp/maintenance`，工具名 = `mariposa_` + 点换下划线）**等权同入口**：解析 Bearer 凭据 → `identity.authenticate` → `registry.invoke`。权限按能力声明在 `allowed_principals`（江乔生 qiaosheng / 周家明 jiaming / worker 后台），路径与传输不赋予角色。三主体规则贯穿：心情只有周家明同期可写、删除审批只有周家明、I 正本只有周家明写、keep/回忆只有两位本人。

### 2.2 写入：一次 `memory.hold` 的完整链路

`memory.hold`（`memory/service.py:190`）是记忆的唯一正式入口：

1. **分类必填**（v1.7 §3.2）：`categories` 为空直接 `CATEGORY_REQUIRED`，没有"未分类"默认值；
2. **同源去重**（§9.3）：带 `raw_refs` 时按 `(conversation_id, message_from, message_to)` 哈希查 `memory_raw_refs`，命中即返回已有 `memory_id`（`deduplicated: true`），不重复建桶；
3. **单事务写入**（BEGIN IMMEDIATE）：
   - `memories` 行：当前指针 `current_version_no=1`、`memory_date`+置信度、`visibility=active`、`compression_state=full`、`held_at`（留存计时的锚）、`occurred_start/end`（事件实际区间，独立于计时）；
   - `memory_versions` 版本 1：`event_text`（v2 分层正文）/`original_title`/`why_remember`/`payload_hash`；
   - **旧投影**：`retrieval_documents` + `search_fts`（白名单正文：只索引事件正文，标题/心情/话语不进）；
   - 分层行：`memory_categories`（平行多选）、`memory_moods`+`memory_mood_tags`（仅周家明、仅 `contemporaneous` 窗口）、`memory_our_words`（按 ordinal）；
   - **v1.7 分字段投影**：`field_search_docs` + `field_fts`，event/title/our_words 各一行（`field_projection.py`，`service.py:172` 是唯一生产调用点）；
   - 审计事件 `memory.created`。

版本模型是 **append-only**：任何修改产生新版本行，`memories.current_version_no` 前移；`representation_state` 只有表示变化（遗忘/恢复——现已退役）才 +1，内容修订不改。删除链已退役（v1.7），现存出口只有 `memory.deletion.*` 审批流（周家明审批）。

### 2.3 明开回温、查看票据、回忆、keep（v1.7 新语义）

这条链是 v1.7 代替"遗忘续期"的机制：

1. `memory.open`（`memory/views.py:25`）：仅两位本人；返回当前表示 + 签发一次性票据（15 分钟 TTL，绑 principal+binding+memory+表示版本）；
2. `memory.view.confirm`（`views.py:54`）：核验票据（身份/桶/版本/TTL）→ **服务端确认时刻**写入 `memories.last_explicit_open_at`，**取 max 防乱序旧确认让计时倒退**；同一票据幂等重放返回首次时间、**不刷新**（`views.py:98-103` 的 CASE 表达式）；同时 `updated_at` 保持不动（打开不算内容变更）。命中/Jev/hydrate/预览/accept 都不算打开；
3. `memory.recollections.append`（`memory/recollections.py:22`）：**必须持有效已确认查看回执**（VIEW-01/02/03）；默认 append-only，修订走 supersedes 链留底；回忆**不进任何检索索引**；
4. **keep「留」**（v1.7 §5.5）：唯一入口是 `recollection.append(keep_wide=True)`——**同一事务内**写 `memory_keeps`，标记绑定本次新写的 `recollection_id+version`（理由即回忆本身，无独立 reason 字段）。两位作者独立标记、OR 生效；`memory.keep.revoke` 谁留谁撤、幂等；**撤销不重置年龄**，全部撤销后按真实 basis 重算（`memory/keep.py`）。

### 2.4 阶段策略（v1.7 核心，`recall/phase_policy.py`）

**阶段永不持久化**——每次查询按当前事实现算（§5.1），库里没有任何 WIDE/MID/CORE 列：

```
九分类（schema CHECK 九值）:
  daily=20天  sex=20天  sad=30天  sweet=30天  date=60天  reloplay=7天(工程初值)
  milestone/anniversary = 永久    plan = 计划资源自管

计算顺序（§5.2，显式分支优先）:
  1. 任一作者有效 keep           → WIDE (EXPLICIT_KEEP)
  2. 含 milestone/anniversary    → WIDE (PERMANENT_CATEGORY)
  3. plan 资源: 未终态 → WIDE；done/cancelled → CORE（状态变化即 CORE，无等待期）
  4. 普通桶: H_eff = max(有限类别H) × k%（部署 k 默认 100）
     basis = max(首次 hold 上海日, 最近有效明开上海日)   ← 明开回温就在这里生效
     D = 今天(上海) − basis（整数自然日）
     D < H → WIDE；H ≤ D < 2H → MID；D ≥ 2H → CORE
```

- 多分类取**最大** H，不累加；坏参数一律 `PolicyError`/`DataGap`（如 `HELD_DATE_GAP`、`FUTURE_HELD_AT`、`PLAN_MAPPING_GAP`），**绝不**用"今天/合成 30 天"兜底；
- **字段矩阵**（§5.3）：WIDE 6 字段（event_date/categories/mood_tags/event_text/original_title/our_words）→ MID 5（去 our_words）→ CORE 4（再去 original_title）。这是"记忆随时间淡出"的实际形态：不是删除，是**可检索字段逐层收窄**；
- 展示值 A=2^(-D/H) 仅用于前端展示（`display_availability`），不参与任何过滤/排序判断。

### 2.5 召回会话运行时（Recall Session，`recall/`）

**正本只在 Mariposa runtime 库**（estómago/CC 只带 session ref 换窗重取）。生命周期：

- **七动作**：`start / refine / reject / accept / navigate / status / close`，每动作有状态前置表（`state_machine.py:16`；终态 EXPIRED/RESOLVED/CANCELLED 不可再动，STALE 只允许 status/close）；
- **九状态**：ACTIVE / AMBIGUOUS / CONFLICT / DEGRADED / BUDGET_EXHAUSTED / STALE_RETRY_REQUIRED / RESOLVED / CANCELLED / EXPIRED。检索结果状态（FOUND/AMBIGUOUS/…/UNAVAILABLE 八种）与 session 状态分层，并存的降级因素进 `degraded_reasons` 而不是都升为 DEGRADED；
- **预算**（`budget.py`）：一个 burst = 原始+≤2 替代表达、初次+≤2 轮补查（3 轮）；每 session ≤3 burst、累计 9 轮。**新 burst 必须带 `continue_request_ref`**（真实用户继续请求的引用）——自动自循环/换窗/反复 start 拿不到无界额度（§9.3）；
- **并发控制**：所有 session 写操作走 CAS（`WHERE current_revision=?`），冲突报 `REVISION_CONFLICT`；操作幂等走 runtime 库 `recall_operation_keys`（与 formal 库幂等表隔离——召回结果不长期缓存进正式库）；
- **纠正不改正式记忆**：reject 只改本 session 候选状态（reject_target 分 candidate/event/word/source_selection 四档）；refine 只推进 revision。

### 2.6 一轮检索的执行顺序（现行主线 `_run_round`，`recall/service.py:190`）

这是**实际在跑**的检索路径：

1. **查询分层校验**（`models.validate_query_plan`）：`explicit_constraints`（白名单字段：categories/mood_tags/event_date 才可硬过滤，白名单外字段**报错不静默**）/ `explicit_negative_constraints`（硬排除）/ `inferred_hints`（只做软提示，永不硬过滤，QUERY-01）/ `alternate_queries`（不新增事实）/ `unknowns`（不自动填成事实）；
2. **作用域前置**（`query_plan.AllowedScope`，v1.4 §5.2）：结构化过滤先于打分与 Top-K——不允许"向量先全库 Top-5 再过滤日期"；
3. **词法路**（`fusion.scoped_lexical_search`）：候选池 ≤2000，**BM25 的 IDF 词频统计只来自池内**（HYBRID-06：加入无权语料不改变本 scope 的可见排名，不只是结果事后过滤）；FTS 表达式由类型化计划编译，用户输入经字符级分词+引号消毒，FT5 语法字符全部失活；空 query = 浏览该池（不构造随机语义向量）；
4. **向量路**：`SEMANTIC_PROVIDER` 只有配了 `local_bge_zh` 才真跑（同一 where 前置）；未配置则显式 `coverage=dense_event: unavailable` + `degraded: semantic_unavailable`，**不伪造语义分**（HYBRID-07）；
5. **融合**：族内去重生成唯一排名 → RRF（`rrf = Σ w/(k+rank)`，k=60）；跨通道（event/words/raw）**不比较未校准原始分数**，各自保持独立证据角色；
6. **Jev 精排**（默认 disabled）：provider 缺省 `DisabledJudge`，`coverage.judge=not_configured`；
7. **代码门控选择**（`selection.py`）：硬门（用户 rejected/同资源去重）→ judge 分优先、无 judge 用 RRF 序（不伪造支持分）→ **0-3 张候选卡**；未校准前唯一候选也标 `needs_validation`（禁自动高置信单条）；证据等级不满足**不是淘汰条件**——paraphrase 候选仍作为线索交付并标 `evidence_requirement_met=false`（触发后续 raw 补查判定，RAWX-01）；
8. **落 runtime 库**：候选引用（不存正文）+ 交付回执（receipts 记 content/representation/permission 版本）；
9. **读时重校验**（`revalidate_receipts`，§9.4）：正文出站前把回执与当前正式库比对（visibility/版本/表示），失效引用**不重放旧正文**，受影响 session 转 `STALE_RETRY_REQUIRED`。`memory.context.validate` 提供换窗装配前的同款校验。

### 2.7 words 通道与证据分级

- **words 是独立通道**（`retrieval/words.py`）：只读派生索引 `words_search_docs`+`words_fts`，与事件投影结构性隔离——仅出现在 our_words 的词不会让 event 命中（WORD-01）。索引带**正表指纹**：读取时指纹不一致即自动重建（含遗忘行清除）；说话人按正式 speaker 字段过滤，不猜（WORD-03）；
- **证据分级八类**（`evidence.py:13`）：`authored_event`（正式事件正文，**不冒充原话**，EVID-01）/ `approved_summary`（遗留遗忘摘要表示）/ `structured_fact` / `word_verbatim` / `word_paraphrase`（不得作为逐字原话）/ `word_unverified`（来源失效的 verbatim 降级，EVID-03）/ `raw_verbatim` / `relation_reference`。`verbatim_required` 只被 word_verbatim/raw_verbatim 满足；
- **全链路安全包装**：每条证据、每个交付包带 `content_role=retrieved_memory` + `instruction_authority=none`（SAFE-01/02）——记忆正文里的"忽略规则/调用工具"只是历史数据；
- 遗忘后的 words 显式检索 = `disabled / PENDING_OWNER_DECISION`（唯一未决业务项，等你拍板）。v1.7 删了遗忘链后，此段只对存量遗留表示生效，且召回侧遇遗留 `forgotten_summary` 一律标 `LEGACY_CONTENT_GAP`，不再从已退役的摘要表取数、不冒充事件正文（`service.py:159-165`）。

### 2.8 raw 补查与 Source 原文层

两套原文体系并存（v1.7 明确旧层不在新管线深搜范围）：

- **v1.4 raw 补查**（`raw/recall.py`）：仅当 words 证据不足 + `MARIPOSA_RAW_FALLBACK_ENABLED` 显式开启 + scope 解析通过（owners only）才跑；**全历史分批游标扫描**（每批 500、≤20 批），返回覆盖状态与 continuation——有预算没查完不得报告"从未说过"（RAWX-02）；speaker 无正式参与者映射前返回 null，不把 user 猜成乔生（RAWX-06）；
- **Source 层（原文证据层，2026-09-27）**：`source.import` → 输入字节先暂存并算 sha256 → `os.replace` 原子发布为只读母本（chmod 0444）→ **解析只读归档字节**（一次固定快照）→ 严格 JSON 文法（utf-8 strict、拒 NaN/尾逗号/闭合后杂字节，单元素 ≤64MB）→ 每会话一事务落库（批次/会话/快照/消息/不可变消息版本表；同 UUID 新内容留版本不覆盖）→ **发布门禁**：解析失败或七项完整性校验不过 → 批次 failed、消息保持 `published=0` → 查询层默认只读 `published=1`，**失败批次默认不可检索**，且 capability 层无绕过参数。`memory_source_bindings` 把记忆绑到消息区间（校验同会话+parent 链可达+code point 半开区间偏移，固定端点 content_hash 防漂移）；`source.memory.open` 动态打开绑定区间，原文不复制进记忆。

### 2.9 v1.7 召回管线：已建成，但**不是当前运行逻辑**（`recall/pipeline.py`）

模块按 v1.7 §6 完整实现，但接线状态如下：

- `round1_candidates`（阶段过滤检索）：**无生产调用方**（只有测试调用）。主线 `_run_round` 仍走 §2.6 的 v1.4 路径——即**阶段/AllowedFields/分字段索引目前不参与任何实际召回**；
- `round2_gate` + `raw_deep_search`（接 Source 层，`raw_verbatim` 唯一管线入口）：gate 八条件中"round1_complete_receipt"需要有人调 `mark_round1_complete` 写回执——该函数**同样无生产调用方** → 回执永不写入 → **`memory.recall.round2` 在生产上恒返回 ROUND2_GATE_DENIED**。这是 F2；
- `find_words_candidates`：唯一接线的部分，经 `memory.find_words` 能力直达（跨阶段全量 our_words，不受 WIDE/MID/CORE 限制）——但绕过了 words 通道开关（F3）。

**一句话**：v1.7 语义（阶段淡出、Round2 原文深搜）目前是"旁挂可测模块"，系统实际召回语义仍 = v1.4。切换主线是已声明的剩余工作，且切之前必须先修 F5（索引陈旧）。

### 2.10 幂等、审计与恢复

- **formal 幂等**（`registry.py:448`）：调用方显式给 key 即生效；原子 INSERT 占位（running）→ 执行 → completed；并发同 key 的一方有界等待后重放终态；**崩溃窗口**（running 残留 >60s）不盲重放，报 `OUTCOME_UNKNOWN`，由 `maintenance.idempotency.reconcile` 对账后放行；业务拒绝（参数/权限类）落 failed 允许修正重试，不伪装成崩溃；
- 召回运行时能力走 runtime 库独立幂等（`recall_operation_keys`），重放前重检权限与版本；
- 全部写操作落 `audit_events`；workspace 库另有任务租约（30 分钟）给 worker 后台任务。

### 2.11 部署与开关

- HTTP 绑 `127.0.0.1:18780`（可配）；`/health`、`/api/capabilities`、`/api/capability/{name}`、`/mcp`、`/mcp/maintenance`、媒体字节端点、`/api/source/upload`（流式限 2GB）；
- **生产开关默认全关**（§15.1）：`MARIPOSA_RECALL_ENABLED` / `MARIPOSA_WORDS_RECALL_ENABLED` / `MARIPOSA_RAW_FALLBACK_ENABLED` / judge（disabled）；`MARIPOSA_ROOT` 非 Windows 必须显式；首次建库需 `MARIPOSA_ALLOW_CREATE=1`——错误路径应启动失败，不静默在别处建空正式库（OPS-RECALL-01，有 conftest fail-closed 保险丝配套：测试根白名单外一律拒绝）；
- ⚠ 但 v1.7 新能力绕过开关族，见 F3。

---

## 3. 审计发现明细

### 高

**F1｜reloplay 九分类在 HEAD 不可写入**
证据：`git show HEAD:backend/mariposa/memory/categories.py` 的 CATEGORIES 只有 8 类；`input_schemas.py` HEAD 版本两处 enum 同样 8 类（hold 与 categories.replace）。schema CHECK 与 phase_policy 是 9 类——即"策略层九分类、入口层八分类"，带 `reloplay` 的 hold 在输入校验即被拒。
影响：剧本类记忆完全无法建桶；若绕过入口直写，阶段层又能算（H=7），语义分裂。
在途修复：categories.py + input_schemas.py + migration 19（旧库重建表升级）已改，**未提交未回归**。
审计建议：回归应含"reloplay hold 全链路 + 旧库跑 migration 19 后 reloplay 可写"两个用例（现行测试只覆盖 phase_policy 纯函数，没有走 hold 的 reloplay 用例——这正是它漏到 HEAD 的原因）。

**F2｜v1.7 召回管线未接线；round2 能力恒不可达**
证据：`grep` 全库，`round1_candidates`/`mark_round1_complete` 仅 `tests/unit/test_v17_pipeline.py` 调用；`_run_round` 主线仍 join `retrieval_documents`（v1.4 投影）。
影响：`memory.recall.round2` 永远 `ROUND2_GATE_DENIED`（gate 缺 round1:complete 回执）；阶段过滤/AllowedFields/分字段索引不参与实际召回。
定性：v17 会话登记过"剩余：主线切换 pipeline（旧测试需 REPLACE）"，所以**半属已知**；但"round2 能力当前不可达"未在 REMOVAL_MANIFEST/NEXT 里明示，按验收纪律应显式登记。

**F3｜v1.7 新能力绕过开关与授权模型**
证据：`registry.py:744-761` `_recall_round2` 的 `judge_status`/`raw_search_authorized`/`budget_available` 取自**调用方参数**（自报），不查 `MARIPOSA_RAW_FALLBACK_ENABLED`、不做 `raw_recall.resolve_scope`；`_find_words`（registry.py:732）不查 `MARIPOSA_WORDS_RECALL_ENABLED`；两者也不在 `_RECALL_RUNTIME_CAPS` 幂等集。
影响：CURRENT.md §4"生产默认全关、隔离验收后分项启用"的承诺对这两个能力不成立；"Round 2 全部服务端条件核验"的模块自述与能力层实现不符。
缓解：均 `_owners()` 限定，两位本人是信任边界内主体；但这正是"验收前不该开的口子"——管线语义以服务端事实为准是整套设计的立身之本。

### 中

**F4｜memory.update 丢 v2 分层字段**（HEAD）
`memory/extras.py` update_text 的 INSERT 不含 original_title/event_text/schema_version → 新版本行三字段 NULL → 读取时 event_text 回退 hold_text、**original_title 永久丢失**。在途 A01 修复覆盖（工作区已见复制逻辑）。

**F5｜分字段投影无更新/重建路径**
`build_for_memory` 唯一生产调用在 hold（`service.py:172`）；`memory.update`、`memory.our_words.append`、`maintenance.rebuild_index`（rebuild.py 只重建 retrieval_documents/search_fts，且仍带遗留 forgotten_summary 分支）都不碰 `field_search_docs/field_fts`；`field_projection.rebuild_all` 无任何注册入口；words 索引有读时指纹自愈，字段投影没有。
影响：当前无用户可见后果（F2 使其不被读取）；**切换主线前必修**，否则 MID/CORE 阶段检索会基于建桶时的陈旧索引。在途 A01 修复未覆盖此点。

**F6｜媒体字节端点授权**（HEAD）
`app.py:141` media_object 只 authenticate；HEAD 的 `media.get_media` 无 owner 校验 → worker 凭已知 hash 可读任意媒体字节。在途 A03 修复覆盖（service 层加 owners 校验）。

**F7｜plans 终态更新疑点**
在途修复删除了 `plans/service.py` 终态更新里的 `anchors["due_date"]` 取值——反推 HEAD 上该行存在 KeyError 或语义错误风险。本审计未独立复核 plans 锚点逻辑，**建议向并行会话求证该改动的动机与测试**。

### 低

- **F8 文档漂移**：`docs/memory_runtime/CURRENT.md` 自称"唯一现行召回说明"，但仍是 v1.4 语义（遗忘=批准摘要、raw 不是直接通道段等），v1.7 删除链/阶段/keep 均未回写；registry 两处描述仍写"八分类"；`categories.py` 模块 docstring 仍写"八分类"且保留无调用方的 `period_days`/`classify`（旧留存死代码，`PERIOD_DAYS` 无 reloplay 项，一旦被复用会 KeyError）；`RECALL_POLICY_VERSION="recall-v1.4-eval-1"` 与 `POLICY_VERSION="mariposa_v1_7"` 并存；
- **F9 死表**：`proposal_envelopes`、`workspace_audit` 全库零引用；`proposal_resolutions` 只读不写（永远空表）；
- **F10 前端/E2E 与退役语义互斥**：`apps/web/e2e/forget-loop.spec.ts` 仍正向跑完整遗忘闭环并调 `memory.restore`（后端已锁 404）→ 该 E2E 必挂；`start-isolated.mjs` 硬编码 `.venv/Scripts/python.exe`（Windows 路径）→ **Mac 上 E2E 无法自启**；`pages.tsx` 仍引用 memory.restore/forgetting。前端八模块+keep UI 本是 v1.7 剩余项，此处登记证据；
- **F11 声明计数出入**：REMOVAL_MANIFEST `verified.negative` 自称锁定 15 个退役能力，`removed_capabilities` 列 20 个，`test_retired_capabilities_uncallable` 实际只锁 **9** 个（workspace.proposals.revise/list/withdraw/get、memory.forgetting.request、workspace.review.get/submit/revise 未单独锁）；ACCEPTANCE.json 的 summary 块（v13 15 pass/3 preliminary）与逐条实况（17 PASS/1 preliminary）矛盾——summary 陈旧；
- **F12 实现质量**：`selection._hard_gate` 第二分支与 `ref in rejected` 完全重复（死逻辑）；`recall/service._CURRENT` 模块级全局 principal——FastAPI sync handler 跑线程池，并发请求下 raw scope 解析存在串主体窗口（建议改为参数传递）；`round1_candidates` 每桶一次 phase_of（各开一条 formal 连接）+一次 FTS 查询，≤2000 桶的 N+1，切主线前需批量化；`sticker_search` 全表扫描（量小可接受）；
- **F13 Source 层健壮性**（子代理只读结论，未逐一复跑，置信度中高）：导入租约 120 分钟被接管后，原进程 `_fail_batch` 无条件 UPDATE 可把接管方已 completed 的批次改回 failed（消息已 published=1，破坏"published 必属 completed"门禁状态）；`_refresh_conversation_aggregates` 未按 published 口径重算，失败批次未发布消息会计入会话条数聚合；`_verify_integrity` 校验范围是全 provider 而非本批（历史遗留问题可阻断新文件 completed）；`binding.reindex_search_docs`/`archive.cleanup_staging` 无调用方（崩溃残留 staging 永不清理）；`_find_message_any` 无 provider 维度（多 provider 后 UUID 碰撞会取错行）；
- **F14 仓库残骸**：根目录 `D:\mariposa\runtime\models/`（字面 Windows 路径名目录，91MB bge 缓存）未跟踪；建议择机移到数据根并补 .gitignore。

---

## 4. 声明核对（验收纪律）

| 声明 | 实测 | 结论 |
|---|---|---|
| v1.7 收口"426/485/497 测试绿" | 本次收集 498；冒烟 82 passed | 数量吻合（+1 增量）；全量未复核（约束见 §1.1） |
| 交付物：REMOVAL_MANIFEST_v17.json / LEGACY_TEST_MAP.json / v17_p0_baseline.json | 三者均在位 | ✅ |
| 删除链"35 测试块+4 文件" | LEGACY_TEST_MAP 记录 retired 39 块/13 文件 + retired_files 4 + replaced 2；抽查 grep 确认已删净，tests 下无正向旧语义残留（forget/linshijian 全为负向守卫） | ✅（数量口径 35↔39 属登记时点差异） |
| "退役能力获 UNKNOWN_CAPABILITY（404）" | test_api 锁 9 个能力 404；`linshijian` tests 下零命中 | ✅ 但只锁 9/20（F11） |
| CURRENT.md"唯一现行召回说明" | 内容仍是 v1.4 语义，未随 v1.7 改写 | ❌ 见 F8 |
| "生产开关默认全关" | 对 v1.4 能力成立；`memory.find_words`/`memory.recall.round2` 不受开关约束 | ❌ 见 F3 |
| Source 层 v1.1"11 缺口全修" | 抽查发布门禁/不可变版本表/区间校验实现均在位；子代理另发现 F13 健壮性点 | 大体 ✅，F13 待议 |

**勘误与保留**：本报告未复跑全量 498 与 v2 验收矩阵 109 条；Source 层 F13 各点来自只读代码走查，未做动态复现。这两个边界如需闭合，另安排分批执行。

---

## 5. 建议下一步（按优先级）

1. **先收拢在途改动**：并行会话的 6 文件修复方向都对，但需（a）分批全量回归（498）；（b）migration 19 在 live 根副本演练（DROP/RENAME 重建）；（c）补"reloplay hold 全链路"用例后再 commit；
2. **接线前先修索引一致性（F5）**：update/our_words.append 事务内重建字段投影 + rebuild_index 纳入 field/words 两套派生索引（或给字段投影加 words 式指纹自愈），然后才做 `_run_round` 主线切换；
3. **round2 gate 收归服务端（F3）**：judge_status 从 session 状态读、授权查 config+scope、预算查 budget snapshot，或按 v1.7 正本明确"两位 owner 信任内自报"并写进 CURRENT.md；
4. **CURRENT.md 回写 v1.7**：阶段/keep/明开/九分类/删除链语义并入，撤掉已退役的遗忘段（或明标 legacy），同步 registry 描述与 POLICY_VERSION；
5. **前端与 E2E 跟进**：Mac 化 start-isolated、forget-loop 改造或下线、pages.tsx 清理退役引用；
6. 她需要拍板的仍是那一项：**forgotten our_words recall**（保持 disabled 还是允许）——v1.7 删除链后此未决项只影响存量遗留表示，范围比之前小了。

---

*报告止于结构与逻辑，不含任何个人记忆内容。测试与探测均在隔离根完成，正式数据未动。*
