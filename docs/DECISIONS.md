# DECISIONS

> 实现者的可逆工程默认（非用户原话）。涉及身份/审批/数据保留/隐私的未做擅自变更。

| # | 决策 | 理由 | 替代方案 |
|---|---|---|---|
| D1 | 语义 provider 用本地 ONNX bge-small-zh-v1.5（非云 API） | 真实模型且零计费变更；§21 语义列为可配置 | 云 embedding 待选型 |
| D2 | 语义阈值 0.51 + 相对窗 0.06 + hybrid 语义 top-5 | 实测分布（有效改写≥0.535/泛化≤0.504）；语义定位为补充召回 | 阈值环境变量可调 |
| D3 | 向量"hash 失效 + 查询内限流补算（20/次）+ warmup 全量" | 审批事务不等推理（§8.3）；冷启动一次预热 | 异步 job 队列（后续） |
| D4 | withdraw 复用 proposal_resolutions(decision=withdrawn) | 与旧系统语义一致；唯一终局约束天然防竞态 | 独立撤回表 |
| D5 | 低置信绑定生成 work_item(raw_binding_review, deferred) | §9.2 不自动认领；复用工作区机制 | 独立审阅表 |
| D6 | 迁移幂等键 = (legacy_id, source_type)，映射入 migration_id_map | §6/03 §6 要求；apply 重跑零重复 | 外部映射文件 |
| D7 | 旧 archived 桶迁移为 visibility=archived；dont_surface→hidden | §20.3 旧隐藏不进新检索 | 兼容字段并存 |
| D8 | E2E 断言用"本轮桶词不出现"而非"零结果" | 语义补充召回命中其他相似桶属正常行为 | 关语义跑 E2E（拒绝） |
| D9 | bootstrap unchanged 薄响应（同 snapshot 且状态未变时） | BOOT-08 不重复灌包的最小实现 | session 级缓存 |
| D10 | scan 排除有 meaning/活跃关联的桶（自动候选） | §7.2 意义审查；主体仍可手动提案 | 仅标记不排除 |
| D11 | 幂等记录/审计/向量各自独立表而非 JSON 列 | 可查询可索引；跨库对账需要 | JSON 聚合 |
| D12 | contracts/capabilities.v1.json 由 registry 同源生成 | 防漂移（重导出 diff 为空有验证） | 手工维护（拒绝） |
