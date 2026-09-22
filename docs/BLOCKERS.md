# BLOCKERS

> 每项：原因 / 已尝试 / 影响范围 / 解锁条件。blocked 只阻塞相关链路，不影响其余工作。

## B0 · chat.*/voice.*/group.*/wishstar.*/wakeup.*（v1.1 规格 blocked 注册）
- 与 B1/B4/B5/B6 同源（CC/Siren/扎西德勒/Superposition/wakeup）；
  兼容层已注册这些能力名并如实返回 blocked 状态与解锁条件（不假实现）。

## B1 · CC Host 全链（T-CC-01..06；T-ID-10）
- 原因：本机无 `claude` CLI（`Get-Command claude` 无结果）
- 已尝试：无（不装 CLI 不做任何 API/`--bare` 替代——按 00 §9 禁止冒充）
- 影响：CC 聊天/会话/取消/原文归档验收 6 条；bootstrap 的 cc profile 已就绪待接
- 解锁：安装官方 Claude Code CLI 并核验订阅登录（`CC_AUTH_MODE=subscription_cli`）

## B2 · 远程 MCP OAuth（T-ID-07）
- 原因：无远程端点/客户端注册凭据
- 已尝试：协议层 /mcp + /mcp/maintenance 已实现并实测（本地双 profile 8 测试）
- 影响：Claude Chat / GPT Chat 平台真实连接验收
- 解锁：提供平台连接配置

## B3 · 真实迁移 apply（MIG-10 同族）
- 原因：§20 四隔离区要求真实快照级 apply 单独授权
- 已尝试：snapshot（484 文件字节级副本）+ dry-run-real（全量映射）+ 合成 apply 幂等演练
- 影响：生产数据切换（Phase 9 前置）
- 解锁：乔生授权快照级 apply；rollback 演练随切换审批执行

## B4 · 外部 provider 写入接入（T-EXT-02/03）
- 原因：无 Siren/Superposition 服务凭据；Siren 自身语音 provider 为 dev 回退（健康检查实测）
- 已尝试：三家只读契约核验（docs/provider_contracts.md）
- 影响：语音/星星/群聊真实写入与 OUTCOME_UNKNOWN 对账验收
- 解锁：为 mariposa 建独立服务凭据（§3.3 不透传用户 token）

## B5 · 扎西德勒
- 原因：8787 未监听（服务未运行）
- 影响：群聊存档 provider
- 解锁：由乔生启动服务后核验

## B6 · wakeup（T-EXT-05）
- 原因：AUTO_WAKEUP_ENABLED=false（§21 默认）；无常驻 scheduler
- 已尝试：提醒到期幂等结算已实现（maintenance.reminders.fire_due）
- 影响：自动唤醒
- 解锁：配置+真实验收后开启

## B7 · 语义检索（已解除 → 记录）
- 原状态：SEMANTIC_PROVIDER 未配置 → degraded
- 现状态：本地 ONNX bge-small-zh-v1.5 已接（真实模型），T-RET-03/04 过
- 遗留：云级 provider（如需）待选型；不做计费变更
## B8 · v2 原文区间绑定与自动绑定评测（V2-RAW-01..13）
- 原因：区间表/确认展开为第二批开发项；自动绑定阈值按 S4 必须先评测
- 已尝试：表结构已建（raw_binding_ranges/raw_context_grants 迁移 11）
- 影响：多区间/反查/展开确认/自动绑定验收 13 条
- 解锁：第二批实现 + 标注集评测完成并获确认后配置阈值

## B9 · v2 前端页面与移动端 E2E（V2-OPS-08 等）
- 原因：本轮交付后端核心+既有页面回归；新 v2 页面未实现
- 影响：桶分栏/召回筛选/审查台/I/原文选区 UI 及对应 E2E
- 解锁：第二批前端开发

## B10 · v2 语义检索投影切换（V2-SEARCH-14/15）
- 原因：BGE 通道仍读 v1 投影；需切 v2 白名单投影+hash 校验+影子切换
- 影响：语义召回对 v2 桶的合规性验收
- 解锁：第二批 retrieval/semantic v2 化
