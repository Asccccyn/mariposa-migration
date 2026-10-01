# WP05 · words 专项 dense 与三入口统一（recall-closure S08）

## 变更

1. **新 `retrieval/words_semantic.py`**：words 专项向量层——
   - 索引父单位 = 稳定 word_id（一条完整 authored 话语一个向量，
     独立 `word_embeddings` 表与 `words|wordbody-v1` model 身份，
     不与 event 向量空间混用）；
   - 向量身份绑定词条指纹（文本+speaker+expression_kind+
     source_ref+所属 memory 版本+语料 generation）——词条编辑/
     来源变化即失效重嵌（测试钉住）；
   - 阈值独立常量 `MARIPOSA_WORDS_SEMANTIC_THRESHOLD`（工程初值，
     专项评测后校准，不复制 event 经验值声明）；provider 未配置
     显式不可用；pending 向量计数外露。
2. **session words 融合**（S08）：BM25 与 dense 各自成序后同通道
   RRF（k=60）按 word_id 去重；speaker/日期条件同样前置到 dense
   池（S05）；coverage 区分 words_dense 状态。
3. **三入口统一**（session words / memory.words.recall /
   memory.find_words）：独立入口内部走 start 机制（短期 session：
   预算、Round1 回执、同一层 Jev 出口、≤3 交付、输出预算），不再
   直返 20-30 条正文，不另写 pipeline；纯 words 轮记
   `kind='words'`（S17 预算类别）。
4. event 向量空间不受影响（普通 WIDE 不新增 words dense 第三票，
   S08/本批不做）。

## 测试

`tests/unit/test_closure_wp05_words.py` 5 个：dense 语义近邻召回
（fake embedder）、独立向量空间身份、词条指纹失效、三入口统一
形态（≤3+needs_validation+session 化预算消耗）、speaker 条件对
dense 生效。旧直返形态断言（v13/v14/parity words hits）迁移到
packet 形态。

全量：unit 518 + acceptance/integration 126 =
**644 passed / 1 skipped / 0 failed**。

## 遗留

- 真实模型 words 阈值校准与 warmup（权重在本地，未授权生产启用）
  → WP07/交审阶段；
- 长词条 token 上限分段（word_id+segment_id+offsets）——当前
  完整词条单向量，超长词条的模型输入截断可观测性归 WP07。
