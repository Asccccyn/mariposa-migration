# Relation / Deletion 语义收口报告（2026-10-01，规格 v2.0）

基线：`fd4176b`（RELATION_FOLLOWUP 代码核证版）。本轮 HEAD：见
`git log -1`（本 commit）。生产环境：**零接触**（未部署/未迁移生产库）。

## 1. 数据与事务模型（WP1）

- **关系实例身份**：四域各补稳定 ID（memory_relations.relation_id /
  i_revision_memory_relations.relation_id / plan_memory_links.link_id /
  source binding_id 原有）；同端点同类型业务唯一约束保留。
- **纠错历史**：新表 `relation_corrections`（五域 CHECK；原因固定
  binding_error；legacy 未知事实保留"未知"不补造）。
- **软删退役**：memory_relations.active/version/confidence、
  source 绑定 revoked 置信度全部移除（active 表只存有效关系）。
- **formal migration 26**：五域表重建+数据迁移（active 行保 ID 迁移、
  inactive/revoked 行迁纠错历史标 legacy 未知）、deletion_requests
  重建为 Memory-only、**memories.visibility CHECK 去 archived**（fresh
  基础 DDL 同步；已部署库 CHECK 字符串保留但写路径全退役——母表
  重建与全库 FK 网冲突，风险>收益，如实申报该偏差）。
- **领域原子写**：`relations.corrections.atomic_write`——同事务
  查完成记录→业务→回执→提交（复用 idempotency_records completed
  行；无进行中队列、不写 Recall 运行库）。

## 2. 关系合同（WP2）

- **纠错（correct）五域**：`memory.relations.correct` /
  `i.item.relations.correct`（仅 jiaming，I 正文不改动）/
  `source.binding.correct`（不碰原文母本；改绑走完整区间校验）/
  `plan.memory.correct`（最后一条错误链接可纠；桶留 plan 分类时
  阶段读 GAP 不伪装） / `memory.our_words.source.correct`（预期
  来源指纹并发保护；新来源必须 source_msg: 现行身份；旧 raw 前缀
  写入拒绝）。correction_action 仅 remove/replace_wrong_binding。
- **反查路由**：`relations.list`（五域 in/out/both，另一端定位可
  原样续查；reversed 标注不改写方向） / `relations.trace`
  （continuation_of 全分支双向、环有界、披露截断）/
  `relations.corrections.list`（历史不混入有效关系）。
- **词表收口**：I 侧 related→related_to（迁移+写入双路径）。
- **退役合同**：memory.relations.detach、source.binding.revoke、
  memory.deletion.restore（无 alias/fallback）。

## 3. Deletion 重写（WP2/§7）

- 人类路径：`memory.deletion.request`（**仅 qiaosheng**；理由必填；
  5 次/桶一生 + 10 次/上海自然日申请口径；同桶单 pending；同 key
  幂等重放返回同一申请不重复计数——回执同事务落库）。
- 决定：`memory.deletion.decide`（仅 jiaming；reject 必填理由；
  approve 无理由要求；关系硬门拦截→整单回滚保 pending，不落
  rejected）。withdraw 仅本人 pending；撤回不返还次数。
- 直删：`memory.delete`（仅认证 jiaming；无申请/理由/配额/审批
  审计；技术幂等回执仅存目标身份+完成码；pending 置 superseded）。
- **五域硬门**（两条路径共享，写事务内）：桶间入/出边、任意 I
  修订、Source 区间绑定、Plan 成员（含终态）、our_words 非空来源
  引用（含不可解析旧引用——需要纠错，不放行）。
- 删除执行：子记录+派生索引同事务；**不再级联删关系**（旧
  DELETE FROM memory_relations/plan_memory_links 业务路径已移除）；
  申请/纠错历史不随桶删（无 live FK）。
- `memory.deletion.get` 移入正式注册层；list 支持 memory_id 过滤。

## 4. Memory archive 退役（WP3）

- deletion/service archive 分支、action 参数、归档提示删除；
  schema CHECK（fresh）；migration.py 旧桶自动 archived 应用分支
  （type=archived 与 archive/ 目录证据双路均改 out_of_scope，
  数据处置待用户裁定）；Source 母本/备份/I 历史合法同名保留。

## 5. Registry 终态

152 项（+10 新：五域 correct×5 + memory.delete + relations.list/
trace/corrections.list + deletion.get 正式化；-3：detach/revoke/
restore）。MCP/HTTP/schema 同步（严格 schema：operation_id 必填、
correction_action 枚举、action 字段结构性拒绝）。

## 6. 测试（WP4）

- 验收矩阵：`tests/unit/test_relation_deletion_acceptance.py`
  22 节点（C/R/Q/D 组核心）+ 专项文件（source 纠错/trace 方向/
  并发 decide/迁移升级）；详见 `RELATION_DELETION_ACCEPTANCE.md`。
- 全量：**569 unit（568+1skip）+ 100 acc/int = 668 passed / 0 failed**
  （collect 口径互斥分批；真实模型 0 调用——smoke 未运行）。
- 迁移升级路径实测：v25 隔离库含 legacy 软删/revoked → migration 26
  迁移正确+重复迁移幂等。

## 7. 遗留（如实申报）

1. **T03/T04 跨域竞态线程矩阵**（approve×直删、五域建边×删除）——
   下批；
2. **R07 words 纠错的公开路径断言**（service 层已实现+冒烟）；
3. words 侧消费（指纹失效后的 dense 缓存清理）依赖既有指纹机制，
   本轮未改动检索政策（§13）；
4. F-01..F-49 核心 findings 未处理（§13 明确留下轮）；
5. 已部署库 memories CHECK 字符串保留 archived（见 §1 偏差申报）。
