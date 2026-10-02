# Relation Followup（2026-10-01，legacy-removal 轮交付）

只记录，不修改（§5 冻结区）。本轮 Relation 零改动。

## 当前结构（未动）

- 模块：`backend/mariposa/memory/relations.py`；表：`memory_relations`
  （formal）、`i_revision_memory_relations`（I 修订链，formal）。
- 能力：memory.relations.link / detach / list / trace（Registry 全在，
  负向测试已回归 link+list+表可写读）。
- I 的 `relation` / `informed_by_revision` 数据链未触碰。

## 当前 relation types 与 resource types

- memory_relations.relation_type：continuation_of（trace 沿用）等
  现行闭集——见 schema CHECK；本轮未改枚举。
- 资源身份前缀（现行）：memory:<id>；I 经 i_revision_memory_relations
  独立表；Source 经 source 绑定层（memory_source_bindings），不复用
  relation 表。

## 退役模块留下的 legacy 关系面

- 生产库取证（2026-10-01 只读）：退役模块相关 relation 数据为空
  （letters/quotes/diary 等表 0 行，且无任何 relation target 指向
  这些资源的行——它们从未通过 relation 表建过关系）。
- `deletion/service.py` 的 DELETE_BLOCKED_BY_REFERENCES 检查
  i_revision_memory_relations + memory_source_bindings——现行语义，
  与退役模块无涉。
- 无新增 legacy relation type 需要登记。

## 歧义与下一轮议题（江乔生、林石见对齐用）

1. relation_type 闭集当前实际值域（schema CHECK vs 服务层白名单）
   是否仍符合双主人语义——本轮未审。
2. memory ↔ I 的表达是否统一走 i_revision_memory_relations，还是
   允许 relation 表承载跨资源类型——现状两者并存。
3. Source binding（range 级）与 relation（桶级）是否需要在"同一
   事实两个入口"上做一致性说明。
4. 上轮审计 F-47 提到 navigate 未复用 AllowedScope——属 Recall 轮，
  不在本轮。

以上均**未实施任何改动**，等待对齐结论。
