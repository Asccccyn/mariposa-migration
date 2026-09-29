# Mariposa v1.7 Codex 复审整改 · 交付报告（2026-09-29 晚）

范围：Codex 复审报告（`mariposa-v1.7-phase1-codex-reaudit-2026-09-29.md`）
全部 13 项 N 编号发现 + F07/F10/F11 残留 + 她当日两项裁定
（Recall commit-at-end 整改、Source 失败批次清场——前者已独立交付
`mariposa-recall-idempotency-crash-safety-2026-09-29.md`，本报告覆盖
其余全部）。

分支 `fix/v17-audit-20260929`，前序 HEAD `6df6dfe`（phase1 报告）。

## 基线与提交

```text
7c89d93 fix(recall): commit-at-end idempotency, derived budget, crash safety
        （她直接指令的 Recall 方案，独立报告已交付；N06 崩溃恢复由此闭合）
— 以下为本轮批次 —
d2a4b71…（批次1）fix(recall): drop dead OperationInProgress, purge
        unverifiable legacy operation rows          → N07/N08
4088bfd fix(audit): close N01/N02/N11/N12/N13
2d2018b fix(registry): real mutations regain write idempotency → N05
8c537b3 fix(source): hash-basis equivalence, conversation-scoped publish,
        failed-batch purge                           → N09/N10 + 清场裁定
c14d888 fix(recall): replay guard covers resource identity, words fields,
        switch, header                               → N03/N04
（批次1 的实际哈希以 git log 为准）
```

## 逐项矩阵

| 项 | 复审判定 | 修复 | 测试 | 状态 |
|---|---|---|---|---|
| N01 payload_hash 漏正文 | REGRESSION | `extras.update_text` hash 输入补 `event_text` | TestN01×2 | FIXED |
| N02 summary 重建 IndexError | REGRESSION | `field_projection` SELECT 补 `compressed_summary` | TestN02 | FIXED |
| N03 guard 白放/不重验 | STILL_BROKEN(P1) | guard 重写：资源身份从 resource_ref 解析、不可验证剔卡、rejected 剔除、分类变更剔卡、RECALL 禁用拒绝、packet 头（revision/budget）刷新 | TestReplayGuardN03N04×5 | FIXED |
| N04 words 重试丢卡 | REGRESSION | 字段名归一化 `our_words.text`→`our_words` 再比 AllowedFields | test_n04_words_replay_keeps_cards | FIXED |
| N05 mutation 失去幂等 | REGRESSION(P2) | 十个真实 mutation 能力改 `write=True` 重入 formal 原子幂等（含 proposals 别名）；派生类按复审分类保持现状 | TestN05×4 | FIXED |
| N06 副作用后异常/crash | PARTIAL | commit-at-end 方案整体闭合（`7c89d93`，报告独立） | A–I 九组 + 子进程 crash | FIXED |
| N07 IN_PROGRESS TypeError | REGRESSION | `OperationInProgress` 无引用且构造有缺陷，已删除；该路径不存在即不出 500 | 既有测试回归 | FIXED |
| N08 旧库 NULL hash/result | PARTIAL | migration 5 删不可验证旧行；`record_operation_row` 事务内接管空结果行 | migration-probe 语义测试随 commit-at-end 套件 | FIXED |
| N09 hash basis 不等价 | REGRESSION(P1) | `_row_content_hash` 的 content 不再对象化（与 `content_json: str` 严格同构） | test_n09_row_hash_roundtrip_equivalent（3 形态逐行相等） | FIXED |
| N10 跨 conv 错绑 | STILL_BROKEN(P1) | 发布门禁加 `provider_conversation_id` 匹配；已发布同内容不再误计 mismatch | test_n10_cross_conversation_uuid_not_relabelled | FIXED |
| N11 memory_tags 漏 FK | STILL_BROKEN | v1 标签入删除清理清单（桶自身附属，非跨域引用，不加 CASCADE） | TestN11 | FIXED |
| N12 双 ROLLBACK 掩盖 | REGRESSION | superseded CAS 输家改 flag 模式，事务提交后再抛结构化 409 | TestN12×2 | FIXED |
| N13 bind↔hold 双绑定 | PARTIAL | `raw.binding.bind` 查重移入 BEGIN IMMEDIATE，与 hold 共享锁内不变量 | TestN13（真实并发） | FIXED |

**Source 失败批次清场（她的产品裁定，选项 A 的实现路径）**：`_fail_batch`
在单事务内清理本批未发布消息/检索投影/快照成员/空壳会话；保留 raw 母本、
不可变版本档案（SL-07）、审计事件。修正文件重导从干净状态全新写入——
同 UUID 改写正文的场景下新正文直接可见，不存在"旧文占位"也不存在
"completed 却全文不可见"的 collision 状态（B.5 问题随之消解，未改动
首版展示规则）。A09/SL-10/resume 三个既有测试已按新语义改写，
`test_f11_failed_batch_purged_then_new_text_published` 钉住新行为。

## 测试证据

```text
pytest tests/unit -q                          456 passed, 1 skipped (24.35s)
pytest tests/acceptance tests/integration -q  122 passed (2.09s)
合计                                           578 passed / 1 skipped / 0 failed
```

新增：`test_v17_reaudit_fixes.py` 18 个（N01/N02/N05/N11/N12/N13 +
Source 批 3 + guard 组 5）；改写 A09/F11/SL-10/resume 至清场语义；
commit-at-end 套件 12 个见独立报告。并发项均为真实线程并发；crash 项
为真实子进程注入。前台分批执行，临时隔离根，无资源异常。

## Contract 状态（修正后的准确表述）

- 向后兼容新增：`memory.versions.read` 字段；结构化错误码
  `OPERATION_REPLAY_STALE` / `DELETE_BLOCKED_BY_REFERENCES` /
  `DELETION_ALREADY_DECIDED` / `IDEMPOTENCY_IN_PROGRESS(formal 侧沿用)`；
  runtime migration 4/5（DDL）。
- 纠正性修正（非新 breaking）：十个真实 mutation 能力的 MCP
  readOnlyHint 由错误 readOnly 改为 write=true（Codex B.3 认定的既有
  错标）；write=True 同时恢复了它们同 key 重试的幂等契约（baseline
  行为回归）。
- I/current 读取保持每次现算（F10 修复未回退，`test_reads_still_fresh_not_cached` 钉住）。

## 未做与边界

- B.6 非 FK 逻辑引用清单（provisional_sources、quotes、跨库
  work_items 等）：按复审自身归类为 non-blocking"需策略"，未纳入本轮。
- B.3 的 REPEATABLE_DERIVED 类（bootstrap 快照、words/semantic 索引
  重建、recall.status 惰性过期）：副作用可重建/单调，保持 fresh 执行，
  报告如实记载。
- Jev/embedding 第三方计费 exactly-once：按她的方案明确排除。
- F05/F06/F08/F09/F27–F30/F41（第二阶段）仍未开始。

## Codex 下轮复审入口

1. 清场事务的边界（`_purge_failed_batch_artifacts`）：空壳会话删除
   条件、与其他批次并发导入的隔离；
2. guard 的资源身份解析穷尽性（还有哪些 resource_ref 前缀在生产出现）；
3. migration 5 与旧 runtime 库升级路径；
4. 十能力 write=True 后 MCP/HTTP 双入口的幂等行为与 readOnlyHint；
5. N09 往返等价在真实旧数据上的抽验。
