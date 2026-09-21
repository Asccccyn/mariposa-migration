# 继承与重建清单

## 已重建（mariposa 新代码，行为规格取自 01 工程文档）

- 记忆状态流、版本链、遗忘审批、恢复：`backend/mariposa/memory/`
- 工作区/提案/跨库信封协议：`backend/mariposa/workspace/`
- 检索投影 + FTS5 中文预分词：`backend/mariposa/retrieval/`
- 身份/绑定/能力注册表：`backend/mariposa/identity/`、`capabilities/`

## 以特征测试形式继承（旧行为规格，不复制代码）

- letters/deletion：见 `legacy_behavior_matrix.md`；mariposa 适配实现时先写 synthetic
  fixture 测试再接 provider（Phase 5），当前状态 `blocked: legacy_contract_partially_verified`
  （submit/withdraw/decide/限额/测试豁免已核验；锁状态机与 MCP 侧语义待核验）。

## 不迁移

- dream/见证机制（历史文件标 legacy archive，不消费）
- 旧 audit/账本作为新操作重放（只读历史）

## 待定（Phase 5 迁移演练时逐项）

- 旧 bucket Markdown → 新 memories 的结构映射与 ID 保留策略
- embeddings 重建（旧向量不迁移，按新投影重算）
- `_media/` 对象复制与 hash 校验
