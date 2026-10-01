# 林石见二轮复审整改（2026-09-30 深夜）

对象 1040dd8 的七项边界漏洞，逐项闭合，全部针对其独立复现的
反例，无新增设计。

| # | 审计反例 | 修复 | 定向测试 |
|---|---|---|---|
| 1 | our_words 命中片段内容错（Jev 收到 event 正文冒充话语命中）；600 字后命中不进窗口 | scoped_bm25 产 `excerpt_by_field`（命中 token ±窗）；pipeline 携带 `_matched`，our_words 命中定位具体话语原文；Jev segments 优先用检索层真实窗 | "梧桐"查询 → our_words 段含真实"梧桐叶落了"、event 段为雨夜正文；600 字后"路灯"进 match_evidence |
| 2 | 多词 OR 被二次安全编译吃掉（0 命中）；speaker 不硬过滤；只截 20 无分页 | source_query.search 新增 `fts_expr`（预编译 OR 直通）+ `speaker` 硬过滤；raw_deep_search 传 OR/speaker/offset，返回 has_more/next_offset；round2 continuation 游标 + coverage partial_has_more | 中秋+约会两词双命中；speaker=qiaosheng 只回乔生消息 |
| 3 | dense unavailable/partial 也签"没有候选"完成事实 | gate 新增 `retrieval_complete`：receipt.coverage 中每个被请求 family 必须 complete_within_scope；unavailable/partial/pending/truncated 列入 incomplete_families 并拒绝 | dense_event=unavailable 时 NO_DELIVERABLE 被拒（独立验证脚本+测试） |
| 4 | 同主体省略 scope 绕过（默认继承） | require_owned_session：session 绑定非空 scope 时请求必须显式携带匹配值（SCOPE_REQUIRED）；空 bound 保留 RUNTIME-09 显式不匹配拒绝；status/round2 schema 补 conversation_scope | chat-A session 省略 scope → SCOPE_REQUIRED；携带匹配值通过 |
| 5 | speaker_excluded 稀疏路不过；专项 replay 按 event phase 剔 CORE 话语；纯 dense word 无证据 | words_search 稀疏路 NOT IN 硬过滤；packet 记 intent，guard 对 intent=find_words 的卡不套 phase；dense word 卡补 content_version + word_verbatim/paraphrase/unverified 证据 + speaker/excerpt | 负向 speaker 过滤；CORE 话语专项重放不丢卡；纯 dense word 有证据有版本 |
| 6 | words_semantic 硬编码 local_bge_zh；word model key 无模型身份 | words_semantic 全面接 provider 抽象：_active_model_key 含 Qwen/BGE 模型名 + wordbody 代次（512/2560 不混用）、_active_dim、_embed_query（BGE 前缀/Qwen instruct）；reindex/search/warmup 统一 | qwen3e4b 下 _provider_active 且 model key 含 Qwen |
| 7 | "小路灯"被只有"小"的文档命中；phrase AND term 未联合 | score_documents：term group 整组连续命中才计分（组间 OR）；phrases AND 保持 | "小路灯"vs"小"不命中；phrase 命中但 term 组未命中不返回 |

另：words.recall 走 operation_id 幂等包装（复审#5 的重放面）；
round2 schema 定稿（session_id+reason 必填 + scope/offset/op 可选）。

## 已知未闭（如实，非本轮新增）

- event 候选池 LIMIT 2000 无分页（S19/WP07）；
- 生产 BURST_ROUNDS=3（v1.7 文档为 2）——待用户批准切换。

## 测试

`tests/unit/test_closure_reaudit2_fixes.py` 12 个（每条对应审计
实测反例）。全量：unit 544 + acceptance/integration 126 =
**670 passed / 1 skipped / 0 failed**。
