# Mariposa 召回闭环实施 · 最终交付（recall-closure-20260930-r2）

包版本：recall-closure-20260930-r1+r2（r2 的 S09 修订已实施）
实施人：程知行（GLM）
起始 HEAD：2e3f1e3；工作分支：recall-closure-20260930
生产源码／数据／配置：未改变（隔离分支与测试根）

## 状态分层

```text
IMPLEMENTED：WP00 / WP01 / WP02 / WP03 / WP04 / WP05 / WP06 + r2 S09
UNIT_TESTED：是（确定性 fake Jev/embedder 全程）
INTEGRATION_TESTED：部分（registry/HTTP/MCP 入口级含 parity；
  完整传输层矩阵未单跑）
LIVE_MODEL_EVALUATED：false（真实 Jev 未授权；真实 BGE 仅语料
  smoke——words/event 语义质量未评测，阈值均为工程初值）
DEPLOYED：false
```

## 工作包与提交

| WP | commit | 规则 | 状态 |
|---|---|---|---|
| WP00 基线+红探针 | c2a0ffa | S10/S11 | PASS |
| WP01 出口硬门+scope | f2bc9d7 | S04/S05/S10 | PASS |
| WP02 外发profile+指纹+取窗 | 483e6db | S09/S15/S16/S18 | PASS |
| r2 S09 多角色证据 | 7cb41e7 | r2 S09 | PASS |
| WP03 scoped BM25+dense | dad8f30 | S04/S05/S06/S07 | PASS |
| WP06 预算/输出包 | 3947413 | S16/S17 | PASS |
| WP04 Round2 门禁+事务 | 8fdb12a | S13/S14 | PASS |
| WP05 words dense+统一入口 | c5f8140 | S08 | PASS |
| WP07 回归/交审 | 本报告 | 03 | 部分（见下） |

## 各包要点（详见 docs/closure/WPxx_*.md 与 r2_S09_roles.md）

- **WP01**：出站硬门（未判/非法分[-1,1]/版本失配/invalid 拒交付；
  rank_only 语义）；words/raw 候选入判（单出口）；provider 不可用
  空交付+结构化；重放 scope 校验；round2 归 runtime；upsert 状态
  优先。
- **WP02+r2**：S15 逐字段外发 profile（未知值 fail closed）；
  query/candidate 指纹扩充；命中取窗；**CandidateEnvelope v2
  多角色段**（match_evidence/event_evidence/primary_evidence/
  title_cue；同段双标不复制；中秋成对反例）。
- **WP03**：scoped-stage-bm25-v1（统计只来自授权+阶段集合；
  N+1 MATCH 清除）；query-only semantic（停止 semantic_query
  静默回填）；向量绑语料 generation；pending 外露。
- **WP06**：burst 真实计数（WHERE burst_no=?）；输出预算实际
  计算（24576/4000/3/600 逐层裁剪）；burst=2 隔离验收（生产值
  未动，待批）。
- **WP04**：Round1 成功回执（migration 6：plan/scope/policy/
  覆盖/judge 统计绑定）；round2 六条件服务端门禁（回执+事实支持
  理由+外发授权+预算+防重跑）；raw 候选过同一层 Jev+selection；
  commit-at-end 单事务（kind='raw'）。
- **WP05**：words 专项向量层（word_id 父单位、独立空间、词条
  指纹失效、独立阈值）；BM25+dense 同通道 RRF；三入口统一
  session 化（预算/回执/Jev/≤3）。

## 关键出口证明

| 入口 | 链路 |
|---|---|
| recall.start/refine | 字段矩阵检索→scoped BM25+dense→RRF→多角色证据→一层 Jev→硬门→≤3 needs_validation |
| recall.round2 | 服务端 plan+六条件门禁→source 层深搜→同一 Jev+硬门→单事务 kind='raw' |
| words.recall / find_words | 统一 session 化主链（words BM25+dense RRF→Jev→≤3） |
| memory.search/recall | v1.7 字段矩阵（2026-09-30 裁定） |
| known-ID get/open/source_ref | 合法详情，不重复判相关 |

## 测试

```text
pytest tests/unit -q                          518 passed, 1 skipped
pytest tests/acceptance tests/integration -q  126 passed
合计                                           644 passed / 0 failed
新增 closure 套件：WP00 7 + WP02 10 + r2 3 + WP03 7 + WP04 7 +
WP05 5 + WP06 4 = 43；旧语义断言迁移 20+ 处（逐处注明规则依据）
```

## 数据与迁移

- runtime migration 5/6（operation 行清理；round1_receipts 表 +
  rounds.kind）；formal migration 23（Source lease_token）。
  全部版本化、可回滚（分支整体放弃无损）。
- 旧库存量：升级后需一次 rebuild_index（field 投影+向量重嵌，
  generation 失效自动触发）；旧 session 无 Round1 回执 → round2
  按"不可证明则不可升级"拒绝（不伪造已判事实）。
- **burst 3→2 生产切换**：config 默认未动（隔离验收按 2）——
  待用户批准后单独执行。
- 旧 embedding/judge cache：generation/指纹失配自动失效重算，
  不改 hash 伪装已重算。

## 未完成与需授权项

1. WP07 剩余：同查询基线/修后候选对照（Hit@K/Recall@K 消融）
   与端到端 p50/p95 资源画像——需固定数据快照，建议交审时与
   真实模型评测一并执行；
2. 生产门（全部待用户批准）：SEMANTIC_PROVIDER 启用（含 words
   warmup）、真实 Jev key 与私人语料外发样本评测、burst 生产值、
   部署。
3. 长词条 token 分段、round2 S16 全局 deadline——WP07/后续批次。
