# PROGRESS

> 会话中断后从本文件与 NEXT.md 续接；每阶段末更新。

## 2026-09-26 · 召回运行时 v1.3/v1.4 全量落地（P0–P6）

依据 Mariposa_记忆运行语义正本_v1.3 + 分层混合召回实现路径_v1.4
（林石见 2026-09-26）。现行唯一召回说明：docs/memory_runtime/CURRENT.md；
路径映射：PATH_MAP.md；基线核对：BASELINE.md；64 条验收证据：
ACCEPTANCE.json（v13 18 条 + v14 46 条全部真实执行，其中 5 条
PASS_preliminary=Mariposa 侧契约已验、外部依赖分层留待）。

- **新增召回运行时**：Recall Session 七动作（start/refine/reject/
  accept/navigate/status/close）+ 九状态机 + burst 预算（3×3，显式
  continue_request_ref 才可申请新 burst）+ runtime 库持久化
  （runtime/recall/recall.sqlite3，重启恢复、TTL 惰性清理、CAS、
  operation_id 幂等隔离于 formal 幂等框架）。
- **查询分层**：explicit/negative constraints（通道白名单，未知字段
  拒绝；冲突返回 CONFLICT）、inferred_hints 仅软提示、alternate/
  unknowns 保留不造事实；terms/phrase 双模式安全编译（FTS 注入消毒）。
- **混合检索**：作用域内词频统计词法检索（HYBRID-06 隔离：scope 外
  语料不影响本 scope 排名/计数，不只结果过滤）+ scoped 向量接口
  （provider 未配置诚实 unavailable）+ RRF 融合（1-based、族内去重
  不叠票）+ 0—3 条交付（多样性/冲突披露/needs_validation/截断标记）。
- **words 通道**：独立派生索引（fingerprint 读取时校验+重建）、
  speaker/日期/原话等级过滤、verbatim/paraphrase/unspecified→
  word_verbatim/word_paraphrase/word_unverified 分级；遗忘后 disabled/
  PENDING_OWNER_DECISION 全链路无旁路（WORD-01..05/EVID-02/03/RAWX-05）。
- **raw 专项补查**：scope resolver + 分批游标全历史扫描（超最近 500
  条可查、partial/continuation 不冒充"从未说过"）+ 有限片段 +
  speaker 不猜测（RAWX-01..07）；raw.search 命中补数据角色标注。
- **证据与安全**：八类 evidence_kind 全链路 content_role=
  retrieved_memory/instruction_authority=none（SAFE-01/02）；读时
  回执重校验（正式修订/遗忘→STALE_RETRY_REQUIRED，不重放旧正文）。
- **JudgeProvider**：协议+DisabledJudge+typesafe_jev 适配器（默认
  disabled；无授权策略/无 key 构造即拒绝；Noul 无 confidence）。
- **运维**：MARIPOSA_ROOT 非 Windows fail-fast + MARIPOSA_ALLOW_CREATE
  建库闸（OPS-RECALL-01/02）；库身份 meta 章；contracts 重导出
  （212 项）+ recall_runtime.v1.schema.json；verify_recall_runtime.py
  自检 9/9；eval_recall.py A/B 评测入口（C 组未授权保留）。
- **测试**：新增 122 条（单元 51/集成 7/验收 63）全绿；存量 313 条
  回归全绿（435 collected）。开关生产默认全关，分项启用。

## 2026-09-22 · v2.0.1 新语义实施·第一批（P0–P6 后端核心）

依据 docs/spec_v2/（v2.0.1 交付包已归档）。两段提交：

**第一段（P0/P1 收尾，582f681）**：B06 测试根保险丝（conftest fail-closed
白名单+realpath 规范化，业务根拒绝，V2-OPS-07）；B08 扫描游标防饥饿
（(memory_date,memory_id) 稳定游标、NULL 日期段 NULL-safe 推进、defer
实际落状态、workspace.proposals.withdraw 独立工具，V2-RET-09）；B02 加固
（崩溃残留 running 不盲删重放→OUTCOME_UNKNOWN；maintenance.idempotency.
reconcile 显式对账后 failed→可重试，V2-OPS-05）。

**第二段（P2–P6 后端核心）**：
- 迁移 11/3：八分类/当时心情+标签/我们的话/查看回执/回忆/留存行/摘要
  版本/审查委托/I 正本+建议/计划终结字段/持久到期队列/纪念日/审查工作项。
