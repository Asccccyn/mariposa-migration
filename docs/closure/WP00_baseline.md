# WP00 · 冻结基线与负向探针（recall-closure-20260930）

- 分支：`recall-closure-20260930`（自 `fix/v17-audit-20260929` 的
  `2e3f1e3` 分出；对参考 HEAD `4b8b34f` 的差异映射见下）
- 解释器：仓库 `.venv/bin/python` 3.12.14 (arm64)
- 代码根：`/Users/zhoujiaming/Projects/mariposa`；测试根：临时隔离根
  （conftest fail-closed 保险丝生效）
- 未读取生产 .env／真实 key；未启动服务；未联网

## 与参考 HEAD 4b8b34f 的差异映射（保留已修内容）

4b8b34f 之上为本会话 2026-09-30 三裁定执行（6152908 / 7dfcf1d /
e8d9105 / 2e3f1e3）：search/recall 切 v1.7 字段矩阵、why/meaning 退出
投影、mood 裁定落地、dense 启用清单。这些与施工包语义一致
（S02/S03/S07 方向相同），全部保留，不 reset。

## 基线核验（inspect_baseline.py）

`selection.py` SHA-256 = `c3c9fc4ecbfc99cc30b983cfc9c70a131fc2873aa7119c3577f1bc26e67c486a`
（与包内 EXPECTED 一致）。合成探针复现包内三项观察：

```text
unjudged                       → delivered=1 needs_validation
low_score_0.01                 → delivered=1 needs_validation
invalid_with_numeric_score_0.99 → delivered=1 needs_validation
```

按 S10/S11 定性：unjudged 与 invalid(带数字分) 交付 = 出站硬门缺失
（WP01 修复目标）；低分 0.01（合法 evaluated）按分排序交付
needs_validation 是 S11 允许行为（rank_only_until_calibrated），本轮
以正向钉子固定语义而非删除。

## 裸进程开关状态（未读生产 env）

```text
RECALL_RUNTIME_ENABLED=False  RECALL_WORDS_ENABLED=False
SEMANTIC_PROVIDER=''          RECALL_RAW_FALLBACK_ENABLED=False
RECALL_DELIVERY_LIMIT=3  BURST_ROUNDS=3  BURSTS_MAX=3
JUDGE_CAP=40  BATCH=12  TIMEOUT_MS=5000  ROUND_DEADLINE_MS=20000
```

S17 迁移差异确认：现行 BURST_ROUNDS=3，原 v1.7 为 2。本批新隔离
会话按 2 验收；生产数值切换与旧会话处理在迁移报告单独呈报，未经
批准不改生产值。

## 对外入口清单（WP00 模板，逐入口归属在 WP01/WP04 补全）

| 入口 | 注册 | 幂等归属 | 出口类型 |
|---|---|---|---|
| memory.recall.start/refine/reject/accept/navigate/status/close | ✓ | runtime | 未知候选搜索→judge→≤3 |
| memory.recall.round2 | ✓ (write=True) | **formal（缺陷：应 runtime）** | raw 二轮（WP04 重做） |
| memory.words.recall / memory.words.get | ✓ | runtime / formal-read | words 专项 |
| memory.find_words | ✓ | formal-read（不走 operation） | words 专项 |
| memory.search / memory.recall | ✓ | formal-read | 已切 v1.7 字段矩阵（2026-09-30 裁定） |
| source.search / raw.search / source.range.open / source.memory.open | ✓ | formal-read | 原文访问（A/B/C 类 S12 归属 WP01/WP04） |
| 详情 get/open（memory.get、raw.read、source.message.get 等） | ✓ | formal-read | known-ID 合法详情 |
| memory.context.validate | ✓ | runtime | 校验 |

## WP00 应用级负向探针

`tests/unit/test_closure_wp00_probes.py`：selection 对 unjudged /
invalid(带数字分) 的交付断言（当前红，WP01 修复后转绿）；低分
evaluated 的 rank_only 交付正向钉子；provider 全失败不返回粗正文。
