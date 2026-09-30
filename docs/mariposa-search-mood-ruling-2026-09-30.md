# 检索入口与心情字段裁定 · 执行报告（2026-09-30）

依据：《Mariposa 本轮语义修正与工程修改说明》（江乔生 2026-09-30
裁定全文，含十六-§11 检查清单）。分支 `fix/v17-audit-20260929`，
前序 HEAD `4b8b34f`。

## 提交

```text
6152908 fix(search): legacy entries route to v1.7 field matrix; forbidden
        fields exit all projections            （裁定§一 + §二-1/2/3）
7dfcf1d feat(mood): final ruling semantics — tags filter, note never
        recalls, lightweight append/revise    （裁定§三—§十）
e8d9105 test(dense): pre-enablement checklist — legacy vector
        invalidation + real-model warmup/smoke（裁定§二-4/5/16）
```

全量测试：unit 475 + acceptance/integration 126 = **601 passed /
1 skipped / 0 failed**（前台分批，隔离根，含真实 bge-small-zh-v1.5
本机权重的模型级 smoke，无联网、无下载）。

## §一 memory.search / memory.recall（接口兼容，语义切换）

- 兼容面：能力名、参数、返回结构（hits 字段不变，matched_fields
  为新增字段）全部保留；
- 语义面：关键词命中底座从旧整桶投影（search_fts，含 why/meaning）
  切换到**分字段投影（field_fts）+ 逐桶当前阶段过滤**——与 v1.7
  主链同一字段矩阵（WIDE 6 / MID 5 / CORE 4）。title/words 在
  WIDE/MID 经旧入口合法命中（SEARCH-06/08 探针按新语义更新），
  CORE 后退出；
- 为什么/含义层（why/meaning）彻底退出一切 searchable projection：
  `rebuild_full_projection` 与 v1 写入路径的 search_text 收窄为纯
  事件正文（与 whitelist_body 一致）；meaning 仍可经
  meanings.list/versions.read 显式读取，但永不因文本匹配触发召回；
- 存量升级说明：旧库存量桶的 field 投影如从未构建，升级后需执行
  一次 `maintenance.rebuild_index`（等价 field rebuild）后进入新
  底座；新写入（v1/v2）已自动构建。v1 存量桶（无 held_at）阶段
  事实缺失时保守按最小允许集（仅事件正文）处理，不猜测宽松阶段。

## §二 dense / local_bge_zh 启用前清单（全部完成）

1. projection 符合 v1.7：语料=whitelist_body（事件正文）✓
2. 废弃字段不经 embedding 回归：钉子测试（why/meaning/mood 关键词
   四通道零命中）✓
3. 旧 embedding 失效：投影内容变化（正文更新）→ search_text_hash
   前进 → 旧 projection_hash 向量不被采纳、按新语料重嵌（结构级
   测试钉住）；meaning 追加不再影响投影本身即裁定生效的证明 ✓
4. 真实模型 warmup：本机既有权重（`D:\mariposa\runtime\models`，
   Qdrant/bge-small-zh-v1.5）加载并全量建向量，无下载 ✓
5. 最小 smoke（真实模型，三条全过）：A 同义表达召回事件正文且卡带
   真实版本；B 禁检来源关键词不触发；C provider 未配置显式
   unavailable + degraded，无任何旧整投影回退 ✓

**生产切换 SEMANTIC_PROVIDER=local_bge_zh 仍是单独的手动决定**，
本报告仅为启用条件就绪的证据。

## §三—§十 心情字段（mood_tags / mood_note）

- 结构：仅 `mood_tags`（结构化标签）+ `mood_note`（一段自由文字，
  不拆多字段、无模板）；
- 检索（硬语义）：mood_note 不进 BM25/dense/RRF/一二轮 searchable
  projection（钉子）；桶因合法字段被召回后 mood 可经 memory.get
  展示；mood_tags = filterable / visible / non-ranking（结构化筛选
  通道沿用 memory.recall filters，非文本打分字段，钉子验证标签词
  不作为自由文本命中）；
- 非原文：memory.get 的 mood 输出统一带 `is_source_text: false`；
- 时间语义：`mood_written_at`（=captured_at）+ event_date 表达
  "后补的当时心情"，不自动挪进回忆；
- 写入：新增 `memory.mood.write`（仅 jiaming；upsert 覆盖、旧值进
  审计；note=null 合法；无窗口服务器/守护进程/不可变锁）；
  hold 时的当场心情校验（contemporaneous）不变，后补走
  mood.write；
- 默认：历史原文导入不自动生成 mood（无此路径，确认）；
  evidence_state 复用 'contemporaneous'（作者声称的当时状态，
  schema CHECK 值域未动）。

## §十一 十六条逐项对照

| # | 检查点 | 结果 |
|---|---|---|
| 1 | 旧入口仍走 v1.4 路径？ | 修复前是；已切换 |
| 2 | 统一路由 v1.7 | ✓ 批次 F |
| 3 | why/meaning/mood/recall 字段退出投影 | ✓（含 v1 写入路径） |
| 4 | BM25 projection | ✓ 分字段投影；scope 内 IDF 归 closure 包 WP03 |
| 5 | dense projection | ✓ whitelist_body |
| 6 | 旧 embedding/cache 携带废弃字段 | ✓ hash 绑定自动失效（测试钉住） |
| 7 | mood_tags 结构化 filter | ✓ 钉子 |
| 8 | mood_note 不进 BM25/dense/RRF | ✓ 钉子 |
| 9 | 命中后可展示 mood | ✓ memory.get |
| 10 | 非原文 provenance | ✓ is_source_text=false |
| 11 | 历史导入不自动 mood | ✓ 无此路径 |
| 12 | 不新增 mood/window 服务 | ✓ |
| 13 | 不加不可变锁 | ✓ mood.write 可覆盖 |
| 14 | 保留修正能力 | ✓ memory.mood.write |
| 15 | mood_note=null 合法 | ✓ 测试覆盖 |
| 16 | dense 启用前检查 | ✓ 本报告 §二；生产切换待手动执行 |

## 行为变化清单（调用方可见）

- memory.search/memory.recall：why/meaning 命中消失（裁定预期）；
  WIDE/MID 阶段新增 title/words 合法命中；CORE 行为不变（仅正文）；
- memory.get：mood 输出新增 is_source_text / mood_written_at 字段
  （向后兼容新增）；
- 新能力：memory.mood.write（jiaming）。
