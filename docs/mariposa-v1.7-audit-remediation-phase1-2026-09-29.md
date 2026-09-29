# Mariposa v1.7 审计整改 · 第一阶段交付报告（2026-09-29）

整改范围：`mariposa-full-code-audit-2026-09-29.md`（v1.7 重新裁定版）中的
F04 / F07 / F10 / F11 / F13 / F14 / F26 / F34 / F38 / F39 十项。

说明：指令所列 `mariposa-full-code-audit-2026-09-29 2.md`（带空格 "2" 的
副本）在磁盘上不存在；本次以同目录 23:12 定稿的
`mariposa-full-code-audit-2026-09-29.md`（与 reclassification 同步更新的
当前有效版）+ `mariposa-audit-reclassification-v1.7-2026-09-29.md` +
`mariposa-audit-evidence-2026-09-29.md` 为审计基线。

---

## A. 基线

```text
整改前 HEAD      b2992733ee820d99d97638ac4382287168da5b02 (main)
整改分支         fix/v17-audit-20260929
baseline commit  b0482a7（审计现场快照：13 tracked 修改 + 4 untracked
                 真实源码/测试；排除 .venv-old-20260929/ 与
                 "D:\mariposa\runtime\models/"，二者保留在工作区未提交）
整改后 HEAD      ba53a1098f7c59545d088bb4b8fb570b15667093
git status       干净（除上述两个故意排除的 untracked 目录）
```

提交序列（每 commit 可独立说明修复项、未变语义、对应 F 编号）：

```text
b0482a7 chore(audit): baseline for v1.7 phase-one remediation
3087fef fix(memory): unify current revision body and projection entry (F04/F13/F14)
0d8f07d fix(idempotency): atomic claim, stale replay revalidation, no read cache (F07/F10/F26)
364b670 fix(source): enforce published provenance consistency (F11)
fdd2c2f fix(memory): atomic deletion decision CAS + reference integrity guard (F34/F38)
ba53a10 fix(memory): optimistic concurrency for updates and same-source holds (F39)
```

---

## B. 逐项矩阵

### F04 — 只改元数据/meaning/重建索引会让 v2 正文从检索消失 — **FIXED**

- 原问题：v2 写入把正文存 `event_text`（`hold_text=NULL`），但
  `extras.update_text` 投影、`listing._rebuild_full_projection`、
  `retrieval/rebuild.py` 全库重建只读 `hold_text`，v2 正文索引化为空。
- 根因：正文来源没有单一解析入口，五条路径各自决定字段。
- 修改文件：`memory/service.py`、`memory/extras.py`、`memory/listing.py`、
  `retrieval/rebuild.py`、`retrieval/field_projection.py`。
- 修改函数/模块：新增 `memory.service.version_body`（唯一正文解析：full
  表示 event_text 优先、v1 旧数据回退 hold_text；forgotten_summary 只认
  compressed_summary）与 `memory.service.rebuild_full_projection`（唯一
  full 投影构造：正文+why+meaning 层）；`get`、`update_text`、
  `meanings_append/replace`、`rebuild_index`、`field_projection._current_texts`
  全部改走上述入口。v1 兼容 fallback 集中在 `version_body` 一处，
  业务层不再散落。
- 解决方式：见上；v2 版本行跨 update 保持 `hold_text=NULL` 形态不漂移
  （延续行形态：v2 行 hold 列保持 NULL，v1 行继续 hold 列承载）。
- 新增测试：`tests/unit/test_v17_audit_p1_memory.py`
  TestF04UnifiedBody（6 个：只改 why 后检索、改正文后新旧词、meaning
  追加、meaning 替换、全库重建、两字段并存不误取旧字段）+
  TestLegacyCompat（2 个：v1 只有 hold_text 的读取/检索/update/重建）。
- 测试结果：全绿（见 §D）。
- 完全修复：FIXED。

### F07 — Recall operation_id 缓存旧正文且重放不校验 — **FIXED**

