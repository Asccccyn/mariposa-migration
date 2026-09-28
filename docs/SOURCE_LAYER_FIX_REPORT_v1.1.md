# Mariposa Source Layer 定点补修报告 v1.1

日期：2026-09-27　｜　实施：程知行（ZCode）　｜　依据：《定点补修指令 v1.1》与《复核证据 v1.1》（林石见）

## 0. 结论先行

- 复核的 **11 项实测缺口（SL-01—SL-11）全部修复并以失败测试先行验证**：新增
  `tests/unit/test_source_fixes_v11.py` 首跑 **41 failed / 8 passed**（红基线，
  复现全部缺口），修复后 **49 passed / 0 failed**。
- 资源边界：限额与有界化已实现并测拒绝路径；**GB 级大文件峰值实测未做**
  （不生成大文件、不压生产宿主），如实列为 not_tested。
- **受限原文补查链路（指令 §7）未接入**，列为 deferred 并附建议方案——
  不在无验收框架下改已审计的 Recall 模块。
- 全量回归（分批前台）：**unit 389 passed + 1 skipped；integration+acceptance
  156 passed**。既有 flaky `test_session01` 本轮三次分批均未触发（基线证据
  见 v1.0 报告；修复仍 deferred）。
- 声明：**未动生产库、未读真实聊天、未外发数据、未改 Recall/raw 层业务
  策略、未扩大原文权限**。

逐项状态与证据路径见同目录 `SOURCE_LAYER_FIX_MATRIX_v1.1.json`
（状态只用 verified / failed / not_tested / deferred，无 waived）。

## 1. 修复方法与快照

- 开工前核验工作区哈希与复核附录一致（importer 20d1367c… 等 6 文件），
  HEAD `cb88610` 未变——复核探针适用于本次修复对象。
- 流程：migration 15 + config 限额（基础设施）→ 失败测试（红基线 41）→
  六模块修复（json_stream / archive / claude / importer / query / binding）
  → 绿 → 既有测试语义适配 → 全量回归。
- 修后文件哈希清单在 MATRIX `code_state`（覆盖全部 untracked Source 文件）。

## 2. 数据库 migration #15（新迁移，不改写 #14）

`published`（发布门禁）、`content_hash`（消息内容身份）、
`source_conversation_snapshots` + `source_snapshot_members`（会话快照与
快照内 sequence）、`source_message_versions`（不可变消息版本，UNIQUE
provider+pid+content_hash）、`memory_source_bindings.start/end_content_hash`
（绑定固定证据版本）。纯新增，对既有数据无损。

**既有导入数据盘点**：生产库从未导入过 source 数据（Source 层尚未投产），
无需修复存量；#14/#15 将在生产首次启用时一次性应用。若未来需要对旧绑定
补固化 hash，方案先报告再执行——本轮未自动重写任何绑定。

## 3. 逐项修复摘要（详细旧行为/新行为/测试见 MATRIX）

| ID | 状态 | 修复要点 |
|---|---|---|
| SL-01 | verified | parse_failures>0 / 校验 problems / 归档 hash 不符 → 阻断 completed，批次 failed + blocking 明细 |
| SL-02 | verified | 一次固定快照（受控暂存→原子发布 payload-<sha16>）；解析只读归档字节；manifest 精确路径；zip 歧义拒绝 |
| SL-03 | verified | excerpt 只出自正文（token 位置映射回原文窗口）；默认路径无 evidence 回退；matched_fields 标注 |
| SL-04 | verified | 顶层 text 仅在 content 缺失/空时回退；tool-only/thinking-only 不晋升正文 |
| SL-05 | verified | code point 半开区间口径（响应内声明）；range 按口径裁切 text/excerpt/content_json；逆序/非整数偏移拒绝 |
| SL-06 | verified | parent 路径回溯校验；sibling 区间拒绝（SOURCE_RANGE_NOT_PATH）；off_path_messages 单列标注 |
| SL-07 | verified | content_hash + 不可变版本表 + 会话快照/快照成员；version_conflicts/sequence_conflicts 显式报告；当前行永不覆盖；元数据按快照留观察 |
| SL-08 | verified | FTS MATCH 并入主查询同集过滤；稳定排序（唯一 id 收尾）+ limit+1 真实 has_more；LIKE ESCAPE |
| SL-09 | verified | 严格 UTF-8/文法（尾逗号/尾随垃圾/NaN/坏字节拒绝）；全元素 schema 校验；空数组≠空会话≠非法元素 |
| SL-10 | verified | published 门禁：批次校验通过才发布；失败批次数据默认不可检索（诊断开关显式标注）；旧批不受新批失败影响 |
| SL-11 | verified | speaker 校验改 IS NOT（NULL 检出）；投影 hash 抽样；sample_checks 真实计算，删除写死断言 |
| 资源边界 | verified（拒绝路径）/ not_tested（大文件实测） | 限额常量（env 可覆盖）+ 有界查重（单会话集合+DB UNIQUE）+ reindex 游标分批 + range 预算 + 上传 413 + zip 限额 + 租约 |
| 并发认领 | verified | running 租约（默认 120 分钟）内 409；过期接管；接管前归档 hash 验证 |
| 绑定版本固定 | verified | 绑定记录 content_hash；读取 drift 显式标注；旧绑定 legacy 兼容 |

