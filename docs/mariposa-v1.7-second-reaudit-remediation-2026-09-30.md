# Mariposa 二轮复审整改 · 交付报告（2026-09-30 凌晨）

对象：Codex 第二轮复审（REJECT FOR FREEZE：P1×3 / P2×2 / P3×1）。
分支 `fix/v17-audit-20260929`，前序 HEAD `f2fd373`。

## 逐项矩阵

| 项 | 复审要点 | 修复 | commit | 测试 |
|---|---|---|---|---|
| **P1-01** commit-at-end 只做一半 | no-budget refine / reject / accept / navigate / close 的业务状态与 operation 记录分离，崩溃窗口破坏幂等 | 五个动作全部单事务化：状态写入 + operation 收据同一 `BEGIN IMMEDIATE` 提交；`run_operation` 删除事后补写，改为 fail-fast（builder 未在最终事务内记账即暴露） | `2323c2b` | 每动作同 key 重试重放首次结果（close 重试不再 INVALID_STATE；no-budget refine 重试不再推进 revision）+ 注入记账失败断言状态整体回滚，共 5 个 |
| **P1-03** Source 租约无 fencing | 旧 worker 复活可 `._fail_batch` 删掉接管方快照、改 metadata | formal migration 23 加 `lease_token`；认领/接管都换发新 token；会话级解析事务、发布事务、`_fail_batch`（在 purge **之前**）全部写锁内校验；LeaseLost 时不写任何东西（含 metadata） | `6a16b0c` | 复活旧 worker 无法破坏接管方成功证据（快照/成员/发布行/DB 状态全完好）；接管换发新 token 且旧 token 即时失效 |
| **P1-02** dense 未接 v1.7 | 旧整投影进 embedding 绕过 CORE；dense 卡 content_version=None / matched_fields=["projection"] 被 guard 自杀 / 证据空 | 语料改 `whitelist_body`（事件正文，三阶段全允许；旧数据回退整投影）；dense 卡携带真实 revision、`matched_fields=['event_text']`、真实 whitelist_body 行供证据；provider 保持默认 disabled | `35eb289` | 确定性 fake embedder：CORE 记忆经 meaning 的语义相似不再被拉回；dense 卡版本/字段/证据齐备且同 key 重放不被删 |
| **P2-01** 投影不一致 | 刚 hold 时 why 搜不到、rebuild 后突然可搜 | v2 hold 改走统一 `rebuild_full_projection`（body+why+meaning，whitelist 只含正文）——hold/update/meaning/rebuild 四路投影内容一致 | `2aef027` | hold 后与 rebuild 后 search_text/whitelist_body 逐字节相等、why 命中数一致 |
| **P2-02** plan 宣称废弃的 20 天 | `forgetting_note` 等三处进 API 数据包 | 全部改为 v1.7 真实口径（终态当天 CORE；查看不续期） | `2aef027` | plan.get 响应无"20"字样、含 CORE |
| **P3** 锁无界增长 | `_OPERATION_LOCKS` 只增不减 | refcount 注册表：等待者归零即淘汰 | `2aef027` | 既有幂等/并发套件回归 |

**遗留决策项（REQUIRES CONTRACT DECISION，未擅动）**：P2-01 的后半
——`memory.search` / `memory.recall` 两个公开 capability 是否降级为
管理查询、或路由到 v1.7 阶段策略（会改变这两个入口的返回语义）。
本轮只修了投影内容一致性；入口归属是产品契约决策，留给江乔生。

## 测试证据

```text
pytest tests/unit -q                          467 passed, 1 skipped (23.85s)
pytest tests/acceptance tests/integration -q  122 passed (1.75s)
合计                                           589 passed / 1 skipped / 0 failed
```

新增：P1-01×5、P1-03×2、P1-02×2（dense-only，fake embedder）、
P2×2；改写 F26 组至"builder 事务内记账"契约。并发项为真实线程；
lease 测试含真实接管路径。分批前台执行，临时隔离根，无资源异常。

## 边界确认（复审声明项）

- Jev/embedding 第三方计费 exactly-once：维持方案排除项（检索在最终
  事务前执行，跨进程同 key 竞争可能双计费一份、库里仍只有一份正式
  结果）。切真实付费 provider 前需她知悉。
- dense `local_bge_zh` 默认仍 disabled；本轮完成的是接线正确性，
  生产启用前建议再跑一次含真实模型的 warmup 验证。

## 冻结状态主张

P1-01/P1-03/P1-02 三个阻断项 + P2/P3 均已闭合，589 全绿。剩余开放项
只有两个产品决策（legacy 检索入口归属；dense 生产启用时机），不属
代码缺陷。Phase 1 是否可冻结由她在 Codex 复验后裁定。
