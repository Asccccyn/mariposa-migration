# 闭环复审（林石见 2026-09-30）整改记录

审计对象 b7bd8d3；整改在 a003914 之上（含 Qwen 部署），全部
针对审计反例，无自由发挥。

## P1（五项全修）

| # | 审计反例 | 修复 | 定向测试 |
|---|---|---|---|
| P1-1 | 跨主体持 session_id 可读/操作（scope 省略绕过） | `require_owned_session`：session.principal_id 必须匹配当前主体（两 owner 共享记忆，但 session 是主体操作上下文）；scope 省略按 session 绑定、显式不一致拒绝——refine/reject/accept/navigate/status/close/round2 七动作统一走此入口 | 跨主体 status/refine/close/round2 全拒 + 归属者不受影响 |
| P1-2 | 第一轮 words 证据不足直查旧 raw（绕过 Round2 门禁） | 旁路整体删除：Round1 一律 `coverage.raw=round2_only`，证据不足以 continuation 指向 memory.recall.round2；raw 候选只在 Round2 出现 | rawx01 单元+验收双版改写（raw 候选零出现 + continuation 指向 round2） |
| P1-3 | needs_validation 被当"证据不足"（rank_only 下一切正常交付都是它）——正常命中的 session 也能升级 raw | Round1 receipt 新增 `_first_round_facts`（delivered_count/requirement_met/conflicts_count/missing_kinds/evidence_requirement）；gate 按真实事实判：EVIDENCE_INSUFFICIENT←有候选且证据未满足；VERBATIM_REQUIRED_NOT_MET←verbatim 要求未满足；SOURCE_DISAMBIGUATION_NEEDED←conflicts>0；NO_DELIVERABLE←零候选 | 正常"中秋约会"命中声明两种 reason 均拒（reason_fact_supported=False）；真证据不足（verbatim+paraphrase）facts 如实记录 |
| P1-4 | dense `word:`/`channel=word` 与稀疏 `our_word:`/`words` 双身份重复占位；普通 WIDE our_words 命中的 envelope 缺 event_evidence | 身份统一 `our_word:<id>`/channel=words（S01：our_word 是对象）；envelope 分支三分：words 专项（话语=primary）/普通 event 里 our_words 字段命中（话语=match + **event_text=event_evidence 必附**）/event 命中（同段双标） | 同句混合检索单槽位单前缀；event 通道 our_words 命中的 segments 含 event_evidence |
| P1-5 | structured_metadata 无许可仍外发 memory_id/speaker 等；空段候选仍被 judge 打分 | metadata 无许可→全 null；segments 全空的候选不送 payload、直接 unavailable（无证据即无有效判断），不产生请求 | profile 仅 event_excerpt 时 metadata 全 null；无文本许可时候选零请求 unavailable |

## P2（一并修）

- **P2-6 重放丢 source_msg**：revalidate_replayed 对 `source_msg:*`
  按当前库校验（存在且 published=1）后保留——round2 同 operation_id
  重放候选与首次逐字一致（定向测试钉住）。
- **P2-7 raw 深搜**：多 lexical_terms 编译为 FTS **OR**（不再 join 成
  连续必需短语）；limit 放宽至 max(20, 4×delivery_limit)；
  coverage 按 limit 命中如实标 partial_limit_reached。
- **P2-8 dense pending 覆盖 bug**：partial_vectors_pending 不再被尾部
  无条件覆盖回 complete。
- **P2-9** 输出预算 >600 分支的 import 路径修复
  （`.retrieval`→`..retrieval`）；words.recall 公开 schema 加
  semantic_query（words dense 在正式入口不再 SCHEMA_VIOLATION）。
- **P2-10** words dense 的 speaker_excluded / source_date_excluded
  负条件落到池查询。

## 测试

`tests/unit/test_closure_reaudit_fixes.py` 10 个（每条对应审计实测
反例）。全量：unit 532 + acceptance/integration 126 =
**658 passed / 1 skipped / 0 failed**。

## 审计提及但未动的

- event pool LIMIT 2000 + 无分页（S19）：范围属 WP07 分页专题，
  本轮未动——如实记录，不谎报 complete 的问题在 2000 以内不存在。
- BM25 "词内连续" docstring 与实现差异：terms 为 token 级 OR 软
  线索（S04），连续性由 exact_phrases 承担——WP03 记录时已定，
  docstring 已在 WP03 修正；审计引用的是旧描述。
