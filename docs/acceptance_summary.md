# 验收汇总表（PASS / BLOCKED / NOT_IMPLEMENTED / unmapped）

> 复审关卡 2 交付。方法论：**场景 = 01 工程执行文档中的可验证断言**；
> 每个场景映射到具体测试文件::测试名（或 BLOCKED 具体原因），明细见
> `docs/acceptance_mapping.md`（逐行）。unmapped = **0**。
>
> **重要声明**：规格提及的 118 条验收清单原件（`02_验收用例.md`）**从未随
> 工程包提供**（两次附件仅为开工指令 + 01 工程执行文档）。本表按 01 文档
> 构建场景集（78 个场景行），**不是** 118 条原件的映射。请提供 02 原件后
> 对号入座替换本表。

## 汇总

| 状态 | 数量 | 说明 |
|---|---|---|
| **PASS** | **72** | 每项映射到 pytest 测试（142 通过）或 E2E（2 通过）或真实服务实测 |
| **BLOCKED** | **3** | 解锁条件明确，见下 |
| **NOT_IMPLEMENTED / reserved** | **2** | 依规格明确列入后续范围 |
| **unmapped** | **0** | 所有场景行均有归宿 |

证据基线：pytest `142 passed`；E2E `2 passed`；真实服务冒烟（含语义三用例、
副本迁移 dry-run）。

## BLOCKED 明细（原因 + 解锁条件）

| 项 | 原因 | 解锁 |
|---|---|---|
| CC Host 接入 | 本机无 `claude` CLI（`Get-Command` 无结果）；未用 API/`--bare` 冒充 | 安装官方 Claude Code CLI 并核验订阅登录 |
| 远程 MCP OAuth（Claude Chat / GPT Chat） | 无远程端点/客户端注册凭据 | 提供平台连接配置（协议层 /mcp 双 profile 已实测） |
| 真实迁移 apply（生产→mariposa 正式库） | §20 四隔离区要求真实快照单独授权 | 乔生授权快照级 apply（dry-run 到副本已完成，见 REVIEW_GATE §6） |

## NOT_IMPLEMENTED / reserved 明细

| 项 | 依据 |
|---|---|
| 情绪补充召回算法 / 一起听歌 | §21 reserved：契约+禁用状态已落库（`emotion.context.get`/`listening.status` 返回 reserved，有测试）；算法/供应商未定 |
| 部分新能力 Web 页签（朋友圈/提醒/租约/校对工作区） | API/MCP 均已可达并有测试；核心九页签+媒体库+设置已就绪 |

## 分模块 PASS 分布（明细见 acceptance_mapping.md）

| 模块 | PASS | 关键证据 |
|---|---|---|
| 遗忘闭环（§8.5 六条标志性测试全钉） | 15 | test_forget_loop.py / E2E / 服务实测 |
| 检索投影/FTS/语义 | 8+4 | 含真实 ONNX 语义三用例（test_semantic_forgetting.py） |
| 身份/权限/幂等 | 10 | 403 实测×2、并发恰一生效 |
| 原文/quotes/handoff | 15 | 导入幂等/30条/独立检索/校对管线 |
| Home/Self/Diary/meaning | 12 | 隔日规则/归档留底/投影纳入 |
| 计划/日历/bootstrap/time | 17 | 三段分页/SNAPSHOT_STALE/三时间线 |
| letters/deletion（旧规格特征测试） | 18 | 与 main2.5 v2.17.11 核验行为一致 |
| 工作区/租约/批量/冷却 | 10 | LEASE_HELD/逐项冻结 |
| 迁移/备份（含真实副本 dry-run） | 9 | 484/484 映射 unmapped=0 |
| 媒体/sticker/moments/reminders | 10 | hash 去重/白名单/幂等结算 |
| MCP 协议层 | 8 | 双 profile/传输名映射/tool error |
| 安全/并发/恢复 | 7 | XSS/穿越/越权/对账 |
