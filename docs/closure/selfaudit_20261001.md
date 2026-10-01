# 自审报告：a28797f（三轮整改）+ 2a64285（S19）全量自查（2026-10-01）

方法：以审计者姿态对两批新增/改动代码逐攻击面构造反例（不看
"测试是否绿"——那是底线不是证据）；既有代码只查与本批的交互面。
自审不替代独立复审，结论供四轮复审对照。

## 发现并已修复（3）

| # | 级别 | 洞 | 修复 |
|---|---|---|---|
| 1 | 中 | **翻页游标并发双花窗口**：token 校验在计算前读行、签发在最终事务，中间无锁——并发同 token 双请求都过校验、重复计算、重复 Jev 出站（真实 key=双倍花销），游标行互相覆盖 | 最终事务 CAS：`store.replace_raw_continuation`/`clear_raw_continuation_if` 带 expect_token，输家回滚拒 CONTINUATION_INVALID。commit-at-end 哲学：崩溃在事务前的重试不受影响（行未动，旧 token 仍有效） |
| 2 | 低 | **首页接受客户端 offset**（自由起跳任意位置）与服务端签发游标模型矛盾 | 无 token 调用 offset 一律 0；schema 字段保留兼容 |
| 3 | 低 | 死代码：`source.query._body_excerpt` wrapper、`store.clear_raw_continuation`（非 CAS 版）无调用方 | 删除 |

定向测试：`tests/unit/test_closure_selfaudit_20261001.py` 3 个
（store 级 CAS 语义+输家不动行；公共链路同 token 双花恰一赢家；
首页 offset=20 企图被忽略、下页游标=20 非 40）。

## 记录不修（5，交四轮复审裁定）

| # | 级别 | 事项 |
|---|---|---|
| 4 | 边界 | phrase 恰好横跨两句 our_words 时单句打分不中，回退字段级拼接命中窗（检索层真实窗，非编造；罕见形态） |
| 5 | 既有 | mixed 通道（event+words）的负向键须同时属于所有通道白名单——v1.4 契约模型既有行为，非本批引入 |
| 6 | 既有 | 浏览模式（无检索词 latest-K 窗口）coverage 写 complete_within_scope（service.py:105）——S19 只管词法候选池；browse 是否同样诚实化待裁定 |
| 7 | 性能 | round1_lexical_hits 逐桶 phase_of（N+1）；池取尽后桶数可超 2000，查询量随之上升（千级桶实测亚秒级）。facts_for_many 批量化（F12 模式，round1_candidates 已用）留 WP07 资源画像一并 |
| 8 | 边界 | `_anchors_excerpt` 锚词全部定位失败时退头部窗（不编造位置）：FTS 归一化与 `_token_spans` 口径如有细微差异表现为摘录降级而非错误内容 |

## 核查过无洞的面（负结果，证明覆盖）

- `_matched` 新键 `our_words_word_id`：typesafe_jev 按键取不遍历，
  无伪段风险；
- `coverage["event"]` 全部判等消费方为透传/值集判断，
  `partial_pool_truncated` 不撞只认 complete 的分支；
- continuation：refine/revision 前进自动失效、单活跃重签、翻尽清行、
  operation_id 缓存重放不触碰游标行、首页 gate 失败不留脏行；
- 负向过滤 NULL 日期安全（words 稀疏/dense/raw 三路一致
  `IS NULL OR NOT BETWEEN`）；
- S19 helper 的 cap 边界（`del ids[cap:]`、触顶探针、恰好扫完仍
  complete）、rejected NOT IN 与 keyset 游标参数序；
- 契约收紧回归面：speaker 正向必须单字符串、excluded 空数组合法；
- find_words 空 query 回填后仍被 validate 拒（不伪造必填）；
- migration 7 幂等升级（版本化 _apply）。

## 测试

全量：unit 566 + 1 skipped（分批前台）+ acceptance/integration
126 = **692 passed / 0 failed**。