### SL-07 版本选择策略（指令 §4.1 要求明确）

当前展示版本 = **首次导入版本**；后续导出同 UUID 不同内容 → 不可变第二
版本入 `source_message_versions` + `version_conflicts` 计数，**不自动合并、
不自动切换**（新版本留档待人工/既有策略选择）。会话 title/updated_at 按
快照留每次观察。sequence 仅在快照内解释；跨快照序号冲突显式计数并出现在
导入响应中。

## 4. 勘误（对 v1.0 实施报告）

1. 「全项通过/十九条完成」结论撤回，以本报告与 MATRIX 状态为准。
2. v1.0 将按月/日历视图并入「查看器美化不扩大范围」的表述不准确：按月
   浏览/日历/时间轴是原指令 §10 列出的应保留能力，应记为**未交付功能**
   （deferred，见 MATRIX）。
3. v1.0 的「接入新 provider importer 零改动」不是已验证结论：
   `_insert_message` 仍调用 `claude_adapter.speaker_of`，新 provider 接入
   需将该依赖泛化到 adapter 协议——已记录，本轮未做（不追加第三家平台）。
4. v1.0 引用的「内存上界=单会话+1MB」在极端单会话接近整份文件时不成立；
   现以 SOURCE_MAX_ELEMENT_BYTES 显式上限 + 超限拒绝替代该表述（大文件
   峰值实测仍为 not_tested）。

## 5. 接入边界（指令 §7）：未接入，方案如下

现状：source.* 仅 owners；generic worker 403；旧 raw.* 与 raw fallback
未动——「新导入 Source → 授权补查」链路**无端到端证据**，本轮不接。

建议（待批准后另轮实施+验收）：`raw_recall.scoped_search` 增加 source
补查分支（published=1、human/assistant、同 scope/日期约束、channel=
"source"、speaker 用正式 speaker 字段），由现有 `RECALL_RAW_FALLBACK_
ENABLED` gate 控制，走共享 Recall Session 七动作隔离验收；周家明对
小模型的委托继续走现有后台筛选通道，不新增旁路。

## 6. 测试与回归证据

- 新增：`tests/unit/test_source_fixes_v11.py`（49 项，全绿；首跑 41 红
  基线在实施会话 transcript 存证）。全部合成数据，受 conftest 测试根
  保险丝保护。
- 适配：`test_source_layer.py` / `test_source_api.py` 按新语义更新
  （裁切断言、异常类型、monkeypatch 签名；变更点在 MATRIX）。
- 全量：`tests/unit` 389 passed + 1 skipped（真实导出可选，默认 skip）；
  `tests/integration tests/acceptance` 156 passed。分批前台执行，遵守
  AGENTS.md（超时、最小范围、无并发全量）。

## 7. 边界声明

未动生产数据库（全部在临时隔离根）；未读取任何真实聊天正文；未向外部
发送任何数据；未修改 Memory/Recall/raw 层业务策略；未扩大原文权限
（owners 集合不变，worker/linshijian 无 source 权限）；未删除任何原始
JSON；未执行 reset/clean/全工作区 stash。

复验入口见 MATRIX `how_to_verify`。