- 原问题：runtime 幂等表保存完整含正文 packet，同 operation_id 重放直接
  返回旧正文；绕过版本、可见性、phase、session 有效性；表无有界清理。
- 根因：`recall/store.claim_operation` 把"写幂等"与"响应正文缓存"合在
  一起，重放路径无任何当前状态校验。
- 修改文件：`recall/store.py`、`recall/service.py`、
  `capabilities/registry.py`（`_with_operation_id`）、`errors.py`。
- 修改函数：`claim_operation`（重写，见 F26）；新增
  `recall.service.revalidate_replayed`：session 必须 ACTIVE 族
  （EXPIRED/CLOSED → `OPERATION_REPLAY_STALE` 409）；逐卡重校验 memory
  当前 visibility、卡片 content_version == 当前 revision、并按**当前
  phase** 的 AllowedFields 重过滤 matched_fields/evidence——WIDE 收缩到
  CORE 后 title-only 命中、版本已前进的候选被剔除而不是原样送出；
  `purge_expired` 对 `recall_operation_keys` 按 2× session TTL 有界清理。
- 新增测试：`tests/unit/test_v17_audit_p1_idempotency.py`
  TestF07StaleReplay（4 个：WIDE→CORE 重放剔除 title 命中【v1.7 证据
  场景复现】、revision 前进剔除、session 过期拒绝、归档剔除）。
- 测试结果：全绿。
- 完全修复：FIXED（保存的仍是完整 packet，但出站必须过当前状态重校验；
  完整"只存 receipt 不存正文"的重构属 Recall 第二阶段范围，本轮以
  重校验闸门达成同一不变量：旧 operation 不得绕过 current-state 判断）。

### F10 — 通用 Idempotency-Key 使 I current 读取重放旧版本 — **FIXED**

- 原问题：读取能力（i.item.get/i.get/bootstrap.get 等）也被卷进 formal
  幂等响应缓存，I 修订/rollback 后同 key 重放旧 current 正文。
- 根因：`registry.invoke` 只按"是否 recall runtime 能力"分流，未区分
  写幂等与读缓存。
- 修改文件：`capabilities/registry.py`（`invoke`）。
- 修改函数：幂等分支加 `and cap.write`——读取（write=False）不做长期
  幂等响应缓存，携带 key 的读请求忽略 key、每次按当前状态执行。
  已核对全部 write=False 能力均为纯读（recall.status 的惰性过期在
  runtime 例外集内，不受影响）；写能力幂等行为不变。
- 新增测试：TestF10ReadNoResponseCache（2 个：i.item.get 同 key 跨
  revision 读到新版；memory.update 写幂等重放保持单版本不双写）。
- 测试结果：全绿。
- 完全修复：FIXED。

### F11 — 失败批次旧正文被成功批次重新发布且 provenance 改指新母本 — **FIXED**

- 原问题：Batch A 失败后遗留的同 UUID 未发布旧行，在 Batch B 成功时被
  `published=1 + import_batch_id=B` 接管，但旧行正文是 A 的、B 的 raw
  归档不含该正文——provenance 造假。
- 根因：发布 UPDATE 只按 UUID 集合发布，未比对内容身份；A13 回填直接
  把本批 hash 贴给旧行。
- 修改文件：`source/importer.py`。
- 修改函数：发布门禁改为按内容身份逐条发布（行 `content_hash` == 本批
  快照成员 hash 才发布并前移 import_batch_id，mismatch 行保持
  unpublished 且来源批次不动；跳过数入 stats
  `publish_skipped_content_mismatch` 与 audit payload）；A13 回填改为
  `_row_content_hash`（从行自身列重算身份）而非贴本批 hash；快照成员
  对同文件内重复 UUID 首见定格（原 INSERT OR REPLACE 会覆盖为最后
  内容、与"行保留首个"的 SL-07 语义脱节）。
- 新增/改写测试：`test_source_audit_fixes.py`——
  `test_a09_failed_batch_uuid_taken_over_same_content`（改写：同正文
  接管仍成立 + published 行 hash == 成功批快照成员 hash 可反查）、
  `test_f11_failed_batch_old_text_not_republished`（新增：Batch A fail +
  Batch B success 同 UUID 异正文 → 旧行不发布、batch 不迁移、新文本
  留不可变版本、检索面两版都不可见）。原接受旧行为的断言（38-51 行）
  已按新语义移除。
