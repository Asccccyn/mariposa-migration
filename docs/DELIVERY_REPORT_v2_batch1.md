# mariposa v2.0.1 · 第一批实施交付汇报（致林石见）

> **出具：程知行（GLM，实现方自查）· 2026-09-22**
> **候选 HEAD：`2bd781a8`**（本批 4 个提交，基线 `1f22c15`）
> **性质：实现者自查，不是独立审计。** 状态口径按规格 §19：
> `tested_local` / `implemented_not_tested` / `not_implemented` / `blocked`。
> 本批**无任何 `verified_live`**：未连接外部平台、未动生产、未迁移真实数据。
>
> 业务依据：`mariposa_v2.0.1_工程交付包.zip`（已归档 `docs/spec_v2/`，
> 含 109 条验收矩阵 JSON 与三份文档）。

---

## 1. 执行取证

| # | 检查 | 结果 |
|---|---|---|
| A1 | `pytest tests -q`（隔离 MARIPOSA_ROOT + 项目内 basetemp） | **289 passed**（基线 198 → P1 后 214 → 本批 289；新增 75 条 v2 用例） |
| A2 | E2E `playwright test`（**隔离实例**：webServer 自启、端口 18799、独立库与 token，不复用业务 18780） | **2 passed**（浏览器级遗忘闭环 + 刷新/移动端） |
| A3 | `pip check` / `npm --prefix apps/web run build` | 通过 / 通过 |
| A4 | git | 本批 4 提交，工作树干净；无生产路径变更 |
| A5 | 能力注册表 | 核心 137 项；含 v1 兼容层 202 项；19 项 v2 能力带真实输入 schema（`V2_INPUT_SCHEMAS` 同源） |

**本批提交**：
`582f681`（P0/P1 收尾）→ `9366740`（P2–P6 后端核心）→ `651d647`（维护性重构，零行为）→ `2bd781a`（docs）。

## 2. §2 技术阻断（B01–B08）对照

| ID | 状态 | 说明 |
|---|---|---|
| B01 MCP 多段名反解 | ✅ 基线已修（`1f22c15`，查表反解+全量 roundtrip 测试） | 本批未动 |
| B02 幂等并发副作用 | ✅ 基线已修原子 claim；**本批加固崩溃窗口**：running 残留不再盲删重放，改 `OUTCOME_UNKNOWN`，经 `maintenance.idempotency.reconcile` 显式对账后 `failed→可重试`（V2-OPS-05 有测试） | |
| B03 严格 schema | ◐ 部分：v2 能力 19 项带真实 schema 且真实校验（含 items/嵌套/anyOf）；v1.1 包 12 项沿用 | |
| B04 NotFound 导入 | ✅ 基线已修 | |
| B05 证据映射如实化 | ✅ 基线已修（持久校验测试钉住） | |
| B06 测试根保险丝 | ✅ 本批：conftest fail-closed 白名单（系统临时 / `.pytest_tmp` / `runtime/verification` / `MARIPOSA_TEST_ROOTS`），realpath 规范化比较，业务根拒绝（含子进程级测试，V2-OPS-07） | |
| B07 `_fix_gate.py` 残留 | ✅ 基线已删 | |
| B08 扫描饥饿/defer 空转 | ✅ 本批：`(memory_date, memory_id)` 稳定游标分页（NULL 日期段 NULL-safe，防饥饿有回归测试）；defer 实际落 `deferred`；`workspace.proposals.withdraw` 独立工具 | |

## 3. 二十四模块（附录A）状态

