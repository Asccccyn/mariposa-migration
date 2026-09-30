# WP01 · 出口与 scope（recall-closure-20260930）

规则依据：S04（可信执行上下文）/ S05（范围先于 Top-K）/
S10（Jev 唯一出站口）。

## 变更

1. **selection 出站硬门**（`retrieval/selection.py`）：未判断（无
   judge）、`evaluation_status != evaluated`、非法分值（NaN/inf/
   越界/布尔，口径=JEV-03 sanitize [-1,1]）、判断版本与候选版本
   失配（双方非空时）一律不得交付。低分 evaluated 合法（S11
   rank_only）。action 修正：未校准 profile 前一切交付标
   `needs_validation`（消除旧 `deliver` 确信态）。
2. **words / raw-fallback 候选进入 judge 输入**
   （`recall/service.py`）：judge_candidates = event + words +
   raw-fallback（cap40）——S10"所有未知候选过一层 Jev"最小落地；
   完整 mixed 各 20/RRF 合序送判归 WP02/WP04/WP05。
3. **provider 不可用不直出**：交付为空 + missing 结构化说明
   （"Jev 判断不可用……候选正文不直出"）。
4. **scope 归属统一**：`revalidate_replayed` 增加 request_args，
   重放同样核对 conversation_scope（显式不一致拒绝；省略不绕过）。
5. **round2 归 runtime 幂等集**：`memory.recall.round2` 加入
   `_RECALL_RUNTIME_CAPS`，不再落入 formal 长期响应缓存。
6. **upsert 状态优先**（`recall/store.py`）：rejected/accepted 不被
   新的 seen 写入覆盖（SQL CASE）。
7. **测试基座**：conftest 注入确定性 fake judge
   （`test_deterministic`，分值与 resource_ref 绑定可复现），
   autouse fixture 对抗 clear 类清理（防顺序依赖）；显式测
   disabled 的用例在自身作用域设置。旧"无 judge 也交付"语义的
   既有测试（fusion/pack/jev05/rawx01 等）按 S10 更新。

## 现状确认（无需改）

- 负向条件（event_date_excluded/categories_excluded）已有真实
  SQL WHERE 实现（query_plan.AllowedScope）；
- 正向 categories/mood_tags/event_date 同样代码化（_pool_where）；
- find_words 专项（memory.words.recall / find_words）不走
  operation 重放 → 无阶段误过滤面（words 专项跨阶段语义的完整
  实现归 WP05）；
- 错误响应 detail 为结构化字段，无正文。

## 测试

`tests/unit/test_closure_wp00_probes.py` 7 探针全绿（WP00 红→绿
闭环）。全量：unit 482 + acceptance/integration 126 =
**608 passed / 1 skipped / 0 failed**。