- memory.hold 分层写入：标题/八分类/事件/同期心情（仅周家明+仅同期，
  V2-REC-03/04/09）/我们的话/creation_mode；v2 投影白名单（标题/心情/
  话语/回忆永不进索引）。
- retention：自然日语义（basis_date+N、跨月/闰日、日界候选资格），
  分类周期 20/30/60/永久，明确打开续期（同日不后移、票据幂等），
  确定留终局不再送审；到期队列（租约/重试/dead-letter）。
- memory.open/view.confirm/recollections.append/revise/list；our_words；
  categories.replace；I（i.get/write/versions/suggest）。
- 计划：完成/放弃→终结自然日+20 固定到期；阅读永不续期/不改
  terminal_revision；明确重启取消旧周期（PLAN-01..11）。
- memory.recall：分类 any/all+心情标签+事件日期筛选、空 query 浏览、
  BM25（升序=更相关）、去重分页、matched_by/matched_fields。
- 林石见审查闭环：v2_review_items 状态机、claim/get/revise/submit
  （release/escalate_*）、memory.retention.decide（keep 终局/continue/
  defer）；禁用概括词拦截；保留线索（共同话语→needs_jiaming_decision、
  第一次/留附上下文、回忆非空）；执行前再核验（源版本/字段hash/到期/
  实时线索）；摘要+审查后 tags 入索引（标题永不）。
- bootstrap v2：三天桶=标题+心情标签+文字（含补录"新收录"标记）+I+
  0..3 日临近日程全文+纪念日；不再默认 30 条原文（V2-BOOT-03）；
  snapshot 指纹覆盖记忆/计划/I/纪念日。
- E2E 隔离化：playwright webServer 自启隔离实例（端口 18799、
  .pytest_tmp/e2e-isolated、独立 token），不再复用业务 18780（§16）；
  顺带修复 HEAD 上已损坏的严格 schema 调用点（scan 缺 policy_version、
  前端 submit 缺 proposal_hash/字段名不对）与静态资源按 MARIPOSA_ROOT
  误定位问题。

**验证：后端 289 passed（原 214+新增 75）/ pip check 通过 / web build 通过 /
E2E 2 passed（隔离实例）**。旧用例仅按 superseded 规则更新 4 处
（V2-BOOT-01/03/08 注明于测试内）。

**尚未实施（见 NEXT/BLOCKERS）**：原文区间绑定 v2（raw_binding_ranges/
context 确认展开/自动绑定评测关卡）、语义检索 v2 投影切换、迁移映射
（旧库→v2 字段）、前端 v2 页面（分栏桶详情/召回筛选/审查/I）、
109 条矩阵的机器可读证据链、v2 契约 JSON 导出。

## 2026-09-22 · 林石见独立复审问题修复（8/8 项）

复审发现（详见对话/commit）：MCP 三段名反解（52 能能全坏）→ 双向表实时反解；
幂等并发双副作用 → 原子 claim（running 占位+有界等待+BUSY）；150 项契约缺口
（60 缺/25 异名）→ 兼容层（别名 6/blocked 31/reserved 8/薄实现 18+4），规格
全覆盖且 blocked 如实返回；12 严格 schema 未接入 → 最小校验器（$ref/anyOf）+
tools/list 真实 schema + 调用点全对齐（hold 显式 raw_pending/date_confidence、
submit 带 proposal_hash、scan 带 policy_version——均为规格必填）；revoke
NameError → 修复+失败分支测试；证据映射短名 → 源头 M 表修复+持久校验测试
（TestEvidenceMapIntegrity 防退化）；pytest basetemp 默认项目内（默认命令可
复现）；一次性脚本清除。
**198 测试 + E2E 2 全绿**；服务级实测：三段名 MCP 调用通过、tools/list 168 项
带真实 schema。

## 2026-09-22 · 执行包 v1.1 完整对照（第二次交付：118 条逐条映射）

工程包原件到位（docs/execution_pack_v1.1/，含 02 验收清单 118 条与 05 连续执行模式）。
本轮：新机制 8 项（撤回竞争/再提起表/意义审查排除/覆盖诚实/低置信绑定审阅/
迁移幂等+ID映射+dont_surface/tags_only 语义/bootstrap unchanged 薄响应/待定日期区/
绑定撤销）；两批评收测试 44 项（每条标注用例 ID）；架构边界测试 4 项（可执行解耦
检查）；05 交付物 BLOCKERS/DECISIONS/verification。04 U01-U18 逐条对照无行为差异。
**118 条：106 PASS / 12 BLOCKED / unmapped=0**（docs/acceptance_mapping_v1.1.md）。
后端 **186** + E2E **2**（含 T-OPS-04 刷新+移动端）全绿。语义检索工程化：
warmup+限流补算+相对窗+hybrid top-5。scan 门槛语义修正。

