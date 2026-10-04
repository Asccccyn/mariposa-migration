# Mariposa 全量审计 · 2026-10-03

审计结论：**当前仍有实现语义不符，也确有不能有效检测回归的绿测。现有测试并非全绿。** 本轮确认 23 项实现、接口或运维问题，按影响分为 7 项 P1、16 项 P2；另单列测试失效和待裁定合同，不把它们混入实现缺陷计数。

本轮只新增审计材料，没有修应用实现、修改既有测试、启用生产开关或读写生产数据。

## 基线和范围

- 本地 `main` 与远端 `HEAD/main` 均为 `b484908cebce23d54c039249386cba9b37f69bfa`；开始审计时工作树干净。
- 现行语义以 [CURRENT](../memory_runtime/CURRENT.md) 的 v1.7 为准，叠加 9 月 30 日检索/心情裁定、Recall closure 的 r2/S09、幂等与最新接续审计修复，以及现行 Relation/Deletion 合同。
- 旧 v2.0.1 的遗忘、压缩、摘要和在线 restore 已退役。本文引用旧规格的存储、自然日、Bootstrap 等条款，仅用于尚未被后续裁定覆盖的部分。
- 仓库未包含 CURRENT 引用的完整 v1.7/v1.4 上游执行包；无法直接核定适用范围的发现放在“待裁定”，没有当成确定缺陷。
- 六域逐文件检查：Recall/session/预算；检索/Jev/words；Memory/keep/views/Bootstrap/Plan/I/时间；五域关系/纠错/删除/身份/审计；Source/Media/存储/迁移/维护；HTTP/MCP/schema/合同/两套 UI/全部测试。

P1 表示应优先修复的授权、关键流程、数据留底、导入或恢复问题；P2 表示范围、时间、接口、降级或运行可靠性问题。以下均有源码依据与合成反例；涉及真实 Jev 的复现使用实际适配器和 fake HTTP 响应，只证明代码路径，不宣称真实模型已经误判。

## 测试实跑结果

唯一收集 **828** 个用例，互斥分批执行，遇到 `-x` 停止后按 nodeid 补完尚未执行的用例：

| 状态 | 数量 |
|---|---:|
| 通过 | 814 |
| 失败 | 5 |
| 跳过 | 4 |
| 未执行的真实模型用例 | 5 |
| 合计 | 828 |

全部使用隔离系统临时目录、前台子进程、60/120 秒显式超时、`-x -q`、关闭 pytest cache 和字节码写入。未加载/下载真实模型，未联网。真实模型排除项为验收 `TestRET` 两项和 real-model smoke 三项；smoke 文件内的结构级旧向量失效用例已执行。没有把重复复验计入通过数。

React 本地 `node_modules` 不存在，本轮没有运行构建或 Playwright；已静态检查两套 UI，并以 HTTP/原测试 AST 注入反例核实相关问题。151 项能力合同与 Registry 的名称、权限、write、idempotent 一致。

原始分批日志目录、五个失败 nodeid 与统计见 [RESULTS.json](RESULTS.json)。

## 应优先修复的七项

### F01 · P1 · 查看票据混用版本维度，更新后的桶无法再写回忆或留

位置：`memory/recollections.py:64`、`memory/views.py:36`、`memory/service.py:36`、`memory/extras.py:105`（均在 `backend/mariposa/` 下）。

`memory.open` 签发 `representation_state`，内容更新只递增 `current_version_no`，而回忆追加把两者直接比较。复现：hold → update 到 v2 → **重新** open+confirm → append/keep，仍被拒绝。新打开返回 `version=2 / representation_version=1`；再次重开也无法修复。反向同根问题：v1 打开未确认 → 内容更新 v2 → 旧票据仍可 confirm，给当前桶写明开回温事实。

违反当前 VIEW 的内容版本绑定与 CURRENT §2/§5。`test_audit_cont_p1_p2.py:102` 只测旧票据追加被拒，缺少“重开后应成功”和“旧未确认票据不能确认新内容”。应统一票据所绑定的版本事实，同时补这对正负用例。

### F02 · P1 · words 开关关闭后，旧结果仍能带话语正文重放

位置：`recall/service.py:1456`、`:1566`。

重放只检查 runtime/raw 开关，`intent=find_words` 分支直接保留卡片。启用 words 保存一张卡 → 关闭 `RECALL_WORDS_ENABLED` → fresh start 为 0 卡，而旧 operation 重放仍返回 1 卡和正文。违反 CURRENT §4“开关无例外”；CB-010 只覆盖 Raw 撤权重放。应对 fresh、continuation、operation replay 统一执行当前通道开关。

