# 林石见三轮复审整改（2026-09-30 深夜·换窗后）

对象 02adf9b 的 5 个边界漏洞 + 1 个接口问题，逐项闭合，全部针对
其独立复现的反例，无新增设计。全量 686 绿（670 基线 + 16 个新反例）。

| # | 审计反例 | 修复 | 定向测试 |
|---|---|---|---|
| 1 | coverage gate 遍历所有字符串字段，`lexical_scorer="scope-stage-bm25-v1"` 被当"不完整 family"，误杀合法 event 原文升级（EVENT_LEGIT_R2_ALLOWED=False） | gate 只认 retrieval family 状态键白名单（event/dense_event/words_lexical/words_dense/judge）；`words_forgotten="disabled:<策略>"` 同属 metadata 一并摘出；dense unavailable 仍拒（原门不松） | lexical_scorer 在 coverage 时合法 round2 通过；words_forgotten 注记不阻断；dense_event=unavailable 仍拒 |
| 2 | 第一页 partial_has_more + continuation offset=20，翻页被 no_prior_raw_round 挡死（分页≠新 logical round） | runtime migration 7 `recall_raw_continuations`：服务端签发 token 绑定 session/revision/burst（单活跃）；带 token 的调用走独立校验路径——不重走六条件门禁、不记 round/attempt、不耗预算；offset 以服务端游标为准；翻尽清行，refine 前进 revision/burst 自动失效；旧/伪造 token → CONTINUATION_INVALID | 25 条原文：第二页执行且 round/attempt 计数不增、第二页候选与第一页不相交、翻尽后旧 token 拒、伪造 token 拒 |
| 3 | 同桶 our_words #1"小猫今天很乖"（含"小"）抢走"小路灯"的命中，Jev 收错句 | `_locate_word_text` 重写：每句 our_words 建 doc（同 normalize），用 `scoped_bm25.score_documents` 同一查询语义（组内连续/组间 OR/phrases AND）取最高分句；返回携带 word_id（`_matched.our_words_word_id`），Jev 不二次猜 | "小路灯"查询 Jev 段=真实命中句、不含"小猫"；单元级：定位结果带 word_id |
| 4 | `source_date_excluded` 稀疏 words 路没接（排除 09-20 两条都回）；raw Round2 只继承正向条件；speaker_excluded 契约容忍字符串 | 新 `query_plan.source_scope`/`source_scope_sql`：正/负 speaker+日期一套语义，words 稀疏（`words.py`）、words dense（`service.py`）、raw Round2（`pipeline.raw_deep_search`→`source_query.search` 新增 speakers_excluded/date_ranges_excluded）三处复用；契约收紧：speaker_excluded 必须合法说话人数组、日期排除必须 {from,to} 区间数组（此前字符串/单对象被静默当无效） | 稀疏路排除日期只剩 09-30；raw 排除"qiaosheng+09-20"后只剩 jiaming 的 09-30；字符串/非数组契约拒绝 |
| 5 | Round2 OR 检索命中，但 Source 层把整个 FTS 表达式当关键词截窗，"中秋"在长消息尾部时 excerpt 给的是开头 | `source_query.search` 分离 `fts_expr`（检索）与 `anchor_terms`（锚词=原始 terms/phrases）；`_locate_window`/`_anchors_excerpt`：锚词按序定位命中窗（token 序列→原文映射，SL-03 不变），全部未定位才退头部窗 | "中秋"在 480+ 字铺垫后的尾部——excerpt 含真实命中词；单元级：OR 表达式不当锚词 |
| 6 | `memory.find_words(query=...)` 只传 query 报"original_request 必填" | `_find_words` 用 query 回填 original_request（显式值优先） | 只传 query 正常建 session 且命中交付 |

另：`speaker_excluded`/`source_date_excluded` 契约收紧是行为变化——
此前传字符串/单对象会被静默忽略，现在直接 INVALID_ARGUMENT（把静默
失效显性化，调用方拿到的拒绝可诊断）。

## 同意并保留的质量项（不属本轮缺陷）

- Qwen words dense 阈值仍为独立默认 0.51，未用真实"我们的话"语料
  校准——留 WP07（与她出题的真实语料阈值复核同一批）。

## 已知未闭（如实，非本轮新增）

- event 候选池 LIMIT 2000 无分页（S19/WP07）；
- 生产 BURST_ROUNDS=3（v1.7 文档为 2）——待用户批准切换。

## 测试

`tests/unit/test_closure_reaudit3_fixes.py` 16 个（每条对应审计
实测反例 + 契约收紧 + 单元级语义探针）。全量：unit 560 + 1 skipped
（分批前台：全目录 21s + test_p1_fixes 单批 17s）+
acceptance/integration 126 = **686 passed / 1 skipped / 0 failed**。

## 数据与迁移

- runtime migration 7（recall_raw_continuations 表）：升级自动跑、
  可回滚；旧 session 无游标行=无活跃翻页，语义不变。
- 无 formal 库迁移；无生产配置变化。
