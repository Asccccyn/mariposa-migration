# Mariposa Recall 幂等与崩溃恢复整改 · 交付报告（2026-09-29）

执行依据：《Mariposa Recall 幂等与崩溃恢复整改执行方案》（本轮用户
指令全文）。分支 `fix/v17-audit-20260929`，前序 HEAD `6df6dfe`。

最终语义达成：一次 Recall 在运行库中只有两种状态——**完全没有发生**，
或**完整成功发生**。不存在中间态、永久 IN_PROGRESS、OUTCOME_UNKNOWN
或人工对账。

---

## 1. 开工前事实（§16.1，只读确认）

### 1.1 数据库事务域

runtime 库（`<root>/runtime/recall/recall.sqlite3`）单文件单库，以下
对象全部在其中、可由同一连接同一事务原子提交：

```text
recall_sessions / recall_query_revisions / recall_candidates /
recall_receipts / recall_attempts / recall_operation_keys /
（新增）recall_rounds
```

检索读 formal 库（跨库**只读**，不破坏原子性）；已逐处核对 recall 层
全部 6 处 `db.formal()` 使用（service.py:229/664/795/839/892/939），
**全部为只读**（检索、导航查询、回执重校验、words 检索、重放 guard
校验）——recall 全链零正式库写入（§12 审查通过；"明确打开/明开回温"
由 memory 视图层承担，不在 recall 动作内）。

### 1.2 检索是否依赖"已提交 session"

不依赖。`_run_round`（现 `_run_round_compute`）对 session 的唯一读
依赖是排除集 `store.rejected_resource_refs(sid)`：start 时新 session
无候选记录（空集），session_id 仅作为内存关联 ID；refine 时 session
行早已存在。候选/回执写入对 session 行的 FK 依赖本就属于"落库材料"，
现全部移入最终事务（session INSERT 先行，同事务满足 FK）。

### 1.3 当前 Jev 调用实现（整改前实测）

- **client 类型**：自封装 `urllib.request` HTTP client（非官方 SDK、
  非第三方 HTTP 库）。端点 `POST /v1/systemone`（TypeSafe System One）。
- **重试行为（整改前）**：**零自动重试**。`urllib` 无默认重试；
  HTTPError（408/429/5xx/529 一视同仁）、URLError/TimeoutError、
  JSON 解析错全部转 `JudgeUnavailable` → 该批候选标 unavailable，
  **有界降级**继续 RRF 主链，不重发请求。
- timeout：`RECALL_JUDGE_TIMEOUT_MS`（默认 5000ms）。
- request cache：已有（`jev_rerank_cache` 表，稳定 fingerprint：
  query/candidate 投影 + 各版本号 + model/prompt/policy/schema）。
- provider request ID / usage 保存：响应未携带 receipt 字段，
  items 的 `receipt_id` 为空——如实记录，未保存。
- **聚合**：已按 §9 要求聚合——`RECALL_JUDGE_BATCH_SIZE`（默认 12）
  个候选合并为一次请求（一个 state + `candidate_0..N` 多 questions），
  无逐候选调用。本轮无需改动。

---

## 2. 修改清单（§16.2）

### backend/mariposa/schema.py（runtime migration 4）

- 原：无 round 记录表；recall_operation_keys 有上一阶段的
  running/failed 中间态语义。
- 现：`recall_rounds`（成功轮唯一事实源，PK(session_id, round_no)）；
  旧 session 的 `rounds_used` 按数量回填 round 行（burst 窗口按默认
  `RECALL_BURST_ROUNDS=3` 推算，注释声明）；删除全部非 completed 的
  operation 行（commit-at-end 下不存在中间态）。

### backend/mariposa/recall/store.py

- 原：`create_session` 独立事务先建 session；`consume_round` 语义在
  budget 层；`claim_operation` 先 INSERT running 占位、失败标 failed、
  等待轮询 + OperationInProgress；`bump_revision` 独立事务先提交
  revision 前进；`upsert_candidates/add_receipts/record_attempt` 各自
  开事务。