- 测试结果：全绿（含 source 全套 108 passed）。
- 完全修复：FIXED（同 UUID 同正文幂等、SL-07 不可变版本、SL-10 失败
  隔离、F19"首版展示"语义全部保持）。

### F13 — maintenance.rebuild_index 部分提交后抛 NameError — **FIXED**

- 原问题：正式事务 COMMIT 后引用未定义 `out`，source 索引重建不可达，
  维护命令永远失败且失败响应掩盖已发生的索引变更。
- 根因：返回对象在首次使用前未定义。
- 修改文件：`retrieval/rebuild.py`（`rebuild_index`）。
- 修改函数：`out` 前置定义；full 分支统一走
  `memory_service.rebuild_full_projection`；field/words/source 各步骤
  统计汇入 `out` 并完整返回。
- 新增测试：`test_v2_full_rebuild_keeps_body_searchable`（返回含
  field_projection/words_index/source_projection 且不再抛错）。
- 测试结果：全绿。
- 完全修复：FIXED。

### F14 — memory.versions.read 对 v2 历史省略事件正文 — **FIXED**

- 原问题：历史接口 SELECT 不含 event_text，v2 版本行返回
  hold_text=NULL，版本列表"有版本无正文"。
- 根因：历史序列化与正文存储形态脱节。
- 修改文件：`memory/service.py`（`versions_read`）。
- 修改函数：SELECT 增加 event_text/original_title/schema_version；每行
  经 `version_body` 解析出 `text` 字段——每个 revision 的 text 准确
  反映对应版本；原始列保留供审计对照（向后兼容：仅新增字段）。
- 新增测试：TestF14VersionsRead（2 个：v2 两版本各自 text 正确且
  hold_text 原始列为 NULL；v1 旧数据回退 hold_text）。
- 测试结果：全绿。
- 完全修复：FIXED。

### F26 — runtime 操作幂等先执行后登记 — **FIXED**

- 原问题：两个同 operation_id 并发请求都通过"不存在检查"各自执行
  副作用，最后 INSERT OR IGNORE 只挡登记；crash 在执行后登记前留下
  无记录副作用；异 payload 同 key 也直接回放旧结果。
- 根因：无原子认领，无 payload 身份。
- 修改文件：`recall/store.py`（`claim_operation`）、`schema.py`
  （RUNTIME_MIGRATIONS 新增 (3)）、`capabilities/registry.py`
  （`_with_operation_id` 传 payload_hash 与 replay_guard）、`errors.py`。
- 修改函数：`claim_operation` 重写为原子认领模式（与 formal 侧
  `_claim_idempotency` 同型）：BEGIN IMMEDIATE 内 INSERT
  status='running' 占位，占位成功者才执行 builder；并发另一方有界
  等待终态后重放（经 F07 guard）；builder 异常 → 标 failed，同 key
  可安全重试；同 key 异 payload → `IDEMPOTENCY_CONFLICT`；仍 running
  超时 → `OperationInProgress`（IDEMPOTENCY_IN_PROGRESS 409）。
  runtime migration (3) 为 `recall_operation_keys` 补
  status/payload_hash/updated_at 列（旧行默认 completed 兼容；
  fresh 库同样经 migration 3 补列，避免 DDL 双处定义漂移）。
- 新增测试：TestF26AtomicClaim（4 个：**真实线程并发**同 key 单次
  执行 + 恰一方重放、failed 安全重试、同 key 异 payload 冲突、顺序
  重试语义保持）。
- 测试结果：全绿。
- 完全修复：FIXED。

### F34 — 已获批删除遇到 I/source 绑定时外键失败 — **FIXED**

- 原问题：物理删除清单未纳入 `i_revision_memory_relations` /
  `memory_source_bindings`，approve 时 FK 裸 500、事务回滚。
