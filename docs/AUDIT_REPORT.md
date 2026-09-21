# mariposa 全量审计报告

> 致：林石见（工程整理/复核）
> 出具：程知行（GLM，实现方自查审计）· 2026-09-21
> **Audit HEAD: `6072d76e33b83a966a4c98a419398d530b8c8951`**
> `git rev-list --count HEAD`: **18**；`git status --porcelain`: **empty**
> §5.1 的修复与本文档同在 commit `6072d76` 内（审计取证基线为 `9c9065e`，
> 即第 17 个 commit；§5.1 修复+报告本身构成第 18 个）。
> 方法：不引用既有结论，全部证据为审计时**重新执行**所得；含两处如实记录的
> 审计发现（其一当场修复）。

---

## 1. 执行取证（命令与结果）

| # | 检查 | 命令 | 结果 |
|---|---|---|---|
| A1 | 后端全量测试 | `.venv\Scripts\python -m pytest tests --basetemp=.pytest_tmp -q` | **131 passed**（2 warnings：anyio 别名弃用，非本仓代码） |
| A2 | Web 构建（TS 严格模式） | `npm --prefix apps/web run build` | ✓ built |
| A3 | 契约与代码一致性 | 重新 `export_contracts.py` 后 `git diff contracts/` | **无差异**（111 能力，文件由 REGISTRY 同源生成） |
| A5 | 浏览器 E2E | `npm --prefix apps/web run test:e2e` | **2 passed**（完整遗忘闭环 + 日历） |
| A6 | git 状态 | `git status --porcelain` | **empty**；rev-list HEAD=**18**（审计取证基线 `9c9065e`=17） |

## 2. 服务级不变量抽查（真实 HTTP，非仅单测）

对运行中的 127.0.0.1:18780 以三个主体 token 重演核心链（审计专属词"靛蓝鹿角"）：

| 不变量 | 结果 |
|---|---|
| 写桶后旧词命中（full 投影） | ✅ |
| 遗忘审批后旧词**不**命中；摘要词命中且 `matched_by=summary_keyword` | ✅ |
| 恢复后旧词重新命中 | ✅ |
| worker 调 `memory.search` → 403 FORBIDDEN | ✅ |
| worker 调 `memory.forgetting.decide` → 403（工具人无审批权） | ✅ |
| 同 Idempotency-Key 重复审批 → `idempotent_replay=true`，仅一个压缩版本 | ✅ |
| **直查 SQLite**（绕过 API）：`retrieval_documents` 与 `search_fts` 行数一致（30==30）；恢复桶投影含旧词、无孤儿投影 | ✅ |

## 3. 泄露面检查

| 检查 | 结果 |
|---|---|
| 开发 token 全 git 历史检索（`git log -S <token>`） | **零命中**（从未进入任何提交） |
| `runtime/`（库/日志/对象/token）被 git 跟踪 | **未跟踪**（.gitignore 生效） |
| "蓝瓷小钥匙"等测试词出现位置 | 仅测试/文档（该词本身是规格 §8.5 的合成测试用词）；fixtures 信件正文为合成并已声明 |
| 审计 payload 抽查 | 不含正文/凭据（memory.created 只记 entry_source/date；semantic_corrected 的 reason 截断 200 字符） |

## 4. 旧生产边界复核（本节含一项审计发现）

