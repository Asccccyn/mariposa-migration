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
| D13 | v1.1 规格 150 项最低能力的缺口以三类兼容：别名（6，同 handler）、blocked/reserved 注册（31+8，如实返回状态不假实现）、薄实现（18，单条查询/快捷动作/hold_candidate 最小机制）；另有 4 项为规格名与实现名并存 | 林石见复审指出契约未完整落地；blocked 状态本身是规格要求的交付物 | 只实现真实可用的部分 |
| D14 | 幂等键由调用方显式给出即生效（与 cap.idempotent hint 无关）；原子 claim=先 INSERT running 占位（60s 崩溃残留可清理）+ 有界等待对方终态 + BUSY 拒绝盲重放 | 复审确认查-执行-写存在并发双副作用窗口 | 分布式锁（过度设计） |
| D15 | 12 个严格 schema 以最小校验器接入（$ref/$defs/anyOf/enum/长度/模式；不引第三方依赖）；schema 生效后 hold 必须显式 date_confidence+raw_pending（U16 来源完整度显式化） | 规格 additionalProperties:false 是真实契约；测试调用点已全部对齐 | jsonschema 库（引入依赖换完整 Draft2020 支持） |

| D15 | v2 分层写入走原 memory.hold 工具（新增可选参数），text 参数即事件正文（spec 的 event_text） | 单工具单语义；旧调用零破坏；字段名映射记录于此 | 独立 memory.hold.v2（拒绝：双入口分叉） |
| D16 | 'plan' 分类无独立桶期限：纯 plan 分类桶 status=plan_managed 不自动压缩；未分类桶 status=excluded（uncategorized）不自动压缩 | 规格 §7.1 未给 plan/未分类桶期限；保守方向=不遗忘，待现场确认后调整 | 默认 20 天（拒绝：编造业务数值） |
| D17 | 旧 v1 hold 的投影仍含 why_remember（祖父条款）；v2 行只索引事件正文 | R22 旧数据未明映射前不动旧行为；迁移映射落地后统一 | 立即重建全部投影（拒绝：改变旧桶检索行为） |
| D18 | 共同话语检测=字面同一句（规范化后相同）；"一方说另一方接住"的语义判定留给绑定评测阶段的匹配能力 | §5.1 明确接应判定需真实语义评测，不编造 | 模糊匹配（拒绝：误报保留线索更糟） |
| D19 | v2 审查闭环落 v2_review_items 新表而非扩 work_items 的 CHECK 约束 | SQLite ALTER 不支持改 CHECK；避免破坏性重建（§4）；v1 闭环继续可用 | 重建 work_items（拒绝） |
| D20 | review.submit(release) 在单事务内完成 ready_to_apply→生效（中间态入审计）；本地单进程无独立"系统执行器"凭据 | 林石见放行权本身含生效（R13/REV-04）；两阶段的核验语义完整保留在 _reverify | 双凭据执行器（待 MCP 常驻后引入） |
| D21 | 遗忘后的投影=summary_body+审查后 forget_tags（标题永不入索引） | §3.1 forget_tags 正式审查后参与；SEARCH-12/13 约束在测试钉住 | 仅 summary_body（tags 无法关键词命中） |
| D22 | 静态资源（backend/mariposa/web、apps/web/dist）按代码树定位，不随 MARIPOSA_ROOT | MARIPOSA_ROOT 是数据根；隔离测试根下无构建产物 | 复制 dist 到隔离根（拒绝） |
| D23 | v2 工具 schema 以代码内 V2_INPUT_SCHEMAS 为同源（优先于 v1.1 包 schema），后续导出 contracts v2 JSON | 包 schema 是 v1.1 历史契约；v2 字段直接改包会篡改历史 | 双文件手工同步（拒绝：漂移） |