### F03 · P1 · accept 最终事务不重验状态，可覆盖并发 close 的终态

位置：`recall/service.py:1053`、`:1064`。

允许动作检查发生在锁外；终态写入仅按 revision，而 close 不推进 revision。合法交错：accept 读 ACTIVE → close 提交 CANCELLED → accept(close=True) 提交 RESOLVED。已实际完成两次事务复现。违反终态不可再执行动作的状态机约束。

CB-009 只补 refine/round2。reject/navigate/close 也存在最终事务未统一复查的遗漏面，本轮直接证明的是 accept 覆盖终态；不将其余路径未经复现的具体后果当成确定结论。应在动作最终写锁内统一重验状态、revision 和归属。

### F04 · P1 · 心情修改覆盖后，旧正文没有任何留底

位置：`memory/service.py:468`。

读取旧 mood 后直接 DELETE；audit/outbox 只存 `replaced_previous`、新 tags 和 `note_present`。写一个独有旧 note → 覆写 null，旧 note 在 mood、memory versions、audit、outbox 中均为 0 命中。9 月 30 日裁定及该函数自身明确“旧值进审计”，实际永久丢失。

`test_v17_mood_ruling.py:124` 的注释声称“覆盖+留审计”，断言却只查新值和时间。应在同事务保存被替换的旧 note/tags 及必要作者时间信息，再验证旧值可审计读取。

### F05 · P1 · Jev 只看到标题，也能放行未经主体判断的事件正文

位置：`retrieval/judges/typesafe_jev.py:90`、`:322`、`:371`；调用链 `recall/service.py:403` → `retrieval/selection.py:38` → `recall/service.py:601`。

合法 `ALLOWED_DATA=title_cue` 会移除 event 段、保留 title 段。当前硬门只检查 `segments` 是否非空。公开 Registry 复现：标题“中秋约会”、正文“当晚在山里修电路”，实际适配器请求只包含标题；fake HTTP 返回 evaluated/0.9 后，公开响应仍交付带 `authored_event` 正文的卡。

违反 [r2 S09](../closure/r2_S09_roles.md)“标题不能替代事件主体”。`test_closure_reaudit_fixes.py:283` 只测所有段都剥空，没有测“标题仍在、必要主体已剥除”。应按候选类型检查必要证据角色：普通事件必须含 event_evidence，words/raw 必须含 primary_evidence；许可不足时明确 unavailable。

此项是可达配置下的代码门缺失，不表示生产当前启用了该 profile，也不表示真实 Jev 一定会给高分。

### F06 · P1 · 失败导入清场误删旧空会话，触发 FK 后批次卡在 running

位置：`source/importer.py:917`、`:948`。

先成功导入合法空会话，有不可变 snapshot 引用；再导入另一会话的 `[有效消息, 42]`。失败清场会 DELETE **全库**无消息会话，碰到此前空会话的 FK，抛 `IntegrityError`。新失败批次仍为 running，新消息残留 1 条，重试又受 lease 阻挡。

违反文件内记录的 9 月 29 日裁定“失败不留下任何会话消息数据”。现有失败清场测试没有“历史成功空会话 + 后续失败”组合。应限定本批清场，并保护合法历史会话和版本快照。

### F07 · P1 · 备份不含 Raw Archive 和媒体对象，verify 仍报成功

位置：`storage.py:34`、`:61`。

备份只有 formal/workspace 两库及 manifest。合成原文导入和媒体上传后备份，再在隔离目录删除原母本/对象，`restore_verify` 仍 `ok=true`；媒体读取 NOT_FOUND、archive verify 失败。库中引用不能恢复对应字节。

继承的存储合同要求对象清单和校验 hash；CURRENT §6 的原文母本仍为证据来源。`test_migration_storage.py:86` 仅检查两库，甚至将数据库集合固定为 formal/workspace。应明确并实现恢复集：数据库、被引用的母本及媒体对象，校验字节与身份；仅验证数据库时不能声称完整恢复可用。本轮没有执行任何实际生产恢复。

## 其他确定问题