- 根因：跨域 schema 演化未同步删除调用链，且无引用完整性策略。
- 修改文件：`letters/service.py`（`_execute`、新增
  `_blocking_references`）、`errors.py`。
- 修改函数：删除前在写事务内检查两类引用；存在则抛 `DeleteBlocked`
  （DELETE_BLOCKED_BY_REFERENCES 409，detail 携带各引用类型行数），
  memory、relation、pending 申请全部保留（整体回滚）。未加任何
  CASCADE；无引用 memory 物理删除行为不变；archive 路径不受引用
  阻止（正文保留仅归档）。
- 新增测试：`tests/unit/test_v17_audit_p1_deletion.py`
  TestF34ReferentialIntegrity（4 个：I 关系阻止 + 结构化 409 + 关系
  不误删 + 申请回滚 pending、source 绑定阻止、两类引用同时全部列出、
  archive 不受影响）。
- 测试结果：全绿。
- 完全修复：FIXED。

### F38 — 删除审批竞态可把已执行删除的申请改成 rejected — **FIXED**

- 原问题：decide 入口读 pending 后进事务，UPDATE 无 status 守卫——
  并发 approve/reject 可双双成功，最终状态可能为 rejected 而物理删除
  已发生；superseded 路径同样无守卫。
- 根因：状态机迁移非原子，无 CAS。
- 修改文件：`letters/service.py`（`deletion_decide`）。
- 修改函数：状态迁移改为写事务内条件更新
  `UPDATE ... WHERE id=? AND status='pending'`，rowcount!=1 →
  `AlreadyDecided`（DELETION_ALREADY_DECIDED 409）；CAS 落定后
  approve 的 `_execute` 与状态更新同事务提交——数据库状态与实际
  副作用一致（approved 必真删、rejected 必保留）；superseded 迁移
  同样加守卫。
- 新增测试：TestF38ApproveRejectConcurrency（3 个：**真实线程并发**
  approve vs reject 一胜一负 + 状态与副作用一致、决定后不可反转
  （NOT_FOUND）、无引用 approve 真删除）。
- 测试结果：全绿。
- 完全修复：FIXED。

### F39 — memory 并发编辑裸 IntegrityError / 同源并发 hold 双写 — **FIXED**

- 原问题：同 expected_version 并发编辑一方裸 IntegrityError；相同
  raw source 的并发 hold 在彼此 commit 前都通过去重检查，写出两个
  逻辑 memory。
- 根因：version 检查与同源去重都在写事务外（check-then-act 竞态）。
- 修改文件：`memory/extras.py`（`update_text`，3087fef）、
  `memory/service.py`（`hold`、`_duplicated_by_raw_ref`，ba53a10）。
- 修改函数：`update_text` 的 expected_version 检查移入 BEGIN
  IMMEDIATE 写锁内——并发编辑一方成功（revision+1）、另一方结构化
  `VersionConflict`（409，含 expected/current），版本链保持 1→2 连续；
  `hold` 的同源去重移入写事务（同连接、写锁内）——并发同源 hold
  只落一个 memory，另一方返回同 memory_id + deduplicated=true。
  唯一性依据正式来源身份（raw ref 的 source_hash）。
  未加部分唯一索引：memory_raw_refs 的 INSERT OR REPLACE 语义与
  UNIQUE(source_hash) 部分索引冲突，单写者 SQLite 的写锁内检查即为
  正确性边界（理由已写入 commit message）。
- 新增测试：`tests/unit/test_v17_audit_p1_concurrency.py`（4 个：
  **真实线程并发**同 revision 编辑一胜一负 + 版本链连续、结构化
  409 断言、**真实并发**同源 hold 单 memory、顺序去重保持）。
- 测试结果：全绿。
- 完全修复：FIXED。

---

## C. 未修改确认

以下项目本轮**没有**被重新实现或恢复（reclassification 撤回项与
v1.7 已验证语义全部保持原样）：

