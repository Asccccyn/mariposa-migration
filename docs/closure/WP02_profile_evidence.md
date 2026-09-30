# WP02 · 外发许可、投影指纹与证据取窗（recall-closure-20260930）

规则依据：S09（候选证据与 Jev 输入对齐）/ S15（外发许可与最小
证据）/ S16（输出预算的单候选窗）/ S18（缓存身份）。

## 变更

1. **S15 provider data profile**（`typesafe_jev.py`）：
   `MARIPOSA_RECALL_JUDGE_ALLOWED_DATA` 解析为显式许可集——
   `event_excerpt / title_cue / word_excerpt / source_excerpt /
   structured_metadata`；`event_excerpt_only` 精确兼容映射；
   含未知值 fail closed（`allowed_data_profile_invalid`，比旧
   "非空即过"严格：拼错也禁用）。`_candidate_projection` 按候选
   来源类型核对许可：word→word_excerpt、raw/source→
   source_excerpt、纯标题命中→title_cue、其余→event_excerpt；
   无许可字段不外发（置空），候选仍可凭其余合法字段被判断。
2. **S09/S18 指纹扩充**：`_query_projection` 增加 semantic_query、
   explicit_negative_constraints、exact_phrases、temporal_axis、
   intent——任一变化即新缓存身份，旧判断不得复用于新语义输入
   （`test_fingerprint` 组四向验证 cache_key 变化）。
3. **S09 命中处取窗**（`evidence.excerpt`）：新增 anchors 参数——
   超长文本以查询词命中文位置取窗（前 1/3 余量），不再固定头部
   截断吞掉后段命中；句读边界收敛与 truncated 标记保持。

## 遗留到后续 WP（如实）

- fusion 并集证据与 CandidateEnvelope 强类型（S09 contracts 结构）
  → 与 WP04 raw envelope 一并落；
- our_words 索引拆 word_id 可反查粒度 → WP05；
- 缓存身份的 offsets/段落粒度 → WP04（raw offsets 引入后统一）。

## 测试

`tests/unit/test_closure_wp02.py` 10 个（profile 解析/未知 fail
closed/兼容映射/字段过滤双向/指纹四向/命中窗）。全量：unit 492 +
acceptance/integration 126 = **618 passed / 1 skipped / 0 failed**。
