# PROGRESS

> 会话中断后从本文件与 NEXT.md 续接；每阶段末更新。

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

**测试**：90 passed + E2E 2 项（pytest / `npm --prefix apps/web run test:e2e`）
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