## 2026-09-22 · 复审关卡（林石见 6 项）——全部交付

详见 docs/REVIEW_GATE.md。要点：审计 HEAD 唯一化（6072d76）；验收汇总
72 PASS/3 BLOCKED/2 NOT_IMPL/unmapped=0（02 原件缺，按 01 文档构建并声明）；
真实语义 provider（本地 ONNX bge-small-zh-v1.5）+ 复核方三用例全过；
letter archive 对齐旧 bucket_mgr.archive（schema v9）；bootstrap 三段真分页；
真实 Ombre 副本 dry-run（484/484 映射零偏差，生产零写入）。
**测试 142 + E2E 2 全绿。**

## 2026-09-21 · 第 5 轮（§17.3 必需能力补全 + 勘误）

**勘误**：第 4 轮报告"工程文档可做项已清零"为**过度宣称**。逐节重查 §17.3
最低能力覆盖表后发现一批遗漏（本栏如实记录并全部补齐）：
memory.list/by_date/by_tag、meanings.append/replace（§10.5 必需而非可选）、
quotes.get/by_memory、diary.read/revise、workspace.tasks.claim 系列与 task_leases、
hold 带 raw_refs 的同源去重（§9.3 前半）、**重建索引命令与 §8.5 最后一条
标志性测试**、calendar.providers/presence.status/jobs.status、必须事件
（emotion.changed/reminder.due/proposal.resolved）、sticker、两阶段导入、
raw.read、Web 设置页与媒体库页。

- schema v8：memory_meanings（层号<1000 有效，>=1000 归档留底）、
  workspace v2 task_leases、stickers、import_jobs
- meaning：只周家明写；追加纳入 full 投影（可搜）；替换旧层归档留底且不再可搜；
  遗忘桶 meaning 不进默认检索（§10.5 实测）
- 重建索引：按当前版本全量重建；**重建后遗忘桶旧词不可搜**（标志性测试钉住）
- 任务租约：claim 冲突 LEASE_HELD/仅认领人 release/过期重领；inspect 只给授权材料
- hold(raw_refs)：同消息范围已绑定 -> 返回已有记录不新建（deduplicated=true）
- Web：新增媒体库（两步上传+预览）与设置页（只读配置快照）；E2E 2 项保持绿
- **后端 131 测试 + E2E 2 项全绿**；能力 110（契约同源导出）

## 2026-09-21 · 第 4 轮（第一版范围收尾：能力 55→86）

- memory：update（新版本+重建投影）/pin/protect/anchor/versions.list（flag 排除自动候选实测）
- relations：link/detach（留历史）/list（显式反向）/trace（continuation_of 链）；
  检索新增 matched_by=relation 途径
- 原文后绑定：raw_pending 先写 Hold、provisional_sources 片段（仅周家明）、
  bind 不改 Hold 内容、同范围他桶 DEDUPE_NEEDS_REVIEW、revoke 留历史
- reminders：CRUD + 日历 provider + 幂等到期结算（无常驻 scheduler/零外部副作用）
- maintenance：outbox drain/status（至少一次+幂等标记）、activity.list、
  reconcile_workspace（崩溃后按终局决议对账，实测修复+幂等）
- media：两步上传（token/字节专用端点，不进工具参数）、hash 去重落盘、
  MIME/大小白名单、鉴权下载、路径穿越惰性（404）
- moments：post/list/comment/react（post 与 group_archive 分 kind）
- 遗忘批量决议 decide_batch（逐项冻结，单项失败不影响其余）+ 拒绝冷却（30 天，
  scan 跳过实测）
- emotion.context.get / listening.status：reserved 契约落库（默认禁用，不伪造）
- 迁移 apply 演练：fixture 报告落新库 + 置顶/锁参数保留核对 + 大报告拒绝
- 安全测试：XSS 按数据返回、media 端点鉴权/穿越；并发测试：4 路并发 decide 恰一生效
- schema v5-v7（9 新表 + source_state 列）；**后端 117 测试全绿**；新能力全部真实冒烟
- 未动边界不变

## 2026-09-21 · 第 3 轮（分页/语义校对/provider 核验/React Web/E2E）

