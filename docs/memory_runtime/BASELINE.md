> **历史文件（2026-10-04 降级）**：本文件记录的是当时的核对/映射
> 快照，不再是现行语义。现行基线唯一入口是
> `docs/memory_runtime/CURRENT.md`（含"域→现行正本清单"）；
> 冲突一律以 CURRENT 为准，审计不得从本文件推导现行合同。

# 召回运行时基线（BASELINE）

日期：2026-09-26｜核对者：GLM/程知行｜性质：P0 冻结证据，只读核对结果

## 现场状态

- `BASELINE_HEAD = 29b5e4fbad8a1fc5aa99ff1251f32fcf6424cf28`（与 v1.4 §1.1 记录一致）
- 工作树原有未提交改动：`tests/acceptance/test_linshijian_review.py`
  ——内容为把硬编码 Windows venv 路径 `.venv\Scripts\python` 改为
  `sys.executable`（Mac 兼容修复）。**归属：先前 Mac 迁移会话遗留，
  合理，本批保留并沿用该方向。**
- 仓库根字面目录 `D:\mariposa\runtime\models/`（91MB）：HuggingFace
  模型缓存（Qdrant/bge-small-zh-v1.5）。**可重建缓存但本批不删除、
  不下载**（v1.4：不自动下载权重）。生产启用语义检索时经
  `FASTEMBED_CACHE_PATH` 或数据根 `derived/` 指向；当前留在原地，
  待乔生决定迁移或清理。
- `config.py` 原默认 `MARIPOSA_ROOT=D:\mariposa`（Windows 路径）：
  本批已改为非 Windows 必须显式设置（fail-fast，OPS-RECALL-01）；
  Windows 保留旧默认不破坏既有生产。
- 既有测试保护：conftest B06 测试根保险丝（realpath 白名单 fail-closed）
  核对有效；AGENTS.md 资源约束（前台/分批/最小范围）在本批遵守。
  Windows test-guard 脚本在 Mac 无等效物——本批以"前台+超时+指定文件"
  约束代替，Mac 常驻 guard 待另建（NEXT 记录）。

## 与 v1.3/v2.0.1 的关系（本批前）

- v2.0.1（0922）实现存储/遗忘/审查/身份/自然日；无任何召回运行时
  （Recall Session、words 通道、证据分级、指令权限标注零实现）。
- v1.3（0926）为召回语义正本；v1.4 为施工方案。本批 = v1.4 落地。
- 旧矩阵 `docs/spec_v2/mariposa_v2.0.1_需求与验收矩阵.json` 的 109 条
  状态仍为 NOT_EXECUTED（与 PROGRESS 自报 289 绿的历史不一致是既有
  文档债，本批不批量刷绿，见 PROGRESS 说明）。

## 语义冲突裁决记录（本批执行）

| 冲突点 | 裁决 | 依据 |
|---|---|---|
| v2.0.1「our_words 不索引、不检索」vs v1.3 §7.2 独立 words 通道 | 两者边界不同：v2.0.1 R06/V2-SEARCH-08 约束的是**普通召回**不混入话语；v1.3 新增的是**独立 words 通道**。隔离不变，通道新增 | v1.4 [P2] 明文 |
| `forgotten our_words recall` | 保持 `disabled / PENDING_OWNER_DECISION`，全链路无旁路 | v1.3 §23 / v1.4 §2.2-5 |
| estómago 角色 | 本仓库只落 Mariposa 侧（session 正本+ref/receipt 获取）；estómago 工程缺失，其宿主侧改动不在本批 | v1.4 §9.5 |
| §17 持久化分层 | 逻辑映射（runtime/recall/ 运行库新增）；生产物理目录迁移未做、单独审批 | v1.4 §10.1 |