| ID / 级别 | 位置 | 触发与影响 | 必须补的验证 |
|---|---|---|---|
| F08 / P2 | `relations/routing.py:128,211,219,233`；`source/binding.py:123` | Source 反查丢字符偏移；半开区间用 `<=` 导致邻接也算相交；跨消息只查 sequence；anchor 未绑定 conversation；binding 反查还将 parent 路径外 sibling 算引用。公开原样续查和完整端点查询均会多返回关系。 | `[0,4)` vs `[4,8)`、跨消息起止偏移、不同会话同 sequence、sibling、返回 other 原样续查。现有 RA-021 只有有间隔的同消息区间。 |
| F09 / P2 | `deletion/service.py:105`；`retrieval/projection.py:110` | 两条物理删除共用清理路径，却遗漏 `memory_embeddings/word_embeddings`。公开删除后 memories=0、两向量表各残留 1。当前 live JOIN 通常阻止命中，但未满足派生索引同事务真删除。 | 用合成向量建两表，验证 approve/直删均清空相应向量与 word 行；不加载模型。 |
| F10 / P2 | `source/json_stream.py:100` | “单元素字节限额”实际计算 Unicode 字符且包含缓冲中后续元素。限额400接受490字节元素；限额100反而拒绝5个各42字节合法元素。 | 多字节中文/emoji；同一读取块中多个小元素；跨块边界；比较当前元素 UTF-8 字节数。 |
| F11 / P2 | `storage.py:29,57,150` | backup 成功打印 ok=true，却返回不含 ok 的 manifest，CLI固定 exit1。秒级目录名还使同秒备份复用同目录。 | 调用 main/CLI验证退出码；连续两次备份必须有独立、不可互相覆盖的结果。 |
| F12 / P2 | `storage.py:115`；`schema.py:1082` | verify只验表名，workspace只要求meta表存在。用formal备份替换workspace并重算合法hash后verify通过，而启动 identity 校验拒绝 formal_v1 冒充workspace_v1。 | 两库互换、meta错误值、空meta；复用启动时身份不变量。 |
| F13 / P2 | `plans/service.py:254` | 临近计划用 `anchor[:10]`。业务今天10/3，UTC `10/6 18:00Z`其实上海10/7（第4天），仍纳入0..3日窗口。 | UTC/不同offset跨上海日；不能只测+08:00字符串。 |
| F14 / P2 | `time_context/service.py:43` | 上次用户联系的Source查询缺 `published=1`；导入校验前未发布消息能给出contact时间和coverage=complete，随后失败清场又消失。 | 导入中、失败批次、完成批次的时间事实可见性。 |
| F15 / P2 | `time_context/service.py:44,49` | SQL MAX/Python max按字符串比较时间。`20:00+08`（12UTC）错误胜过同日`15:00Z`（15UTC）。 | 混合offset的已发布消息及不同联系来源；规范化瞬时时间比较。 |
| F16 / P2 | `bootstrap/service.py:272,301,306,399` | 单条3万字I全文返回，estimated_tokens=20000>16000；warning声称分节续取，但所有cursor为空、I无continuation。软预算本身不是硬拒绝合同，缺的是承诺的正文分页。 | 单条长I/Plan/mood分节续取，而非只测50条数量分页。 |
| F17 / P2 | `retrieval/query_plan.py:30`；`retrieval/words.py:191,234` | term“小路灯”编成三个词项AND，丢顺序与连续性；公开找话对“灯在小路旁”交付1，改exact_phrases才为0。event已修的组内连续规则未同步words。 | words乱序/间隔反例及多term组间OR；真实查询编译路径。 |
| F18 / P2 | `retrieval/judges/typesafe_jev.py:95,255` | Jev query投影与缓存键遗漏lexical_terms；只把查询词“海边”改“山里”，仍cache_hits=1/request_count=0。runtime query_fp不能补偿供应商内部缓存身份。 | original_request不变、只改词法目标；精排输入与缓存键必须同步变化。 |
| F19 / P2 | `retrieval/judges/typesafe_jev.py:157,575,584` | HTTP read中途抛IncompleteRead未转JudgeUnavailable；实际adapter+fake断流令start整轮异常，无结构化降级。原事务会回滚，没有误称部分提交。 | fake response.read断流；使用实际适配器，不只让fake provider直接返回unavailable。 |
| F20 / P2 | `recall/service.py:784`；`recall/budget.py:22` | 成功包先preview增加rounds_used，但剩余轮数查询未提交DB。start响应1/3，紧接status1/2，剩余额度多报一轮。持久化预算硬门仍有效。 | 成功响应与提交后status一致；首次、补查、跨burst都验证。 |
| F21 / P2 | `capabilities/input_schemas.py:452`；`relations/routing.py:39` | relations.list resource只验证object，缺memory_id、未知type、id为数组等合法通过schema后HTTP500。 | 每种端点嵌套schema及结构化4xx；不能仅验证正常object。 |
| F22 / P2 | `apps/web/src/pages.tsx:56,65,101` | 备用React hold未送必填categories，实测403 CATEGORY_REQUIRED；自动写40天前日期。遗留摘要仍显示“恢复旧正文”并调用退役memory.restore。 | 使用有效token操作React写入；离线迁移文案和退役入口不可达。适用npm dev或构建后的/app，不宣称README默认HTML页有此错。 |
| F23 / P2 | `capabilities/registry.py:407,413,510`；`relations/corrections.py:1` | transport t:running与领域op:completed分事务。业务删除已提交、outer回执未写的崩溃窗口，同header/body重试OUTCOME_UNKNOWN，去掉header却立刻领域重放成功。违反atomic_write所宣称的端到端重试行为，但未发现重复删除。 | 在领域提交后/transport回执前注入崩溃，仍须找回同一完成结果；可以依据领域receipt恢复外层，不必放松未知结果保护。 |

