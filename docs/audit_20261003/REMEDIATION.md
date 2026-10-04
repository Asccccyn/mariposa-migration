# Remediation / Acceptance 报告 · 2026-10-04

依据：`REPORT.md`（周家明全量审计 @b484908）+ 江乔生 2026-10-04
待裁定项最终口径。本文件是本轮修复与验收的统一交付记录。

## 1. 裁定落地（五项合同）

### 1.1 request_ref 幂等 + 线性接续（P0）

- `request_ref` = 一次具体请求的幂等身份：未给 `operation_id` 时由
  request_ref 派生（`reqref:` 前缀进入 runtime 幂等空间）。同 ref 同
  payload 网络重试重放原结果（start 回原 session、不重新领预算）；
  同 ref 异 payload → `REF_REUSE_MISMATCH`。
- `continue_request_ref` 只表示接续点：runtime migration 11 新表
  `recall_continue_refs`（PK(session_id, ref)），burst 授予与消费
  同事务——旧 ref 再申领判 `StaleOperation`，回滚时消费一并撤销。
- 预算仍按 session 内成功轮次派生 COUNT（事务内重数，硬门不受
  预览影响）。
- schema：`memory.recall.start/refine` 的 `operation_id` 改为与
  `request_ref` 二选一（`required_oneof`，兼容 query_plan 内既有
  位置）。
- 回归：`tests/unit/test_ruling_request_ref.py`（5 例：重放回原
  session / 异 payload 拒绝 / 重放不领新额度 / 旧 continue_ref
  stale+新 ref 可续 / 同 ref 重试不双耗）。

### 1.2 provenance 来源收紧（P0，优先项）

- 写侧（hold 内联与 words.append 两条路径共用
  `require_resolvable_sources`）：`source_msg:<id>` 必须解析到当前
  **已发布**消息；`raw_msg:` 无正式 raw registry，新写一律拒收
  （`INVALID_SOURCE_REF`）。
- 删除硬门 `word_sources` 只数可解析到已发布消息的引用——dangling
  不再阻止删除。
- 读侧 gap 细分：legacy raw 前缀标 `legacy_raw_prefix`、不可解析标
  `invalid_or_missing`；均不签 `word_verbatim`。
- 回归：`tests/unit/test_ruling_provenance.py`（7 例）。

### 1.3 EXPLICIT_REJECT_AFTER_DELIVERY 的 delivery（P1）

- 定义 = 候选真实进入过模型侧可见的出站交付包：交付回执
  （runtime migration 12 给 `recall_receipts` 补 `revision` 列）。
- gate 收紧为 `rejected ∩ delivered_refs`——内部 seen、检索池、
  judge 看过都不算；不限定当前 revision（可拒绝前一轮真实交付
  过的候选），但必须能证明在哪一轮出站。
- 判定抽为 `_reason_fact_supported` 供 round2 与测试共用。
- 回归：`tests/unit/test_ruling_delivery_receipt.py`（2 例）。

### 1.4 words scoped BM25（P0，优先项）

- S05/S06 覆盖 words：`words_search` 弃用 FTS5 `bm25()` 全库统计，
  改为与 event 同款 `scoped_bm25`（scope 池内存评分；term 组内
  相邻+有序）。FTS MATCH 保留为召回下限。
- 回归：审计反例"追加 50 条 scope 外话语不得翻转可见排名"
  （`TestScopedBm25WordsS05S06`）+ 既有 words 域 40 例。

### 1.5 读侧安全语义（P1）

- 安全语义统一、序列化形状不要求统一：
  - `memory.get`（原有 content_role/instruction_authority 基础上）
    补结构化 `content_gap`：legacy forgotten_summary 与空正文各标
    `LEGACY_CONTENT_GAP`（reason 区分）；正常桶无 gap 字段。
  - Bootstrap 开窗包补顶层 `content_role=bootstrap_memory_package` +
    `instruction_authority=none`。
- 回归：`TestReadSideSafetySemantics`（4 例）。

## 2. 其他裁定项

- **F16 补 Plan 分节**：默认包 plan content 超长截断
  （`BOOT_PLAN_SECTION_CHARS`）+ `next_page(section="plan_content")`
  续取拼回全文；mood 按裁定不做。
