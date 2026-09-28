# Mariposa v1.7 审计修复报告（A01–A15 全项）

日期：2026-09-28　｜　实施：程知行（ZCode）　｜　依据：两份审计（林石见《MARIPOSA_V17_LOCAL_AUDIT》15 项 + 自审《AUDIT_REPORT_v1.7》F 项）合并清单

## 修复总览

| 项 | 内容 | 修复 commit | 复验 |
|---|---|---|---|
| A01/F4 | memory.update 复制 event_text/original_title/schema_version，仅改日期不清空 | 4fc8815 | 既有 update 测试绿 |
| A02/F7 | plans 终态移除 due_date 引用（500 修复） | 4fc8815 | 既有 plans 测试绿 |
| A03/F6 | 媒体字节端点 owners 校验（与 Registry 同权） | 4fc8815 | worker 已知 hash 403（审计复现场景，由媒体测试矩阵覆盖） |
| A06 | recollections.append schema 声明 keep_wide | 4fc8815 | schema 校验放行 |
| A07/F1 | reloplay 三处枚举同步 | 4fc8815 | reloplay hold 直写 ✓ |
| A08 | migration 19 重建分类 CHECK | 4fc8815 | fresh + 旧 DDL 升级双路径实测 ✓ |
| A04/F2 | 召回主线切换 v1.7：词法路=阶段过滤分字段检索 | 4fc8815 | WORD-01 系 REPLACE 为「WIDE 命中/CORE 不命中」3 处 |
| A05/F2 | 首轮完成自动签 ROUND1_COMPLETE；round2 gate 服务端事实 | 4fc8815 | gate 测试改验证自动回执 |
| F3 | round2 judge/授权/预算取服务端事实；find_words 尊重 words 开关 | 4fc8815 | handler 不再采信客户端布尔 |
| F5 | 分字段投影：update/our_words.append 同事务重建 + rebuild_index 纳入 field/words | 4fc8815 | 投影一致性测试 |
| A09 | 失败批次 UUID 接管：发布按本批快照成员（含旧 failed 行） | a1f15d2 | test_a09（接管发布+版本留档）✓ |
| A10 | verify 九项检查限定本批 | a1f15d2 | test_a10（坏日期不阻断无关导入）✓ |
| A11 | 分页 (sequence,id) 复合游标 + 整数兼容 | a1f15d2 | test_a11（[A,B]→[A,NEW,B] 不漏）✓ |
| A12 | metadata 落盘失败不改批次状态 | a1f15d2 | test_a12（OSError 注入仍 completed）✓ |
| A13 | migration 20 回填旧 completed 批次 published | a1f15d2 | 迁移幂等；content_hash 由重导接管回填 |
| A14 | 前端 hold 九分类下拉 + 传 categories | a1f15d2 | JS 语法 ✓；真实回调传参（审计法） |
| A15 | 迁移 apply 传分类 + CLI 入口顺序 | a1f15d2 | NameError 修复 |

## 回归

501 passed + 1 skipped（真实导出可选项默认跳过），分批前台（2026-09-28）。审计报告的 498 收集基数上净增 4 项审计复现测试 − 1 项退役。

## 语义变化声明（按 §12.2 REPLACE）

1. **WORD-01 系（3 处）**：旧「event 通道永不命中 our_words 独有词」→ 新「WIDE 命中（六入口含我们的话）、CORE 阶段过滤不命中」。这是 v1.7 §5.3 的核心语义，非缺陷。
2. **Round 2 回执**：由首轮执行自动签发（服务端事实），不再依赖手工 mark；故障/降级轮不签发。
3. **A09 接管语义**：同 UUID 内容变化时当前展示行保持首次导入版本、新内容进版本表留档（与 SL-07 版本策略一致）——不是"新内容覆盖"。

## 未修/遗留（如实）

- F8 文档漂移（CURRENT.md 仍 v1.4 语义）、F9 死表、F10 E2E（Windows 路径+退役用例）、F11 负向锁 9/20、F12 `_CURRENT` 全局主体并发窗口与 round1 N+1 —— 属文档/清理/性能项，未在本两批内
- F13 其余健壮性点（租约接管竞态 `_fail_batch` 无条件 UPDATE、聚合 published 口径、`_find_message_any` 无 provider 维度）——部分被 A09/A10 改动自然覆盖，其余待复核
- A13 的 content_hash 回填依赖同文件重导；从未重导的旧数据 hash 保持 NULL（绑定读取已兼容 NULL）

## 边界

未动生产库、未读真实聊天、未外发数据、未部署。三个 commit：2c9417b（候选）→ 4fc8815（批1）→ a1f15d2（批2），未 push。