## 当前五个红测为什么失败

这些失败均已实际执行，不能继续沿用历史“0 failed”结论；也不能通过回退最新语义来让旧测试变绿。

| 用例 | 当前原因 | 正确处理方向 |
|---|---|---|
| `test_p1_fixes.py::TestB02CrashWindow::test_stale_running_raises_outcome_unknown_not_replay` | fixture向裸key种running；Registry已读 `_transport_key(key)`。该行根本没有被命中，操作正常创建，因而DID NOT RAISE。 | 按现行t:键空间构造已运行/崩溃状态，并保留无业务副作用断言。 |
| 同类 `test_fresh_running_still_busy` | 同上，未命中fresh running。 | 同上，验证真正同一个transport key。 |
| 同类 `test_same_key_different_payload_conflicts` | 同上，未命中旧payload记录。 | 同上，必须让真实请求读取到冲突行。 |
| `test_v17_reaudit_fixes.py::TestReplayGuardN03N04::test_guard_refreshes_packet_header` | refine把original_request从“查搬家”改为“再查搬家”；新CB-014拒绝不同query fingerprint的旧候选包。旧测试仍期待重标为新revision。 | 断言StaleOperation；另测同计划的可合法重放。不是缺失指纹的fixture，而是确实改了查询计划。 |
| `test_recall_runtime_parity.py::test_session01_mcp_full_loop` | refine scope仅八月，只有8/10一桶；earlier合法为空，旧测试期待7月桶越界返回。 | 加8月更早种子，或断言空且不越界，保留新scope门。 |

## “测试测不出失败”的确证

没有发现全局吞pytest异常、统一xfail或强制全绿机制。问题是具体断言、前置条件、假依赖和覆盖不足。多数fake是合理的结构隔离；fake存在本身不是缺陷。

1. **恒真子断言。** `test_recall_query_plan.py:118` 的 `... or True` 永远通过；`test_recall_v14.py:445` 文件存在性断言同样如此。这些测试仍有其他有效断言，不能称整个测试完全恒真。
2. **退役MCP守卫匹配了错误名字空间。** `test_retired_capabilities_negative.py:48` 用canonical `diary.` 等正则匹配固定以`mariposa_`开头的transport工具名。AST执行原测试，注入三个退役工具仍通过。应正确映射或比较完整transport禁止集合。
3. **E2E在未认证时已经绿。** `apps/web/e2e/forget-loop.spec.ts:6` 用无效Bearer test，并把401当成功。实际探针：正常memory.hold和三个退役入口均401，断言全通过，没触达Registry。需要隔离实例有效token，明确断言退役错误；不能把认证失败当业务保护成功。
4. **BOOT-07只测试自造逻辑。** `test_v2_bootstrap.py:122` 自己生成三天日期列表，不调用实现。AST将bootstrap.get替换为永远抛错，原测试仍通过。应冻结时钟、写边界桶、调用真实Bootstrap。
5. **负测被其他错误提前拦住。** `test_relation_deletion_closure.py:108` 的legacy前缀用例缺expected_source_version，实际得到SCHEMA_VIOLATION，未走prefix guard；只断言Forbidden掩盖了路径错误。需要完整有效前置及专门错误码，并验证移除目标guard后测试真红。
6. **并发结果没有被断言。** 同文件`:196` 忽略线程结果，桶存活走pass；纠错/删除均异常也能过。`:246` 收集statuses却不assert，不能验证单次成功副作用。应检查线程结束、成功/失败数量、业务状态和审计次数。
7. **应失败的前置缺失变成skip。** `test_closure_reaudit_fixes.py:254` 在待验证的words命中消失时直接skip。若这是测试必须构造出的前置，应assert；只有外部可选依赖缺失才适合skip。
8. **reset不是干净基线。** `tests/conftest.py:137` 漏清memory_keeps、field_search_docs、field_fts、relation_corrections；关闭FK后清父表，不会级联。合成reset前四表各1，之后memories=0而前三个子/派生表仍各1。可引入顺序依赖、孤儿统计及误覆盖，应完整清理并验证无残留。
9. **验收PASS证据已经漂移。** `docs/memory_runtime/ACCEPTANCE.json` 的71个唯一evidence引用中，12个对应文件或函数已不存在，例如退役`test_raw_recall_scope.py`和已改名words测试。该文件记录的是9月26日v1.4历史验收，不能继续当最新v1.7通过证据。应版本化保留历史记录，并建立当前nodeid映射。

