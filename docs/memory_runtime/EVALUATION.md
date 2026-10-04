> **历史文件（2026-10-04 降级）**：本文件记录的是当时的核对/映射
> 快照，不再是现行语义。现行基线唯一入口是
> `docs/memory_runtime/CURRENT.md`（含"域→现行正本清单"）；
> 冲突一律以 CURRENT 为准，审计不得从本文件推导现行合同。

# 召回质量评测状态（EVALUATION）

日期：2026-09-26｜对应 v1.4 §14

## 分层状态（如实）

| 层 | 状态 | 说明 |
|---|---|---|
| 功能验收（64 条） | **已执行** | 见 ACCEPTANCE.json；A/B 行为层全部真实断言 |
| 合成数据 A/B 对照 | **入口就绪，未批量执行** | `scripts/eval_recall.py`（Hit@1/3、MRR、Recall@10；A=keyword-only 现状，B=hybrid 证据包）；需构造标注评测集（`tests/fixtures/recall_eval/` 目前为空——真实私人样本不得入库） |
| B 组消融（去 dense/去 BM25/改上限） | 未执行 | 依赖评测集 |
| C 组（+真实 Jev） | **未执行（未授权）** | provider 默认 disabled；真实调用需三重前置：provider 配置 + 外发数据策略（MARIPOSA_RECALL_JUDGE_ALLOWED_DATA）+ API key。mock 契约已测（JEV-01..10） |
| dense 通道真实行为 | 未执行（未配置 provider） | SEMANTIC_PROVIDER 未启用时诚实 unavailable（HYBRID-07 已验）；模型缓存已在仓库根（bge-small-zh，91MB），启用属生产授权项 |
| estómago live 换窗 | 未执行（宿主工程缺失） | Mariposa 侧契约已验（SESSION-03 preliminary） |

## 未编造的阈值

- 旧余弦 0.51/相对窗 0.06 保留为现状参数（未调）；
- 自动高置信单条（automatic_confident_top1）保持 **关闭**；
- 未宣称任何"已找对"百分比；门限校准留待真实评测集 + 授权。

## 建议的下一步评测（需乔生确认后执行）

1. 构造 30—50 条**合成标注**样本（覆盖 §14.2 清单：同类不同日期/
   同句不同 speaker/被排除候选/原话-复述-未验证/遗忘/纯无答案/
   超最近 500 条/缺向量），跑 A/B 对照并留报告；
2. 授权后小规模开启本地 dense（模型已在缓存），补 B+dense 消融；
3. Jev 授权链（数据范围→key→合成样本）走完再谈 C 组。