- 现：
  - `new_session_draft`/`insert_session`：start 改为内存 draft（阶段
    B），session 与首版 query revision 只在最终事务写入。
  - `advance_revision`：refine 的 revision 前进（CAS）移入最终事务。
  - `record_round`：最终事务内登记成功轮（写锁内 MAX+1），并把
    session 行 `rounds_used` 对齐为派生值（展示缓存，非权威）。
  - `get_session`：读时以 `COUNT(recall_rounds)` 派生覆盖 rounds_used
    （单一派生点；列不再是权威）。
  - `run_operation` 取代 `claim_operation`：**不写任何中间态**；先读
    已完成 operation（命中即经 guard 重放）；进程内 per-key 锁（仅
    性能优化：避免同进程重复跑昂贵检索/Jev）；builder 内部最终事务
    完成全部写入。跨进程并发输家由最终事务写锁内的 operation 查重
    （`record_operation_row` 抛 `OperationRaceLost`）+ 数据库主键
    UNIQUE 双重裁决，输家整体回滚并重放赢家结果。设为型动作由
    run_operation 在执行成功后补写响应记录（动作自身幂等，崩溃窗口
    重放结果一致）。
  - `upsert_candidates/add_receipts/record_attempt` 改为事务内函数
    （conn 由调用方传入）；navigate 的 upsert 调用自开小事务。
  - 删除 `OperationInProgress` 依赖；`purge_expired` 保留 TTL 清理与
    operation 行有界清理。

### backend/mariposa/recall/budget.py

- 原：`consume_round`（UPDATE rounds_used+1 + record_attempt）作为
  预算扣减事实源；session 行可变计数为权威。
- 现：删除 `consume_round`。唯一事实源 = `COUNT(recall_rounds)`；
  `require_round_available`（阶段 A 只读预检，避免明显无预算时的
  API 调用）；新增 `ensure_round_available_conn`（最终事务内、写锁
  串行化下的预算终检，超限抛 BUDGET_EXHAUSTED 整体回滚）。
  `snapshot/rounds_left_in_burst/try_new_burst` 语义不变（读派生值）。

### backend/mariposa/recall/service.py

- 原 `start`：建 session（提交）→ 扣预算（提交）→ 检索 → 写候选/回执
  （各自提交）→ 状态（提交）——五段提交，任意中途失败留下半状态。
- 现 `start`（阶段 A→D）：只读预检 → 内存 draft → `_run_round_compute`
  纯计算（检索/Jev/组装，允许失败=零痕迹）→ **单事务**（BEGIN
  IMMEDIATE）：operation 裁决 → session+query_revision → 预算终检 →
  round → candidates/receipts/状态/round1 回执 → attempt → COMMIT。
- 原 `refine`：bump_revision（提交）→ 扣预算（提交）→ 检索 → 各自提交。
- 现 `refine`：读校验 → burst 授予计算（纯内存）→ 纯计算 → **单事务**：
  operation 裁决 → advance_revision（CAS，并发输家 REVISION_CONFLICT
  整体回滚）→ 预算终检 → round → 派生行 → COMMIT。无余额分支保留
  "允许修订条件（revision 前进）但不发起有成本检索"的既有语义
  （v1.4 §9.3），设为型独立小事务。
- `_run_round` → `_run_round_compute`：不再写库，返回
  (packet, effects)；budget 快照按"含本轮成功"预览。
- `reject/accept/navigate/close` 增加 `op_ctx` 形参（设为型动作，不
  在最终事务内写 operation，由 run_operation 补写响应记录——重复
  执行结果不变的幂等"设为"语义，§11 类型 B）。

### backend/mariposa/recall/pipeline.py

- 新增 `mark_round1_complete_tx`（同语义事务内版本）；原独立事务
  版本保留（无其他调用方后仅为兼容）。

### backend/mariposa/capabilities/registry.py

- `_with_operation_id`：改走 `run_operation`；payload_hash 保留同 key
  异请求结构化冲突；重放仍经 `revalidate_replayed` guard（审计 F07
  修复延续：session 有效期/候选版本/可见性/当前 phase 重校验）。