补充：`test_v2_plans.py` 当前只有fixture/helper，收集0项。其他文件仍覆盖部分计划行为，因此不据此宣称“计划完全无测试”；需要按语义矩阵检查缺失的生命周期场景。

## 现象已证明、合同范围仍需裁定

以下不计入23项确定缺陷，避免用不完整正本推导新增要求：

- **继续请求与重新start的额度绑定。** 相同continue_request_ref两次refine可分别领burst2/3；相同request_ref两次start创建不同session。budget.py自身要求新的请求及“反复start不产生新额度”，但仓库没有完整v1.4正本及可信用户消息归属合同。应明确需要服务端去重/绑定的粒度，不能仅以非空客户端字符串证明请求真实性。
- **新写话语来源的校验范围。** hold可写新raw_msg:前缀，append可写不存在的source_msg:；纠错入口会拒绝。现行清理报告的“新来源必须现行身份”位于纠错合同内，是否约束初次写入需裁定。已证明这类来源会增加删除硬门word_sources计数。
- **Round2拒绝理由。** EXPLICIT_REJECT_AFTER_DELIVERY只看任意rejected；拒绝仅seen未入本revision交付包的候选也能背书。若status提供的候选ref也算交付，范围不同，不能直接断言越权。
- **words的scope内评分。** SQL WHERE过滤speaker/date，但bm25(words_fts)仍用全库统计。追加被排除speaker的50条alpha会翻转可见alpha/beta的排名。S05/S06是否强制覆盖words专项需核定；实现现象已证明。
- **Bootstrap安全包装与遗留摘要gap。** Bootstrap外层不补content_role/instruction_authority；memory.get对legacy summary无LEGACY_CONTENT_GAP。需要明确CURRENT“读侧/安全包装”的具体适用面；已存在的Recall包装与gap不能自动证明所有显式读取都必须同形。

## 建议修复顺序与交付标准

先修 F01/F02/F03/F04/F05/F06/F07：票据双向版本校验、开关重放、锁内终态校验、心情留底、必要Jev角色、失败批次清场、完整恢复集。其次按域修Source关系与删除派生数据、时间与Bootstrap、Jev缓存/断流、接口与备用UI。独立修陈旧红测和假覆盖，避免调整实现迎合已退役语义。

每项修复应先运行本文反例或加入等价失败用例，证明基线真红，再证明修复后通过；同时验证目标guard/行为被移除时用例确实失败。最后按本轮互斥分批方式重跑，报告唯一nodeid计数和明确未执行项，不使用历史累计绿数代替当前证据。

五项关键反例已保存为 [reproduce_critical.py](reproduce_critical.py)：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B docs/audit_20261003/reproduce_critical.py
```

脚本必须在仓库根运行，强制新临时根与合成数据、禁止网络模型，并设45秒总超时。当前五项都输出FAIL、退出1（不是测试框架错误）；若全部对应语义修好则退出0，探针自身错误退出2。本轮独立执行约0.214秒。脚本依赖仓库测试环境安装的确定性Judge，不验证真实模型召回质量。

真实dense/Jev质量、私有语料阈值、estómago live换窗、生产部署和实际恢复均未执行，也没有据此宣称通过。
