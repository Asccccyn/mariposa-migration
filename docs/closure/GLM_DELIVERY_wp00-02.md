# Mariposa 召回闭环实施 · 阶段交付（WP00–WP02）

包版本：recall-closure-20260930-r1
实施人：程知行（GLM）
起始 HEAD：2e3f1e3（工作分支基线；对参考 HEAD 4b8b34f 的差异映射
见 WP00 记录——2026-09-30 三裁定执行，保留）
工作分支：recall-closure-20260930
生产源码／数据／配置是否改变：NO（全部为隔离分支与测试根工作）

## 状态分层

```text
IMPLEMENTED：WP00 / WP01 / WP02（本报告范围）
UNIT_TESTED：是（含结构级 fake Jev/embedder）
INTEGRATION_TESTED：部分（registry 入口级；HTTP/MCP 适配器层未单独跑）
LIVE_MODEL_EVALUATED：false（真实 Jev 调用未授权；真实 BGE 仅此前
  smoke 覆盖 dense 语料，未做质量对照）
DEPLOYED：false
```

## 工作包与提交

| WP | commit | 状态 | 规则 |
|---|---|---|---|
| WP00 基线冻结+红探针 | c2a0ffa | PASS | S10/S11 复现 |
| WP01 出口硬门+可信 scope | f2bc9d7 | PASS | S04/S05/S10 |
| WP02 外发 profile+指纹+取窗 | 483e6db | PASS | S09/S15/S16/S18 |
| WP03 scope 内真 BM25 | — | NOT_STARTED | S06 |
| WP04 Round2 运行库事务化 | — | NOT_STARTED | S12-S14/S17 |
| WP05 words 专项 dense | — | NOT_STARTED | S08 |
| WP06 预算/截止/输出包 | — | NOT_STARTED | S16/S17 |
| WP07 回归/质量对照/交审 | — | NOT_STARTED | 03 全节 |

## 各 WP 要点

**WP00**（`docs/closure/WP00_baseline.md`）：selection.py SHA 与包内
EXPECTED 一致；三合成探针复现（unjudged/低分0.01/invalid 带分均
交付）；入口清单含 round2-in-formal-cache 缺陷标记；BURST_ROUNDS
3-vs-2 迁移差异记录在案（生产切换待批准）。7 个应用级红探针。

**WP01**（`docs/closure/WP01_exit_scope.md`）：出站硬门（未判/
非 evaluated/非法分值[-1,1] 口径/版本失配拒交付；低分 evaluated
合法 rank_only；未校准一律 needs_validation，旧 deliver 确信态
删除）；words 与 raw-fallback 候选进入 judge 输入（S10 单出口最小
落地）；provider 不可用交付空+结构化 missing；重放核对
conversation_scope；round2 归 runtime 幂等集；upsert 保持
rejected/accepted 优先。测试基座：确定性注入 judge + autouse
重装（顺序无关）。**7 红探针全转绿。**

**WP02**（`docs/closure/WP02_profile_evidence.md`）：S15 显式外发
profile（五字段许可集 + event_excerpt_only 兼容 + 未知值 fail
closed）；按来源类型的逐字段外发过滤；query projection 指纹扩充
（semantic_query/负条件/exact_phrases/time axis/intent，四向 cache_key
验证）；excerpt 命中处取窗（anchors）。

## 关键出口证明（当前状态）

- 普通 recall（start/refine）：检索（v1.7 字段矩阵）→ 证据装配 →
  一层 Jev（event+words+raw-fallback 全入判）→ 出站硬门 → ≤3 交付
  needs_validation；provider 不可用=空交付+结构化说明（探针钉住）
- words 专项（words.recall/find_words）：不走 operation 重放，无
  阶段误过滤面；dense 补齐在 WP05
- raw：当前仅 words-证据不足 fallback 路径且已入判；Round2 完整
  门禁/事务化在 WP04
- known-ID 详情（get/open/source_ref）：保留，未受影响

## 测试

```text
pytest tests/unit -q                          492 passed, 1 skipped
pytest tests/acceptance tests/integration -q  126 passed
合计                                           618 passed / 0 failed
新增：closure 探针 7 + WP02 10；改写旧语义测试 12 处
（fusion/pack/jev05/jev09/jev10/rawx01 等，全部注明 S10 依据）
```

前台分批、隔离根、无网络、无真实 key。资源：单批 ≤24s。

## 未完成与需授权项

1. WP03-WP07 未开始（见上表）；其中 WP04 涉及 runtime schema 与
   Round2 门禁重建，WP05 涉及 words 派生索引 schema。
2. 生产数值门（未动）：BURST_ROUNDS 3→2 的生产切换、
   SEMANTIC_PROVIDER 启用、真实 Jev key 外发样本评测——均待
   用户单独批准。
3. LIVE_MODEL_EVALUATED=false：结构级 fake 证明代码如何处理分数，
   不证明真实 Jev/BGE 质量（03 验收三级分离）。

## 迁移/回滚

本三包无 schema 变更、无数据迁移；分支可整体放弃回 2e3f1e3 无损。