### backend/mariposa/retrieval/judges/typesafe_jev.py（§8）

- 原：零重试（所有错误一次降级）。
- 现：仅 **429/529**（远端明确拒绝、输入未执行）有限重试（2 次，
  0.5s/1.0s 退避）；408/其他 5xx/timeout/connection error 保持
  **不自动重发**——是否已被远端执行无法判断，重发会产生重复供应商
  输入费用。聚合（§9）与缓存定位（§10，性能优化非正确性依赖）经
  确认已符合，未改动。

---

## 3. 数据库迁移（§16.3）

runtime migration (4)：

```text
新增表   recall_rounds(session_id, round_no, burst_no, operation_key,
          created_at; PK(session_id, round_no))
新增索引 idx_recall_rounds_op(operation_key)
回填     旧 session 按 rounds_used 数量生成 round 行
          （burst 窗口按默认 3 推算；部署曾改配置则仅影响旧 session
          的 burst 归属展示，不影响总量语义）
清理     DELETE recall_operation_keys WHERE status<>'completed'
          （上一阶段 running/failed 中间态语义废弃；running 残留的
          副作用状态不可知，删除后同 key 重试重新完整计算）
保留     recall_operation_keys 主键 = 数据库级 UNIQUE
          (principal_id, operation_key)（§5.2 满足）
          recall_sessions.rounds_used 列保留但降级为派生展示值
          （get_session 读时以 COUNT 覆盖，record_round 同事务对齐）
```

未改正式库 schema。未删除任何用户数据（migration 仅删中间态
operation 行，非业务数据）。

---

## 4. 所有动作审查表（§16.4）

| action | 写入内容 | 单事务 | 天然幂等 | 不可恢复副作用 | 结论 |
|---|---|---|---|---|---|
| start | session+query_revision+round+candidates+receipts+status+round1回执+attempt+operation | ✓ commit-at-end | 否（计算重）→ operation UNIQUE + 写锁内裁决 | 无 | 完成 |
| refine（有检索） | advance_revision(CAS)+round+candidates+receipts+status+attempt+operation | ✓ | 否 → UNIQUE + CAS | 无 | 完成 |
| refine（无余额） | advance_revision+status=BUDGET_EXHAUSTED | ✓ 独立小事务 | ✓ 设为型 | 无 | 完成（operation 后写缓存响应） |
| reject | set_candidate_state=rejected（含事件级展开） | 独立小事务 | ✓ 设为 | 无 | 完成（§11-B） |
| accept | state=accepted（+可选 status=RESOLVED） | 独立小事务 | ✓ 设为 | 无 | 完成（§11-B） |
| navigate | candidates upsert（ON CONFLICT UPDATE） | 独立小事务 | ✓ upsert 同数据 | 无 | 完成（§11-B） |
| close | status=RESOLVED/CANCELLED | 独立小事务 | ✓ 设为 | 无 | 完成（§11-B） |
| status | 惰性 EXPIRED/STALE（运行态单调维护） | 独立 | ✓ 单调 | 无 | 读类维护 |
| words_recall | words 检索 + 派生索引重建 | — | ✓ 派生可重建 | 无 | 读类 |
| 正式库 | 无写入（全链只读） | — | — | — | §12 通过 |

累加型写入（§11-C）：预算"扣减"已整体转换为成功 round 记录（UNIQUE
PK 保护，无重复累加面）；`record_attempt` 为诊断记录（UPSERT，
非事实源）。

---

## 5. 测试（§16.5）

新增 `tests/unit/test_v17_recall_commit_at_end.py`（§14 A–I 全组）：

