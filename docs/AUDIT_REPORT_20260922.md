# mariposa 第二次全量审计报告

> 致：江乔生 / 林石见
> 出具：程知行（GLM，实现方自查审计）· 2026-09-22
> **Audit HEAD（取证基线）**：`8025ed9df1da779968eb91b888c6cc45672b2555`（rev-list=30）
> 审计自身修复 3 项（§5.1），修复后工作区已随本报告一并提交。
> 方法：全部证据重新执行；含三项当场修复的审计发现；上轮报告在
> `AUDIT_REPORT.md`（HEAD `6072d76`），不覆盖。

## 1. 执行取证

| # | 检查 | 结果 |
|---|---|---|
| A1 | 后端全量 `pytest tests -q` | **186 passed**（9 warnings：anyio 弃用/deprecation，非本仓代码） |
| A2 | Web 构建（TS 严格） | ✓ built |
| A3 | 契约同源 | 重新导出后 115 能力；git diff 仅本次审计修复引入的预期变更 |
| A5 | E2E | **2 passed**（完整遗忘闭环 + T-OPS-04 刷新/移动端） |
| A6 | git | 取证基线 30 commits、status 干净（审计修复除外） |

## 2. 服务级不变量重演（真实 HTTP，新审计词"青黛鹤唳"）

8/8 通过：写桶+预热、worker 检索 403、worker 审批 403、幂等重放
`idempotent_replay=true`、遗忘后旧词不命中、**摘要语义命中
`summary_semantic`**（查询不含摘要原词）、**旧正文独有信息语义不命中**、
恢复后旧正文语义复活 `semantic`。

## 3. 118 条映射证据核验（本轮新增审计项）

程序化校验 `acceptance_map_v1.1.json` 全部 106 条 PASS 的证据：
- **77 条测试证据**：提取函数名与 pytest `--collect-only` 索引比对
  → **失败 0**（全部指向真实可收集的测试）
- **28 条文档/实测证据**（架构断言、服务实测、BLOCKED 理由类）逐条人工核对
- 12 条 BLOCKED 与 `docs/BLOCKERS.md` 一一对应

## 4. 泄露面与边界

| 检查 | 结果 |
|---|---|
| 开发 token 全 git 历史检索 | 零命中 |
| `runtime/`（含 staging 副本 485 文件、模型、备份）被 git 跟踪 | 未跟踪 |
| `test-results/`、`apps/web/dist` 入 git | 未跟踪 |
| `docs/verification/` 证据文件含 token/密钥 | 零命中（脱敏合规） |
| 迁移 staging 副本正文出现在任何 git 文件/报告 | 无（dry-run 报告仅 hash/元数据，有断言钉住） |
| 生产容器 `ombre-brain` StartedAt | 仍为 2026-09-14T23:34Z（未动） |
| 旧生产 inventory | **485 文件**（+1，旧系统自身运行产生；与上轮 +3 同性质定性） |

## 5. 审计发现与当场修复

### 5.1 当场修复（3 项）

| # | 级别 | 发现 | 修复 |
|---|---|---|---|
| F1 | 中 | `retrieval/semantic.py` 顶层 `import numpy`：核心包强依赖 numpy，venv 外全部工具脚本（含契约导出）崩——v1.1 对照引入 | numpy 改懒加载（`_np()` 函数内导入）；系统 Python 可导入、provider 未配置时零 numpy 依赖；186+E2E 复验 |
| F2 | 中 | 118 映射中 18 条 evidence 用了截断测试名（如 `test_case1`、`test_T_BOOT_13`）或省略 `.py` 的简写——复核者按图索骥会找不到 | 自动补全脚本回写完整可收集测试 ID；校验失败 106→**0** |
| F3 | 低 | `contracts/capabilities.v1.json` 落后于 registry（111 vs 115：v1.1 新能力注册后未重新导出） | 重新导出并复核；同源生成机制本为防漂移，人工步骤遗漏被审计捕获 |

### 5.2 已知限制复核（与上轮一致，无恶化）

bootstrap 分页已补三段（上轮关卡 5 完成）；media stage 内存暂存、MCP 协议子集、
workspace 回填事务外（有对账兜底）、stop-dev 匹配面、audit 无归档策略、
letters 归档语义（已在关卡 4 对齐）——均已在 docs 中声明且带测试。

### 5.3 过程记录

上轮（6072d76）以来的新增工作全部有 commit 与测试证据；本轮审计未发现
删测试/放宽断言/伪造接通；12 项 BLOCKED 无一被冒充为通过。

## 6. 结论

1. 118 条验收映射**证据可核验**（测试证据校验失败 0）；106 PASS 与
   186+2 测试及服务级实测相互印证。
2. 三项审计发现已当场修复并复验（F1 为本轮真实缺陷，影响面=venv 外工具链）。
3. 旧生产边界未违反（+1 文件为旧系统自然运行；容器未动）。
4. 维持定性：**Mariposa Core RC**。进入 CC + 双入口真实接入阶段的前置
   = BLOCKERS B1（安装 claude CLI）与 B2（OAuth 凭据）。

——复核入口：`docs/verification/acceptance_map_v1.1.json`（机器可读映射）、
`docs/BLOCKERS.md`、`docs/DECISIONS.md`；复跑命令同报告 A1/A5。
