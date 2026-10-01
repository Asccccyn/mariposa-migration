# 林石见第三轮复审请求（2026-09-30 深夜）

对象：`02adf9b`（分支 recall-closure-20260930 tip，工作树无跟踪内改动）。
前序：一轮五项闭合于 `1040dd8`，二轮七项闭合于 `02adf9b`。

**请求范围**：
1. 二轮七项逐项验证闭合（建议仍按你的方法独立构造反例，不要只读修复说明）；
2. 修复本身是否引入新洞（尤其 #2 预编译 FTS 直通与 #7 组连续计分这两处动过打分/查询内核）；
3. 一轮五项只需回归确认（`tests/unit/test_closure_reaudit_fixes.py` 10 个仍在）。

## 二轮七项 → 修复落点 → 定向测试

| # | 你的反例 | 修复落点 | 定向测试（tests/unit/test_closure_reaudit2_fixes.py） |
|---|---|---|---|
| 1 | our_words 命中片段是 event 正文；600 字后命中丢失 | `retrieval/scoped_bm25.py`（`excerpt_by_field` 命中 token ±窗）；`recall/pipeline.py`（卡携带 `_matched`，our_words 命中定位话语原文）；`retrieval/judges/typesafe_jev.py`（segments 用检索层真实窗） | `test_our_words_hit_sends_real_word_text` / `test_late_hit_reaches_jev` |
| 2 | 多词 OR 被二次安全编译吃掉；speaker 不硬过滤；截 20 无分页 | `source/query.py`（`fts_expr` 预编译直通 + `speaker` 硬过滤 + offset 分页）；`recall/service.py`（raw_deep_search 返回 has_more/next_offset；round2 continuation 游标 + coverage `partial_has_more`） | `test_multi_term_or_finds_both` / `test_speaker_hard_filter` |
| 3 | dense unavailable/partial 也签"没有候选"完成事实 | `recall/pipeline.py`（gate 新增 `retrieval_complete`：被请求 family 必须 complete_within_scope，否则拒绝 NO_DELIVERABLE） | `test_unavailable_dense_blocks_no_deliverable` |
| 4 | 同主体省略 scope 绕过（默认继承） | `capabilities/registry.py`（require_owned_session：非空绑定 scope 必须显式携带匹配值，否则 SCOPE_REQUIRED）；`capabilities/input_schemas.py`（status/round2 schema 补 conversation_scope） | `test_scoped_session_requires_explicit_scope` |
| 5 | 负向 speaker 稀疏路不过；专项 replay 剔 CORE 话语；纯 dense word 无证据 | `retrieval/words.py`（稀疏路 NOT IN）；`recall/pipeline.py`（packet 记 intent，find_words 卡不套 event phase；dense word 卡补 content_version + word_verbatim/paraphrase/unverified + speaker/excerpt）；`recall/service.py`（words.recall operation_id 幂等） | `test_negative_speaker_excluded_sparse` / `test_specialized_replay_keeps_core_words` / `test_dense_word_has_evidence` |
| 6 | words_semantic 硬编码 local_bge_zh；word model key 无模型身份 | `retrieval/words_semantic.py` 全面接 provider 抽象（`_active_model_key` 含模型名+wordbody 代次、`_active_dim`、`_embed_query` BGE 前缀/Qwen instruct；reindex/search/warmup 统一） | `test_words_semantic_active_under_qwen` |
| 7 | "小路灯"被只有"小"的文档命中；phrase 与 term 未联合 | `retrieval/scoped_bm25.py`（term group 整组连续命中才计分，组间 OR；phrases AND 保持） | `test_term_group_requires_continuity` / `test_phrase_and_terms` |

## 02adf9b 改动面（除文档/测试外的 9 个源文件）

input_schemas / registry / recall/pipeline / recall/service /
judges/typesafe_jev / scoped_bm25 / words / words_semantic / source/query

## 测试基线（2026-09-30 深夜实测）

- 全量：unit 544 + acceptance/integration 126 = **670 passed / 1 skipped / 0 failed**
- 本请求发出前复验两轮反例套件：**22 passed in 1.81s**（前台、最小范围，符合 AGENTS.md 约束）
- 全程确定性 fake Jev/embedder（conftest autouse），真实模型未参与——见下

## 已知未闭（如实申报，请勿计为三轮新发现）

1. event 候选池 LIMIT 2000 无分页（S19，WP07 批次）；
2. 生产 BURST_ROUNDS=3（隔离验收按 2，切换待江乔生批准）；
3. WP07 对照消融（Hit@K/Recall@K）与她出题的真实语料阈值复核——待固定快照+真实模型；
4. LIVE_MODEL_EVALUATED=false（真实 Jev key 与外发样本属生产门，未授权）。

## 通过判据

七项全闭合且无新洞 → 判主链闭合 → 合并 main（生产门另批，见
GLM_DELIVERY_final.md「未完成与需授权项」）。
