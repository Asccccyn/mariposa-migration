# 旧 Ombre 行为矩阵（本地 main2.5 v2.17.11 源码核验）

> 依据：`D:\Ombre-Brain-main2.5\src\web\letters.py`、`src/deletion_requests.py`（只读源码）。
> 状态：【已核验】源码确认；【待核验】尚未读源或未跑特征测试。

## Letters

| 行为 | 状态 | 本地实际行为 |
|---|---|---|
| 列表 `GET /api/letters` | 已核验 | metadata-only：author 过滤（user / ai+claude+ai_name 别名 / 自定义署名原样）；按 `letter_date` 或 `created` 倒序；过期锁经 `normalize_expired_lock` 归一化后返回 |
| 写信 `POST /api/letter` | 已核验 | `lock_type` + `unlock_date`（经 `normalize_unlock_date(lock_type, …)` 归一）；作者署名归一（user→user；ai/claude/ai_name→ai_name；其它原样） |
| 编辑 `PATCH /api/letter/{id}` | 已核验 | lock 字段单独处理（`lock_type`/`unlock_date`），更新走 `letter_lock_revision` |
| 删除 `DELETE /api/letter/{id}?confirm=true` | 已核验 | **不是物理删除**：提交 `deletion_requests.submit(letter_id, reason, is_letter=True)`；`confirm` 缺失 → 400 |
| 锁状态 | 部分核验 | `letter_lock_state` / `letter_lock_revision` / `normalize_lock_type` / `normalize_unlock_date` / `normalize_expired_lock`；完整状态机【待核验】 |
| 列表可见字段全集 | 待核验 | `safe_letter_metadata` 未逐字段读 |

## Deletion（DeletionRequestStore + HumanDeleteExecutor）

| 行为 | 状态 | 本地实际行为 |
|---|---|---|
| submit | 已核验 | `action ∈ {archive, delete}`；reason 必填（空 → `reason_required`）；同桶已有 pending → `pending_exists`(409)；`DAILY_LIMIT=10`/天（全库计数）、`LIFETIME_LIMIT=5`/桶；持久化为 JSON（`_filesystem_turn` 文件锁） |
| 测试桶豁免 | 已核验 | `is_test_bucket(bucket)` 直接执行删除并标 `exempt_test_data: true` —— 即 01 文档所指“仅针对显式测试 provenance 的开发者擦除入口”；mariposa 不经普通业务 MCP 暴露 |
| withdraw | 已核验 | 撤回该桶最近一条 pending，写 `decided_at` |
| decide | 已核验 | `decision ∈ {approve, reject}`，可带 `ai_reason`、`expected_bucket_id` 乐观校验（不符 → `bucket_mismatch`）；approve → 执行器；目标已非 active → `superseded` |
| 执行器 archive | 已核验 | `bucket_mgr.archive()` 归档 |
| 执行器 delete | 已核验 | `bucket_mgr.delete()`（物理删 Markdown）+ letter 场景清理 embedding/bm25/outbox 派生层；letter 历史行为：Markdown 已消失也修复派生层（注释明确 web/human 与 MCP/AI 删除语义保持区分） |
| 批量 submit_batch | 已核验 | 同一限额逻辑的批处理 |
| 谁审批 | 已核验（结构） | `ai_reason` 字段与调用点表明决定方为 AI 侧（周家明）；MCP 侧入口【待核验】 |
| 恢复（restore deleted） | 待核验 | 未在已读源码段确认 |

## 对 mariposa 的直接约束

1. 公开契约命名 `memory.deletion.*` / `letter.*`；`delete` 一词不承诺抹除字节。
2. 迁移期不得把旧 pending/approved/rejected 记录当新操作重放；作为只读历史归档。
3. 测试豁免通道不得通过 mariposa 普通 MCP 暴露。
4. UI 语义标签 = 实际行为（申请删除→审批→物理删/归档）。