```text
A  test_retrieval_failure_no_trace_then_retry      检索异常→零痕迹→重试成功
B  test_jev_exception_no_trace_then_retry          Jev 异常→零痕迹→重试成功
C  test_assembly_failure_after_jev_no_trace        Jev 成功后组装异常→零痕迹
D  test_mid_transaction_failure_full_rollback      事务中途（session/round
   INSERT 已成功后 receipts 抛错）→ 整体回滚四表全空
E  test_duplicate_operation_returns_first_result   重复提交返回首次结果，
   session/round/operation 零新增
F  test_two_workers_same_operation_single_effect   真实线程并发同 op：
   1 session / 1 round / 1 operation，恰一方重放
G  test_concurrent_refines_cannot_exceed_burst_rounds
   两边计算完成后同步冲线并发抢最后一轮：恰一个成功（CAS 串行化），
   rounds 恰为上限 3，无第 6 轮类超额
H  test_crash_leaves_no_trace_and_retry_succeeds × 4
   真实子进程 os._exit 于：draft 后 / 检索后 / Jev 后 / 最终事务内
   （session INSERT 已执行、COMMIT 前）——崩溃后 runtime 库零痕迹
   （独立进程 sqlite 只读核验），重启同 op_id 重试恰产生一次正式结果
I  test_response_lost_client_resubmits             COMMIT 后响应丢失重试
   → 读取已有结果，无重复副作用
```

另有上一阶段 F26 测试按新语义改写（`TestF26CommitAtEnd`：并发单执行、
失败零痕迹可重试、payload 冲突、顺序重放）。

执行（venv，前台、显式超时、临时隔离根，遵守 AGENTS.md 约束）：

```text
pytest tests/unit -q                                438 passed, 1 skipped (22.87s)
pytest tests/acceptance tests/integration -q        122 passed (1.74s)
合计                                                560 passed, 1 skipped / 0 failed
```

并发测试为真实 ThreadPoolExecutor + Barrier；crash 测试为真实
`multiprocessing spawn` 子进程 + `os._exit`，断言用独立 sqlite 连接
只读核验，不经业务代码。

---

## 6. 剩余风险（§16.6）

1. **第三方 API 计费非 exactly-once（方案已声明，如实保留）**：Jev /
   embedding 是外部 API。极端情况下远端已处理请求而本地进程随后
   崩溃，同 operation 重试会重新发送输入、可能再次产生供应商费用。
   本轮已通过"零自动重试不可判断错误 + 仅 429/529 有限重试 + 稳定
   fingerprint 缓存"把窗口压到最小，但**不**为此引入 durable
   workflow / step ledger / Saga（§15 禁止）。此风险与 Mariposa 自身
   round budget 的 exactly-once（已由本方案保证）是两件事。
2. **旧库 burst 归属推算**：migration 4 回填按默认 BURST_ROUNDS=3
   推算旧 session 的 burst 窗口；若部署曾改该配置，仅影响旧 session
   的 burst 展示归属，总量（round 行数）不受影响。
3. **设为型动作的 operation 后写窗口**：reject/accept/navigate/close
   的响应记录在动作事务之后补写；窗口内崩溃则重试重新执行动作——
   动作本身幂等（设为），结果一致，仅响应体可能重新组装。
4. **进程内锁非最终保证**：跨进程同 key 并发会各自计算一次（检索/
   Jev 费用可能双份），数据库仍只留一份正式结果（写锁内裁决）。

---

## 7. 删除的旧逻辑（§13 条件核对）

```text
1 start/refine commit-at-end 改造             ✓（§2/§5）
2 operation 不再提前落库                      ✓（无 running/failed 写入）
3 operation_id 数据库唯一约束                 ✓（主键 principal_id+
                                              operation_key）
4 round budget 成功记录派生                   ✓（recall_rounds）
5 最终 budget check 写锁串行化                ✓（ensure_round_available_
                                              conn + G 组并发测试）
6 其余 actions 逐个检查                       ✓（§4 审查表）
7 无"先提交不可逆副作用再继续"残留             ✓（检索阶段零写库）
8 崩溃/并发/重复提交测试全过                  ✓（A–I + 560 绿）
```

OUTCOME_UNKNOWN / reconcile / stale IN_PROGRESS cleanup 的运行库语义
已按条件全部移除（上一阶段的 OperationInProgress 错误类保留在
errors.py 但 runtime 不再使用——错误码定义无害，formal 侧 Busy 同码
场景不受影响）。