- **F23 升格 deletion**：核实 `memory.delete`（直删）业务+回执已在
  atomic_write 同事务（补证明测试）；`memory.deletion.decide` 补可
  选 `operation_id`——决定与回执同事务（RA-019 同款），同 key 重试
  重放同一决定结果、删除副作用恰一次、异 payload 结构化冲突。
- **F05 Registry 级集成**：复刻审计反例（title_cue-only profile +
  fake HTTP 0.9）——公开响应不得交付带事件正文的卡（1 例全链路）。
- **F13 负 offset**：`-05:00` 跨业务日边界用例（方向相反的跨日）。
- **F10 跨块边界**：元素跨读取块劈开时限额仍按当前元素 UTF-8 字节
  （过/不过两例，强制 `_CHUNK=20/8`）。
- **CLI exit code**：`python -m mariposa.storage backup` exit 0 /
  坏目录 restore exit 1 的真实子进程验证。
- **F17 lexical_terms 合同**（维持收紧 + 文档化）：
  `lexical_terms[]` 每项是一个独立 lexical clause/phrase（组内有序
  相邻），不是隐含 OR 的关键词袋；要 OR 就传多项；语义近似交给
  dense 路径。keyword-mode 适配应在明确入口做，不动底层合同。

## 3. Playwright / React 验证（环境欠账关闭）

- `npm ci`（lockfile 安装，无改动）；React `npm run build` 通过
  （修掉三个**既有**类型错：`Plan`/`MediaObj` 类型缺失、`DelReq`
  缺 `request_id` 可选字段——与 F22 修改无关的存量问题）。
- Playwright chromium 安装 + 隔离实例 E2E 实跑 **1 passed**：
  有效 token 认证冒烟（活跃能力 200）→ 退役入口 404 本身。
- `start-isolated.mjs` 补显式 `MARIPOSA_ALLOW_CREATE=1`（隔离根
  是 OPS-RECALL-01 的"显式允许新建"合同场景；此前靠 connect 副作
  用建空文件绕过，已被本轮连接前拒绝修复拦出）。

## 4. Migration 状态

| 迁移 | 内容 | 状态 |
| - | - | - |
| formal 28 | 票据 content_version（上一批） | 已应用 |
| runtime 11 | recall_continue_refs（线性接续消费） | 本批新增，事务化 |
| runtime 12 | recall_receipts.revision（交付轮绑定） | 本批新增，事务化 |

均为幂等 `_apply`（schema_migrations 版本门），重复执行不重复
ALTER。

## 5. ACCEPTANCE mapping

- `docs/memory_runtime/ACCEPTANCE.json` 重写为 **v1.7 当前映射**：
  18 条 requirement（V17-R1..R18）→ 真实代表 nodeid → 状态；
  全部 evidence 经 collect-only 核对存在。
- v1.4 历史证据完整保留于 `ACCEPTANCE_v1.4_history.json`（不参与
  当前验收判断，不删除）。
- `V17-R18`（真实 Jev/dense 质量）保持 `NOT_RUN_REAL_MODEL`：
  credential/外部调用/计费/非确定性，不作为静态修复验收阻断，
  不伪装 PASS。

## 6. 全量测试结果（2026-10-04）

- 分批前台、隔离临时根、无网络、无真实模型：
  **894 收集，893 passed + 1 skipped，0 failed**
  （batch1 unit 778+1skip/29s；batch2 p1_fixes+acceptance+integration
  115/19s）。
- 审计五反例脚本：5/5 PASS、退出 0。
- 修复全程沿用"基线证真 → 修复 → 证绿 → 拆 guard 证红"闭环。

## 7. 仍然 NOT_RUN 的项

| 项 | 状态 | 原因 |
| - | - | - |
| TestRET 两项 + real-model smoke 三项 | NOT_RUN_REAL_MODEL | 真实模型 credential/计费/非确定性（裁定维持） |
| 真实 dense/Jev 质量、私有语料阈值 | NOT_RUN | 同上 |
| 生产部署/实际恢复演练 | NOT_RUN | 未授权执行生产操作 |

## 8. 基线

- 修复前基线：`b484908`（审计）→ 第一批修复 `15213a2`。
- 本批 commit：`4e58bd5`（baseline_head 同步登记于
  ACCEPTANCE.json）。