- bootstrap.next 分页 cursor（不静默截断；资源变化 SNAPSHOT_STALE 不一半新一半旧）+4 测试
- quotes 语义校对受控管线：双步判定、无 provider 挂起不写、撤下不复活、狭窄修正留底 +7 测试
- provider 只读契约核验（docs/provider_contracts.md）：Superposition 8321 实测 v1.3.0；
  Siren 8790 实测 v1.1.1（其语音 provider 为 dev 回退，如实记录）；扎西德勒 8787 未运行 blocked
- React/Vite 版 apps/web（TS 严格模式）：七页签真实 API；后端 /app 同源 serve；
  修复 note 引用导致的无限请求循环（真 bug，E2E 发现）
- Playwright E2E 2 项通过：浏览器级完整遗忘闭环（写桶→扫描→提案→审批→摘要切换→恢复→旧词重现）+ 日历
- **后端 90 测试全绿 + E2E 2 项**；期间发现并修复 React 前端无限请求循环与分页竞态

## 2026-09-21 · 第 2 轮（Home/Self/Diary/情绪标签/SNAPSHOT_STALE）

- schema v4：home/self/diary 全版本链 + memory_tags(whose CHECK) + bootstrap_snapshots
- 能力 39→52（contracts 同源重新导出）；日历新增 diary provider
- Self 隔日规则、情绪主语必填、遗忘桶按情绪仍可查、snapshot 状态指纹失效——均含测试与真实服务验证
- **测试 79 passed**；服务重启后全能力真实冒烟（含 cc profile 入口校验、SNAPSHOT_STALE 实测）
- 未动边界不变（旧生产零接触）

## 2026-09-21 · 连续执行第 1 轮（Phase 0–5a + 工具层 + MCP 协议层）

**Commits**（git log 顺序，均为可追踪增量）：
1. `de6b90b` Phase 1-2 遗忘纵向闭环 + 23 测试
2. `ea82147` doctor/start-dev/stop-dev + 真实启动验证
3. `9408a63` Phase 0 只读基线盘点（4 份 docs）
4. `bc5ac40` Phase 3-4 raw/quotes/handoff/plans/calendar/bootstrap/time + 16 测试
5. letters/deletion 兼容层 + 16 测试（55 绿）
6. MCP JSON-RPC 适配层 + 8 测试（63 绿）
7. migration/storage 工程命令 + 7 测试（70 绿）+ 真实 inventory 证据

**测试**：142 passed + E2E 2 项（pytest / `npm --prefix apps/web run test:e2e`）
**真实服务**：127.0.0.1:18780 运行中；全能力真实冒烟通过（见 acceptance_mapping §8）

### 各 Phase 状态

| Phase | 状态 | 说明 |
|---|---|---|
| 0 现场盘点 | ✅ | 生产源=main2.5 v2.17.11（容器挂载证据）；letters/deletion 行为矩阵已核验 |
| 1 身份底座 | ✅ | token→principal 服务端解析；registry 统一入口；双库分离 |
| 2 遗忘闭环 | ✅ | §8.5 蓝瓷小钥匙全链通过 |
| 3 原文/quotes | ✅（语义校对 reserved） | 导入幂等/30条/独立检索 |
| 4 计划/日历/bootstrap/time | ✅ | 两入口开窗规则+三时间线 |
| 5 迁移工具层 | ✅ 工具 | inventory/dry-run/verify/backup；真实 apply blocked（未获准快照） |
| 5a letters/deletion | ✅ | 已核验旧规格特征测试；锁状态机子集 |
| 6 MCP/CC | 🟡 协议层✅ | MCP JSON-RPC 双 profile 真实验证；远程 OAuth/CC blocked |
| 7 生活功能 | ⬜ not_started | 媒体/朋友圈/提醒/外部 provider |
| 8 全量验收 | 🟡 | 见 acceptance_mapping.md（如实验收，reserved/blocked 不计入完成） |
| 9 生产切换 | ⛔ 未授权 | 未动旧生产；旧库仍唯一写入 |

### 未动边界确认

- `D:\Ombre-Brain-main2.5` / `D:\Ombre-Brain-dev`：只读（一次 inventory 元数据统计）；容器未停、compose/Tunnel/DNS 未改
- 未读取任何私人记忆/锁信正文；测试全部合成数据；`runtime/` 不入 git
- 端口 18780/5178 为 mariposa 专属；未抢占 8000/18001

### Schema 版本

formal: migration v1-v3（memories/versions/投影/FTS/envelope/resolution/idempotency/audit/outbox + raw/quotes/handoffs/plans/links/activity + letters/deletion_requests）
workspace: migration v1（work_items/proposal_versions/worker_runs/workspace_audit）