| 模块 | 本批后状态 | 备注 |
|---|---|---|
| M01 身份 | ◐ | 新增 `linshijian` 受限审查 principal + `review_delegations` 委托表（开发默认委托）；worker 未升权 |
| M02 双库 | ✅ 可复用 | v2 审查走新表 `v2_review_items`（不重建 work_items 的 CHECK，DECISIONS D19） |
| M03 桶写入 | ✅ v2 分层 | hold 带标题/八分类/事件/同期心情/我们的话/creation_mode；作者自凭据 |
| M04 八分类 | ✅ | 平行多选、同桶一分类一行、取最长周期 |
| M05 当时心情 | ✅ | 仅周家明+仅同窗口；心情空白≠不重要；只标签可检索 |
| M06 我们的话 | ✅ | speaker/ordinal/expression_kind；不索引 |
| M07 回忆与打开 | ✅ | open→confirm→append 回执链；同桶同身份同版本；修订留底 |
| M08/M09 原文 | ❌ 未实施 | `raw_binding_ranges`/确认展开/评测关卡全部未做（BLOCKERS B8） |
| M10 召回投影 | ◐ | `memory.recall` 全套筛选+BM25+白名单（新写桶）；语义通道 v2 化未做（B10）；旧 v1 桶投影祖父条款（D17） |
| M11 遗忘期限 | ✅ | 自然日语义全量落地（见 §4 RET） |
| M12 审查闭环 | ✅ 后端 | 生成→林石见受限审查→放行/转疑难→终裁 keep 终局 |
| M13 计划/纪念 | ◐ | 计划终结锚点全部落地；纪念日仅表+开窗查询，无管理工具 |
| M14 I | ✅ | 正本版本、仅周家明写、乔生建议不改稿、无情绪门槛 |
| M15 开窗 | ✅ 后端 | v2 基础包（三天标题+心情、I、0..3 日日程全文、纪念日；无 30 条原文） |
| M16 日历 | ◐ | 事件日期维度沿用；v2 字段未进日历聚合 |
| M17 历史恢复 | ✅ | 恢复创建新表示版本；遗忘桶打开只读摘要 |
| M18 MCP/HTTP | ◐ | 名称 roundtrip 含全部新工具；`/api/v2` 版本路由与 v2 契约 JSON 未做 |
| M19 幂等 | ✅ | 同键同内容恰一副作用（6 路并发测试）+ 崩溃窗口语义 |
| M20 Web 页面 | ◐ | 既有页面回归通过；v2 新页面（分栏/筛选/审查台/I/选区）未做（B9） |
| M21 媒体/表情等 | ✅ 保留 | 未受重构影响（测试全绿） |
| M22 CC/外部 | ❌ blocked | 与 BLOCKERS B1/B2 同源，未变 |
| M23 信件/删除 | ✅ 保留 | 行为未动 |
| M24 迁移 | ◐ | 表结构就绪、合成库迁移测试通过；**旧库→v2 字段逐条映射未做**（NEXT #3） |

## 4. 109 条验收矩阵覆盖（自查口径）

> ⚠️ 尚未做矩阵 JSON 的机器化 nodeid 映射（NEXT #5）；下表为实现者逐条
> 自查，标注"已测"的均有对应 pytest 用例（文件见 §6），但**不是**按矩阵
> ID 逐条登记的证据链。

| 组 | 已测 | 部分/说明 | 未覆盖 |
|---|---|---|---|
| REC ×10 | 01,02,03,04,06,07,09 | 05（无绕行入口，结构性成立但无显式负向测试） | 08（完整闭合记录断言弱）、10（迁移未做） |
| RET ×16 | 01–09,11,13,14,15 | 08（打开使提案失效已测，读作 PROPOSAL_STALE/NOT_DUE 皆拒）、12（顺序提交已测，真并发未单列） | 10（沿用 v1 测试）、16（Asia/Shanghai 已测；夏令时合成时区未测） |
| REV ×14 | 01–09,13 | 12（材料四件套+tags diff 已给；心情文字可展开=字段存在） | 10（未说不变已说：无摘要-心情对照断言）、11（特殊词仅不拦截，无专测）、14（迟到结果拒绝未测） |
| SEARCH ×17 | 01–11,17 | 16（restore 新表示版本沿用 v1+新测试） | 12（结构成立无专测）、13（tags 证据链未实现）、14,15（重建/迟到 embedding：语义 v2 未做） |
| RAW ×13 | — | — | **全部**（B8） |
| VIEW ×5 | 01–05 | — | — |
| PLAN ×11 | 01–05,08,09,11 | 06（3 日窗口+逾期已测；长文分节续取沿用段机制）、07（纪念日表+开窗，无周期规则工具） | 10（计划侧送审审查未接 v2 闭环：generate 目前仅处理 memory） |
| BOOT ×8 | 01–05,07,08 | — | 06（长文 continuation 未做全段分页） |
| I ×3 | 01–03 | — | — |
| OPS ×12 | 01–07,09 | 08（移动端 E2E 仅既有两条，非 v2 全链） | 10（影子迁移）、11（机器证据链）、12（边界遵守无自动化断言） |

