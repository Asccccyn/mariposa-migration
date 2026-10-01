# WP03 · scope 内真 BM25 与 dense 正确性（recall-closure-20260930）

规则依据：S04（QueryPlan 语义）/ S05（范围先于评分）/ S06
（scope-stage-bm25-v1）/ S07（dense 正确性）。

## 变更

1. **新 `retrieval/scoped_bm25.py`（scope-stage-bm25-v1）**：标准
   BM25——对数 IDF `ln(1+(N-df+.5)/(df+.5))`，k1=1.5、b=0.75
   （版本化参数）；N/df/avgdl 只来自授权＋阶段过滤后的文档集合
   （S05/S06：隐藏/撤权桶不改变可见排名依据）；terms 展开 token
   级 OR 软线索（多字词顺序/连续由 exact_phrases 承担，docstring
   明示）；phrases AND＋连续子序列；重复 token 去重；空集合零
   结果；每 owner 取命中文档最大分（不累加选票）。
2. **`pipeline.round1_lexical_hits` 重写**：field_fts 的全局
   bm25()/逐桶 N+1 MATCH 全部移除——scope＋阶段允许字段批量拉取
   field_search_docs 文本，内存 scoped BM25 评分；matched_fields
   如实、卡带 bm25_score（trace）；coverage 标注 scorer 版本。
3. **S04 query-only semantic**：`_dense_tail` 不再要求词法命中
   为前提；`validate_query_plan` 停止把 original_request 静默
   回填进 semantic_query（browse 不伪造语义请求——纯词法请求
   coverage=not_requested 且不无端降级，显式语义请求才
   unavailable/degraded）。
4. **S07 语料 generation 绑定**：`MODEL_KEY = 名|eventbody-v1`
   进入向量身份与校验——旧 model 名向量整体失效重嵌（RET-05
   迟到行语义保持），corpus 规则升级有明确换代点。
5. **pending 覆盖真实**：semantic_search 外露 pending 向量计数
   （哨兵条目由两处消费端剥离）；coverage 区分
   complete_within_scope / partial_vectors_pending 并带计数
   ——不再在向量未就绪时报告 dense 全覆盖。

## 遗留到后续 WP

- raw 多 term 编译（S14 typed lexical）→ WP04；
- words 索引与 dense 专项 → WP05；
- 流式/分批上限的 2000 池截断 partial+游标（S19）→ WP06。

## 测试

`tests/unit/test_closure_wp03.py` 7 个：BM25 单元（OR/去重/
phrase 连续不拼接/空集/max 语义）、隐藏桶不改可见排名（IDF 隔离）、
query-only semantic、旧 model 身份向量失效。全量：unit 499 +
acceptance/integration 126 = **625 passed / 1 skipped / 0 failed**
（hybrid07/RET_05 旧断言按 S04/S07 新语义更新）。