| 检查 | 结果 |
|---|---|
| 生产容器 `ombre-brain` StartedAt | 仍为 2026-09-14T23:34Z（**未重启/未触碰**） |
| `D:\Ombre-Brain-main2.5\buckets-data` 只读 inventory 复核 | **484 文件 / 570711 字节 / 1 锁信**（09-21 基线为 481/564644/1） |
| **+3 文件定性** | 差异目录 `dynamic\恋爱`（+2）与 `plans\active`（+1），文件时间戳为今日上午——**旧系统自身运行产生**（生产仍在被正常使用，符合"旧库唯一写入"）。mariposa 代码库内不存在对该路径的任何写入调用；mariposa 全部数据在 `D:\mariposa\runtime\`。**结论：边界未违反** |

## 5. 审计发现（如实）

### 5.1 当场修复

**[中] inventory 锁信检测读取头部 2000 字节**，可能越过 frontmatter 触及锁信正文开头（§14.3 只允许解析必要头部元数据）。已修复：改为只读至第二个 `---`（frontmatter 结束符），回归 131 测试全绿。审计前实现从未输出正文，但读取范围不合规，特此记录。

### 5.2 已知限制（先前已在文档声明，复核属实）

| 级别 | 项 |
|---|---|
| 中 | bootstrap 分页仅覆盖 raw 部分；memory/plans 全量返回（数据量小可接受；超预算只告警不截断） |
| 中 | media 上传 stage 为进程内存暂存：重启丢未完成上传（token 10 分钟过期缓解） |
| 中 | letters 归档语义简化：letters 表无独立 archived 状态，archive 动作仅记审计（代码注释已标注，Phase 5 真实迁移前需补） |
| 中 | Web 认证为 localStorage Bearer：无 CSRF 面（非 cookie 自动附带）但存在 XSS 窃取面；React 全量转义 + API 仅 JSON；未加 CSP 响应头 |
| 中 | MCP 协议子集：无 SSE 兼容、无 session 管理、不支持 batch |
| 中 | workspace 状态回填在正式库事务之外（崩溃窗口由 `reconcile_workspace` 对账兜底，有测试） |
| 低 | `stop-dev.ps1` 按命令行含 "mariposa" 匹配进程，理论可误杀无关同名进程 |
| 低 | reminder 到期结算无常驻 scheduler（幂等手动/可接入） |
| 低 | `audit_events` 无归档策略（长期增长） |
| 低 | E2E 个别 UI 提示条断言因竞态改为功能断言（核心链路全断言） |

### 5.3 过程性勘误（供信任校准）

第 4 轮曾报告"工程文档可做项已清零"，经重查**不实**（漏报 §17.3 必需项 15 项），第 5 轮已全部补齐并在 PROGRESS 勘误。本轮审计逐条核对后，**当前**"必需能力除 blocked 外全覆盖"的声明与代码一致。

## 6. 声明 vs 实际核对

| 声明 | 实际 | 一致 |
|---|---|---|
| 后端 131 测试 | 131 passed（审计重跑） | ✅ |
| E2E 2 项 | 2 passed（审计重跑） | ✅ |
| 能力 111 | contracts.v1.json 111 条，重导出无 diff | ✅ |
| git 18 commits（Audit HEAD 6072d76） | rev-list=18，status empty | ✅ |
| schema formal v1-v8 / workspace v1-v2 | `schema_migrations` 实查一致 | ✅ |
| §17.3 必需能力 | 除 blocked 项逐条可达（HTTP 与 MCP 同 handler） | ✅ |
| blocked 清单（CC/OAuth/语义/真实迁移/外部 provider/扎西德勒） | 全部有解锁条件，无一项被冒充 | ✅ |

## 7. 复核入口（给林石见）

```text
docs/PROGRESS.md                 五轮过程+勘误（含第4轮过度宣称记录）
docs/acceptance_mapping.md       场景→测试逐条映射；§7 未实现如实清单
docs/legacy_behavior_matrix.md   旧 Ombre 行为核验（源码级）
docs/provider_contracts.md       外部服务只读核验（含 Siren dev 回退事实）
docs/00_local_baseline.md        生产源判定证据
contracts/capabilities.v1.json   111 能力（同源生成，可直接对 §17.3 打勾）
复跑：.venv\Scripts\python -m pytest tests --basetemp=.pytest_tmp -q
     npm --prefix apps/web run test:e2e
服务：http://127.0.0.1:18780（/ 旧直出页，/app React 版）；token 在 runtime/dev_tokens.json
```

## 8. 结论

1. 授权范围内的隔离开发**与仓库现状相符**；无删除失败用例/放宽权限/伪造接通的情况。
2. 旧生产边界**未违反**（+3 文件为旧系统自身运行；容器未动）。
3. 两项审计发现：其一（inventory 读取范围）当场修复；其余已知限制如实列于 §5.2。
4. blocked 项（CC/远程 OAuth/语义 provider/真实迁移 apply/外部写入接入）**均未开始亦未冒充**，解锁条件明确，留待授权。

——实现方自查审计完毕。本报告的证据均可在本机复现。
