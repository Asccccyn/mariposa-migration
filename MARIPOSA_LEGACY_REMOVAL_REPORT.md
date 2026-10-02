# Mariposa 旧模块退役报告（2026-10-01）

Baseline：`db6fa36`（+ D01 letters @ `610317c`，含隔壁窗口完成的
letters→独立项目拆分与 deletion/ 收窄）。

## Deleted modules（D01–D13 全部执行）

| D | 模块 | 处置 |
|---|---|---|
| 01 | Letter | 已拆独立项目（deletion/ 收窄保留 memory 删除申请）；timed-lock/并发/幂等 findings = RETIRED_BY_MODULE_REMOVAL |
| 02 | Home/Self/Diary | content/ 整删；旧 Self 与 I 严格区分，I 未动 |
| 03 | Calendar | calendar/ 整删（资源日期字段 memory_date/plan dates 保留） |
| 04 | Reminder | reminders/ 整删；maintenance 只摘 reminders_fire_due，其余保留 |
| 05 | Moments | moments/ 整删（media 保留） |
| 06 | Sticker | handlers+stickers 表删（表情包职责归 estómago；media 保留） |
| 07 | Quotes | quotes/ 整删；不迁移 our_words（生产 0 行，无迁移需求） |
| 08 | semantic_review | 随 quotes/ 删；未改造为 Jev |
| 09 | Candidate/Review/Proposal | v1_compat 逐 handler 删（deletion.get/restore、plan.* 等现行薄实现保留）；workspace.candidates/memory.candidates/memory.emotions 退场 |
| 10 | Raw binding review 残链 | 随 D13 整删（不重实现审批链） |
| 11 | Meaning | meanings_* 三函数+注册+表删；禁检语义不变（本就禁检） |
| 12 | Workspace Tasks/Lease | **DELETE**——静态依赖检查：调用方仅 v1_compat 候选流（D09）与 raw review（D10），无现行核心依赖；work_items/proposal_versions/worker_runs/workspace_task_leases 全退 |
| 13 | Legacy Raw | **DELETE（情形 A）**——recall 的 import 为死代码、bootstrap 仅一条文案（已改指 Source）、words._source_ref_valid 旧前缀退役（一律 invalid，生产 0 行无影响）；新 Source 层与 Round2 原文路径未动 |

## Retired public capabilities（Registry/MCP/HTTP 全退）

letter.×4、home.×2、self.×6、diary.×6、calendar.×5、reminder.×3、
moments.×4(+1)、sticker.×3(+1)、memory.quotes.×6、workspace.quotes.×2、
memory.meanings.×3、raw.×8(+2两阶段)、workspace.tasks.×3、
workspace.runs.×2、workspace.candidates.×1、memory.candidates.×3、
memory.emotions.set。contracts/capabilities.v1.json 同步清除。
**Registry 终态 145 项全现行**（负向测试断言零残留）。

## Modified shared files

registry.py（注册/handler/import 清理+v1_compat 装配接线）、
v1_compat.py（薄实现逐 handler 裁剪；plan.*/deletion.* 保留）、
input_schemas.py、maintenance/service.py、time_context/service.py
（最近用户消息改查 Source 层）、deletion/service.py（表清单）、
bootstrap/service.py（note 改指 source.message.get）、migration.py
（letter/self/diary 桶 out_of_scope）、schema.py、conftest.py、
web/index.html（日历/她的话 UI 删）、apps/web（Calendar/Quotes/
Content/Workspace 四页删）、contracts/capabilities.v1.json。

## Schema closure

- fresh 正式库不再创建 19 张退役表（基础 DDL 移除）；
  formal migration 25 / workspace migration 6 统一 DROP（生产库
  2026-10-01 只读取证全部 0 行，无 LEGACY_TABLE_DATA_PRESENT）；
  letters/letter_versions 由 migration 24（隔壁）DROP。
- 迁移版本无重复；已部署库按版本号跳过已应用项，安全。

## Tests

- 删除退役专属测试（letters/content/raw 回退/RawX/quotes/semantic
  review/sticker/moments/reminder/calendar/候选审批/meaning/任务租约）；
  现行核心测试中"旧 raw 未写入"类断言随表退役移除（语义恒真）。
- 新增 `tests/unit/test_retired_capabilities_negative.py` 7 项：
  Registry/MCP 零残留、invoke 拒绝、fresh 库无退役表（formal+
  workspace）、模块不可导入、Relation 冻结区回归。
- **全量：550 unit（549 passed + 1 skipped，collect 口径=执行口径）
  + 100 acceptance/integration + 4 real-model smoke（本机跑，复审
  口径不含）= 653 passed / 1 skipped / 0 failed。**
- compileall 通过；Relation/Recall/Plan/I/Source 结构回归全绿。

## Media（附录：剩余 callers）

media.upload.prepare/finalize/list/get + web 媒体库页 + Source 附件
未来路径。moments/sticker 引用已清。

## Remaining blockers

无 BLOCKER。遗留说明：①怪目录 `D:\mariposa\runtime\models` 再现一次
（内容仅锁文件，已随本轮再次清理；根治属审计修复轮）；②web 静态页
JS 语法经手工裁剪——浏览器实测留前端轮；③`test_v2_plans.py` 仍是
空壳（0 收集）。
