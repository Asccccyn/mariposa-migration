# S19 提前收口：event 候选池分页取尽 + coverage 诚实化（2026-10-01 凌晨）

依据：江乔生指示"先给我修掉"——原排 WP07 的 S19 提前到四轮复审前
完成（林石见三轮报告引用的已知未闭项："超过 2000 个作用域候选时
依旧可能漏，同时 event=complete_within_scope 会说得过头"）。

## 修复

1. **池取尽**：`pipeline._scope_pool_ids` 以 keyset 分页（memory_id
   游标、批 500）迭代完整 scope 池——第 2001+ 个桶不再被
   `LIMIT 2000` 截断丢弃。主链 `round1_lexical_hits` 与旧适配层
   `round1_candidates` 两处同步替换。
2. **安全阀**：`RECALL_POOL_MAX_BUCKETS`（env
   `MARIPOSA_RECALL_POOL_MAX_BUCKETS`，默认 20000）防失控规模；
   触顶前先探针确认确有下一桶才标 truncated（恰好扫完=完整，不
   保守误报）。
3. **诚实 coverage**：truncated 池 `event="partial_pool_truncated"`
   （不在 round2 gate 的 complete 值集 → 正确进 incomplete_families
   拒绝升级）；新增 metadata `event_pool={scanned, truncated}`，
   按三轮#1 白名单语义不参与 family 判断。
4. dense 路核查：`semantic_search` 池无上限（全量扫描），
   `dense_event=complete_within_scope` 声明为真，无需改动。

## 测试

`tests/unit/test_closure_s19_pool.py` 3 个：

- 第 2005 桶（2004 填料 + 1 命中）被检索到且签 complete
  （scanned=2005）；
- cap=50 触顶：`partial_pool_truncated` + 无候选冒充 + round2 被
  incomplete_families 含 event 拒绝；
- 恰好 50 桶扫完：探针精确——仍签 complete（scanned=50）。

全量：unit 563 + 1 skipped（分批前台）+ acceptance/integration
126 = **689 passed / 0 failed**。

## 留存

- 生产默认 20000 上限对私人库规模（千级桶）远不可及；若未来语料
  涨到万级，触顶会以 partial_pool_truncated 显性暴露而非静默漏。
- 浏览模式（无 terms 的 latest-K 窗口）的 coverage 语义未在本项
  范围（S19 只指词法候选池）；如四轮复审认为 browse 也需同样
  诚实化，单独立项。