**合计：约 70/109 有直接测试证据，约 16 部分覆盖，23 未覆盖（含 RAW 整组 13）。**

## 5. 本批发现并修复的基线（HEAD）既有问题

1. **E2E 配置违反规格 §16**：playwright 无 webServer、直连业务库 18780 实例、token 读业务 `runtime/dev_tokens.json`。已改隔离自启（18799/独立根/独立 token）。
2. **`workspace.forgetting.scan` 严格 schema 必填 `policy_version` 与处理器行为不符**（`1f22c15` 引入），基线上该工具经 HTTP 调用必失败。已在 v2 schema 层对齐（提供则校验）。
3. **前端 `workspace.proposals.submit` 传参错误**（缺 `proposal_hash`、字段名用旧 `revision`），基线 E2E 实际已跑不过。已按冻结 hash 修复。
4. （附带）静态资源曾按 `MARIPOSA_ROOT` 数据根定位，隔离根下 404。已改按代码树定位。

以上说明 `1f22c15` 声称的"E2E 2 全绿"在其 schema 接入后未复验——本批已修复并复验。

## 6. 证据与复跑入口

- v2 用例：`tests/unit/test_p1_fixes.py`、`test_v2_layers.py`、`test_v2_plans.py`、`test_v2_review.py`、`test_v2_recall.py`、`test_v2_bootstrap.py`（每组 class/用例名标注对应 V2-XXX 规则号）
- 旧用例 superseded 更新 4 处（V2-BOOT-01/03/08，测试内注明）；无删除、无放宽断言
- 复跑（PowerShell，隔离环境模板见规格 §16.1；测试根受保险丝保护）：
  ```powershell
  $env:MARIPOSA_ROOT = "D:\mariposa\runtime\verification\v2-review-<stamp>\isolated-root"
  .venv\Scripts\python.exe -m pytest tests -q --basetemp=.pytest_tmp\review
  Remove-Item Env:MARIPOSA_ROOT
  npm --prefix apps/web run build
  npm --prefix apps\web run test:e2e   # 自启隔离实例 18799，勿与业务 18780 混淆
  ```

## 7. 待你们确认的解释性决策（DECISIONS D15–D23 摘要）

- **D16**：纯 plan 分类/未分类桶 → 不自动压缩（规格未给期限；保守方向）。待乔生定数值后改。
- **D18**：共同话语检测 = 字面同一句；"一方说另一方接住"留给语义评测阶段。
- **D20**：review.submit(release) 单事务内 ready_to_apply→生效（中间态入审计）；单进程无独立执行器凭据，双凭据待 MCP 常驻后引入。
- 其余 D15/D17/D19/D21–D23 为工程等价实现选择，详见 `docs/DECISIONS.md`。

## 8. 边界声明

- 未动 `D:\Ombre-Brain-*`、AI-Companion、Zashidele、siren、superposition、DevSpace 及其端口/凭据/隧道；
- 未动 Cloudflare/DNS/OAuth/平台账号/API 计费；无真实消息/信件/记忆正文写入；
- 未迁移真实数据（表结构与合成库迁移已就绪，真实副本 dry-run 待授权）；
- 自动绑定阈值保持 `null / awaiting_evaluation / automatic_apply_enabled=false`，无预设分数；
- runtime/ 与测试产物未入 git；token 不出现在任何 git 文件。

## 9. 下一步（NEXT.md 已排）

原文 v2（RAW 整组）→ 语义 v2 投影 → 迁移映射 → 前端 v2 页面 → 矩阵机器化证据链 → v2 契约 JSON/版本路由。旧代码四个长函数（`workspace.decide/submit`、`apply_forget_approval/restore`）重构留待独立一轮。

——请林石见按 §4 覆盖表抽验复跑；"部分/未覆盖"项不应当作 PASS。
