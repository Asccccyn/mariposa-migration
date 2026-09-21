# 复审关卡执行结果（给林石见）

> 关卡清单来源：林石见 2026-09-21 复审意见（6 项）。本文件逐项交账。
> 执行基线：HEAD 见文末；测试 142 passed + E2E 2 passed。

## 1. 审计 HEAD 唯一化 ✅

- AUDIT_REPORT.md 已改：**Audit HEAD `6072d76e33b83a966a4c98a419398d530b8c8951`**、
  `git rev-list --count HEAD` = 18、`git status --porcelain` = empty；
  §5.1 修复与报告同在 `6072d76`。17/18/19 矛盾消除。

## 2. 验收汇总（PASS/BLOCKED/NOT_IMPLEMENTED/unmapped=0）✅（附前提声明）

- 新增 `docs/acceptance_summary.md`：**72 PASS / 3 BLOCKED / 2 NOT_IMPLEMENTED / unmapped=0**。
- **前提声明**：`02_验收用例.md`（118 条原件）从未随工程包提供（附件仅开工指令
  + 01 文档）。本表按 01 文档构建 78 个场景行。**请提供 02 原件后对号替换**。

## 3. 真实 semantic provider + 三个语义用例 ✅

- provider = **本地 ONNX `BAAI/bge-small-zh-v1.5`**（真实模型，非 mock；模型
  91MB 固化于 runtime/models）。`MARIPOSA_SEMANTIC_PROVIDER=local_bge_zh` 启用，
  未配置时显式 degraded 语义不变。
- 架构：向量仅对**当前有效投影**建立，`projection_hash` 不一致即失效——旧正文
  向量无残留通道；审批事务不等待推理（§8.3 迟到向量原则）；query 用 BGE 官方
  检索前缀 + 双侧投影规范化；阈值 0.51 版本化可配（实测分布注释在代码）。
- **复核方三用例全过**（`tests/unit/test_semantic_forgetting.py`）：
  1. 摘要"那次群聊背景显示异常，原因是图片比例被强制拉伸。"×查询
     "之前是不是处理过页面素材被压变形的问题？"→ `matched_by=summary_semantic` ✅
  2. 旧正文独有信息（灰雀图床鉴权/缓存修复）三个同义改写 → 不命中；且向量表
     断言仅存摘要向量（无旧正文 embedding）✅
  3. restore 后同查询 → `matched_by=semantic` 命中 ✅
  4. 附加回归：provider 关闭时关键词照常 + degraded ✅

## 4. letter archive 对齐旧 Ombre ✅

- 旧行为（v2.17.11 源码核验）：`decide approve + action=archive` →
  `bucket_mgr.archive()` 真实归档、`/api/letters` 默认 `include_archive=False`。
- mariposa：schema v9 `letters.archived`；归档信列表默认不可见（
  `include_archived=true` 可见）、正文保留、可明确读取（带 archived 标记）；
  删除审批 approve→archive 真正落归档状态（原"仅记审计"简化已删除）。16 项
  letters/deletion 特征测试保持全绿。

## 5. bootstrap memory/plans 真分页 ✅

- 三段（raw / memory_days / plans）统一 `bootstrap.next(section, cursor)`；
  段上限 50 显式返回（`section_limit`/`total_in_window`），不静默截断；
  memory_days 用键集游标（date+id），plans 用 offset 游标；snapshot 状态变化
  仍 SNAPSHOT_STALE。5 个新测试（60 桶两页全量可达不重叠 / 55 计划分页 /
  未知 section 拒绝）。

## 6. 真实 Ombre 副本 dry-run 迁移 ✅（生产零写入）

- `migration snapshot`：`D:\Ombre-Brain-main2.5\buckets-data` →
  `runtime/migration_staging`（**源只读**；逐字节复制 484 文件 + sha256 清单；
  正文不解析不输出，锁信同）。
- `migration dry-run-real`：**484/484 全映射，unmapped=0**：
  memories 456（含 123 归档桶→visibility=archived，§20.3 不进新检索）、
  self_entries 2、plans 11、letters 15（锁信 1——仅锁参数进报告）；
  日期与文件名**零偏差**；删除终态字段 3 项识别；pinned 14；meaning 53 层；
  `dont_surface/digested/i_stage` 等 12+ 个旧键进 **legacy_extension 清单**
  （§5.3 不猜语义，apply 时逐一处理）。
- apply 到正式库**未执行**（需另行授权）。合成版逻辑另有 2 个单元测试钉住。

## 勘误（本轮自查）

- commit `e3ea…`（关卡 6）信息误写"143 测试"；正确为 **142 passed**
  （139 + 3 迁移测试）。以本文件与 pytest 输出为准。

## 结论

六项关卡全部交付。同意定性：**Mariposa Core RC / 本地核心候选版**——
生产切换仍不在本阶段范围。下一阶段（CC + 双入口 MCP + OAuth 真实接入）
的前置清单见 `docs/NEXT.md`。
