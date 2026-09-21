# PROGRESS

> 会话中断后从本文件与 NEXT.md 续接；每阶段末更新。

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

**测试**：131 passed + E2E 2 项（pytest / `npm --prefix apps/web run test:e2e`）
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