```text
F01 F02 F03 F16 F19 F25 F35 F37   （撤回项：未按旧审计改动任何行为）
Recall ranking                     （未动排序方案）
BM25                               （未新增、未移除）
Jev architecture                   （未重写；仅 baseline 吸收既有 cache.py）
654 / max H / permanent WIDE / 明开回温 / keep / plan phase
                                   （全部现算语义原样；相关测试全绿，
                                    见 §D）
WIDE/MID/CORE 字段矩阵             （phase_policy 未改动一行；
                                    F07 重放校验只是调用它）
Source Layer 产品语义              （首版展示、SL-07 不可变版本、
                                    SL-10 失败隔离、A09 同正文接管均保持）
Recall 主链 / 第二阶段项           （F05/F06/F08/F09/F27/F28/F29/F30/F41
                                    未开始）
遗忘规则 / phase 天数 / 自动遗忘 / 摘要压缩 / 20天到期
                                   （不存在也未引入）
```

---

## D. 测试证据

执行环境：`.venv/bin/python -m pytest`，前台运行、显式超时、临时隔离
测试根（conftest fail-closed 保险丝生效），遵守 AGENTS.md 测试硬约束
（分批、无后台悬挂）。单批最长 22.3s，无内存异常。

```text
命令                                                     结果
--------------------------------------------------------------------
pytest tests/unit -q                                    426 passed, 1 skipped (22.32s)
pytest tests/acceptance -q                               89 passed (1.37s)
pytest tests/integration -q                              33 passed (0.59s)
--------------------------------------------------------------------
合计（549 collected）                                548 passed, 1 skipped

v1.7 核心语义回归（点名重跑）：
pytest tests/unit/test_phase_policy.py
       tests/unit/test_phase34.py
       tests/acceptance/test_recall_v14.py
       tests/unit/test_v17_pipeline.py
       tests/unit/test_recall_judge.py -q              145 passed (0.89s)
pytest tests/unit/test_v2_layers.py                     87 passed (0.30s)
       tests/unit/test_phase_policy.py                        （含 654/明开回温/
       tests/unit/test_v2_bootstrap.py -q                      keep/plan phase 载荷）
--------------------------------------------------------------------
新增第一阶段 regression（本轮新增 31 个 + 改写 2 个）：
test_v17_audit_p1_memory.py                             10 passed
test_v17_audit_p1_idempotency.py                        10 passed
test_v17_audit_p1_deletion.py                            7 passed
test_v17_audit_p1_concurrency.py                         4 passed
test_source_audit_fixes.py（改写后）                     5 passed
--------------------------------------------------------------------
失败：0；资源使用异常：无。
```

并发测试为真实线程并发（ThreadPoolExecutor + Barrier 同步进入），
非顺序重复调用。

---

## E. Diff 摘要

相对整改前 HEAD（b299273 → ba53a10）：30 files changed,
3061 insertions(+), 293 deletions(-)（其中 baseline commit b0482a7 占
约一半，系审计现场既有修改的快照）。

