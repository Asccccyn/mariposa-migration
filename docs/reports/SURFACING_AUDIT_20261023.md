# 同轮浮现合同与预算整改 · 2026-10-23

本批在 `audit/surfacing-wake-keepwarm-20261023` 隔离分支完成；基线 `23b777d`。没有部署、重启、修改生产数据库或更换生产凭据。

## 现行合同

- `memory.surfacing.prepare` 接受可选 `deadline_at`。EST 必须传接受消息时形成的绝对总期限；M 检索、后续 Judge 与 validate 共用该期限，不能重新给满预算。独立旧调用未传时保留有界本地上限。
- `validate` 的 source 引用、target 消息/修订/body_hash 必须与 prepare 快照完全相符。旧跨轮投递被拒绝；running/unknown 操作按已有 Busy/OutcomeUnknown 返回，不重复付费。
- `check` 重新读取当前开关、provider 与外发许可，不对这个纯本地操作再收费或预占。
- 多事件继承日期使用原始文本内已验证的日期跨度；不能丢掉用户明确给出的日期约束。
- `memory.detail.read` 继续提供 memory/relations/source 分层全文与分页；无法读取来源时保留 `incomplete_sections.source=unavailable`，不把失败投影为空。

## JEV 派发预算

运行库增加迁移 **17**，仅扩展既有 `surface_judge_ledger`。每次真实 HTTP（包括 429/529 有限重试）先在同一 SQLite 写事务中检查现有用户次数/金额限额并预占，释放事务后才出站。未知结果保留预占；阶段结算不重复计数。

公开报价快照正本为 `config.SURFACE_JEV_PRICE_SNAPSHOT`：[TypeSafe Models](https://docs.typesafe.ai/models)，2026-10-23 核验：Jev 1.13/1.14 输入 $0.042/百万 token、输出免费。当前适配器不发送 long-context header；使用 65,536 token 保守上界，每次预占 $0.002752512。没有新增或改写用户日金额限额。

返回的 `usage.input_tokens × 公开报价` 写入 `est_cost_usd`，`cost_basis` 明确标为 `usage_estimate`，不是供应商账单。型号/usage 不可核或结果未知时留足原预占。未核价型号拒绝出站；alias 解析到未核价版本后阻断后续请求。

日界使用 Asia/Shanghai，并按 `created_at` 查询日区间，因此历史 UTC day 字段不被改写。历史当日收费记录若没有金额依据，返回 `SURFACE_COST_UNKNOWN`，需要对账或等待新的业务日；不得把未知费用当零清掉。

## 验证

- 17 个指定测试文件，**221 项通过、0 skip**（两批 177 + 44）。含 HTTP 受限凭据、并发最后额度、逐重试预占、未知金额/型号、同轮绑定、多事件日期、源全文与 205 条双向关系分页。
- 真正启动的隔离 FastAPI + EST 默认 HTTP client：13 请求，4 个同日独立事件，source 6 页完整还原，越权能力 403、缺认证 401；Judge 为明确的离线替身。
- 本轮另由根代理执行 6 个合成语义场景，真实 JEV **2 次**；明确同事件走确定性 prepare + 真实 delivery，异事未确认，其余普通/未来/日期冲突/无候选场景无新增 JEV。样本不能推导生产准确率。
- 原文 `memory_source_spans` 加入现有测试 reset 清单；过时跨轮正例改为同轮，错误跨度夹具校正，未删除失败断言或放宽判断阈值。

## 仍需区分

本地已启动的 Dense 推理不能在 C++ 调用中间强制终止；本批在各阶段前后和 SQLite 进度点检查总期限，超时后不再 JEV、不交付。未下载或加载嵌入模型，未把离线时延冒称 Dense 性能。

真实主模型 OR/Opus 缓存没有新增付费验证。EST 的 OR final-wire 与稳定前缀由 fake transport 验证；不能用真实 DS/JEV 成功代替 OR 供应商缓存证据。