```text
新增文件（整改 commit 部分）：
  tests/unit/test_v17_audit_p1_memory.py        F04/F13/F14 回归
  tests/unit/test_v17_audit_p1_idempotency.py   F07/F10/F26 回归
  tests/unit/test_v17_audit_p1_deletion.py      F34/F38 回归
  tests/unit/test_v17_audit_p1_concurrency.py   F39 回归
（baseline 带入的新文件：backend/mariposa/retrieval/judges/cache.py、
  docs/I_REVISION_MODEL_20260929.md、test_i_item_history.py、
  test_typesafe_jev_cache.py——审计时已在现场的既有实现）

修改文件（整改 commit 部分）：
  backend/mariposa/memory/service.py        version_body / 投影入口 / hold 锁内去重
  backend/mariposa/memory/extras.py         update_text 锁内版本检查 + 统一入口
  backend/mariposa/memory/listing.py        meaning 重建委托
  backend/mariposa/retrieval/rebuild.py     F13 + 统一重建
  backend/mariposa/retrieval/field_projection.py  正文解析走统一入口
  backend/mariposa/recall/store.py          claim_operation 重写 / purge 有界
  backend/mariposa/recall/service.py        revalidate_replayed
  backend/mariposa/capabilities/registry.py invoke 读不缓存 / _with_operation_id
  backend/mariposa/source/importer.py       发布门禁 / A13 回填 / 快照首见定格
  backend/mariposa/letters/service.py       deletion CAS / 引用守卫
  backend/mariposa/errors.py                4 个新结构化错误
  backend/mariposa/schema.py                RUNTIME_MIGRATIONS (3)
  tests/unit/test_source_audit_fixes.py     A09 改写 + F11 新增

删除文件：无。

schema/migration：runtime 库 migration (3)——recall_operation_keys
新增 status/payload_hash/updated_at 三列（ALTER，旧行默认 completed
兼容；不触正式库 schema，不删任何数据）。

公共 API / MCP contract：
  No public contract change.（能力清单、参数 schema、既有响应字段均未
  改动。向后兼容的新增：memory.versions.read 响应每行新增
  event_text/original_title/schema_version/text 字段；新增结构化错误码
  OPERATION_REPLAY_STALE / DELETE_BLOCKED_REFERENCES /
  DELETION_ALREADY_DECIDED / IDEMPOTENCY_IN_PROGRESS(runtime)——这些
  场景此前为裸 500 或错误行为，新错误码仅为新增错误情形，非契约变更。）
```

---

## F. Codex Re-audit Scope（下一轮复审入口）

只审本轮十项及其 regression，不需要重新解释 v1.7 产品语义：

1. **F04/F13/F14**：`memory/service.py::version_body`、
   `rebuild_full_projection` 及其五个调用点是否仍有绕过统一入口、
   自行决定正文来源的路径（含 recall evidence 的 whitelist_body 读取）；
   v1 旧数据（无 event_text）读取兼容是否完整。
2. **F07**：`recall/service.py::revalidate_replayed` 的逐卡校验是否
   覆盖 words 通道卡（有 memory_id 的）与 raw 卡；session 终态集合是否
   与 ACTIVE_STATUSES 一致；保存 packet 中 candidates 为空的响应是否
   有泄露面。
3. **F10**：`registry.invoke` 的 `cap.write` 分流——是否存在 write=False
   但实际有副作用、因而丢失幂等保护的能力（本轮已核对，建议复核）。
4. **F26**：`claim_operation` 并发时序——占位失败方等待循环的 4s 上限、
   failed 重入、payload_hash 冲突分支；migration (3) 对已有 runtime 库
   的升级路径。
5. **F11**：`_import_staged` 发布门禁逐条 UPDATE 的正确性；同文件内
   重复 UUID 首见定格后 `sequence_conflicts` 计数是否仍准确；
   `_row_content_hash` 与 `_content_hash` 的 basis 是否严格同构
   （attachments/content_json 往返）。
6. **F34**：`_blocking_references` 引用集合是否完整（还有没有其他
   FK→memories 的正式跨域表）；archive 语义未被引用阻止是否符合预期。
7. **F38**：CAS 与 `_execute` 同事务的原子性；并发输家两种结构化拒绝
   （AlreadyDecided / NotFound）的对外一致性。
8. **F39**：写锁内版本检查与去重的窗口是否闭合（单写者 SQLite 前提下）；
   未加部分唯一索引的权衡（commit ba53a10 message 有理由）是否接受。
9. **回归保护**：v1.7 phase 语义测试（§D 点名清单）是否仍然全绿。
10. **测试真实性**：并发测试是否真并发（Barrier 同步）、失败路径
   （builder 异常、CAS 失败、guard 拒绝）是否都有断言。

---

## 结论

十项全部 FIXED，548 passed / 1 skipped / 0 failed，v1.7 核心语义
（654、max H、permanent WIDE、明开回温、keep、plan phase、
WIDE/MID/CORE、Source 首版展示）逐项点名回归全绿。未修改任何
撤回项（F01/F02/F03/F16/F19/F25/F35/F37）对应行为，未开始第二阶段
事项，公共契约无变更。
