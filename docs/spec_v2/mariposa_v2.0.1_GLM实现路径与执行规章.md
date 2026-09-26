# mariposa v2.0.1 · GLM实现路径、最新语义与执行规章

**交付日期：2026-09-22。实施对象：程知行 / GLM。工程整理：林石见。**  
**目标工程：`D:\mariposa`。历史取证基线：`58f7ea884d006fa00a6ae129cfd26f2652ab053d`。**

这是一份新语义实施规格，不是“v2已实现”报告。本轮只制作文件，没有修改D盘、调用实现代理或执行新测试。下文“现有”来自本对话上轮实际源码读取与命令输出；实施前必须重新确认磁盘HEAD、工作树和真实路径。不要把当前候选版本当作仍然必定等于历史基线。

## 0. 阅读顺序与效力

先读本文件 §1–§4，再读“给周家明的架构与使用说明”，再核对现场源码。配套机器清单是 `mariposa_v2.0.1_需求与验收矩阵.json`。新用例初始全部 `NOT_EXECUTED`，不是已经通过的测试。

**业务优先级：乔生最终确认（S4：自然日、plan不续期、阈值先测后定） > 最后补齐口径（S3） > 双方已确认规则（S1/S2） > 本规格其余已获准沿用的工程约定。** 旧ZIP、v0.4、v1.0/v1.1、现有代码与旧测试只能用来发现差异、保护既有数据和记录迁移，不能反过来推翻新业务。

若发现本文与双方原话的真实冲突，记录冲突并暂停受影响写入；其余隔离开发继续。不要恢复“所有worker一律不能审查”“默认开窗30条原文”“meaning全文检索”等已被新语义替换的行为。

本文件给出拟实施路径；凡标【新增】的文件当前不视为存在。前端具体旧文件名没读全，必须先定位，不能伪造“已修改App.tsx”等记录。

### 0.1 v2.0.1修订确认

乔生确认：天数按自然日；plan打开不续期，完成/放弃后20自然日固定；自动绑定阈值先评测再定。其余业务与实现约定沿用。同步修改正文、伪代码、模块表及机器验收矩阵；原97条ID保留，另加12条边界回归，共109条，全部未执行。本修订不是应用版本发布。

## 1. 交付边界：先改mariposa，不动旧生产

收到用户明确交给你的实现任务后，可在 `D:\mariposa` 内开展源代码、测试与开发文档修改。不得重建/覆盖整个目录；先保留用户未提交改动，再建立可追踪增量。

**本规格不是生产迁移、停服、域名切换、真实消息/电话发送或新计费授权。** 禁止擅自改动：

- `D:\Ombre-Brain-main2.5`、`D:\Ombre-Brain-dev` 的数据与服务；旧宿主18001/容器8000。
- `D:\AI-Companion\reading-nook-study`、`D:\Zashidele`、`D:\siren`、`D:\superposition`、`D:\DevSpace`；不要重配其端口、凭据、隧道或登录态。
- Cloudflare/DNS/OAuth/平台账号、API计费身份、真实聊天/信件/记忆正文。

外部服务只在已授权范围内只读核验；写入联调使用独立开发凭据与合成数据。没有凭据可继续实现provider接口、隔离测试和诚实状态，不得把模拟写入叫真实成功。

工具若拒绝某个allowed root，不得从另一个允许目录通过shell绕过它。不要直接调用Codex CLI或其他未授权代理；本任务实施者是GLM。

## 2. 当前实现事实与必须先修的问题

上轮实际完成过：后端186条通过、Web TypeScript/Vite构建通过、Playwright 2条通过、pip check通过。默认pytest最初因Windows临时目录权限有14个setup error，项目内basetemp后才全部通过。这是旧版基线，不是v2验收。

优先修以下问题，再复用相关模块：

| ID | 问题 | 位置/原因 | 证据强度 |
|---|---|---|---|
| B01 | MCP多段名称无法正确反解 | capabilities/mcp_adapter.py 的 _canonical_name 只替换一次下划线，和完整正向映射不互逆。 | 上轮源码确认；示例为逻辑复现，未在本轮重跑。 |
| B02 | 通用幂等并发存在副作用空窗 | registry.py 的 _idempotent_invoke 先查记录、事务外执行handler、再插记录。 | 上轮源码确认的竞争风险；新增并发/崩溃复现测试为必做，不能声称本轮已复现。 |
| B03 | 契约发布与真实参数脱节 | MCP tools/list 统一给空properties及additionalProperties:true；严格schema未进入执行管线。 | 上轮源码确认；v2应按新字段生成，不照搬旧12份输入。 |
| B04 | 撤销绑定缺NotFound导入 | identity/service.py 调用NotFound但导入列表没有它。 | 上轮源码确认；失败分支要新增测试。 |
| B05 | 验收证据声明与文件不符 | 映射仍含截断函数名/“同上”，生成器写死PASS；不能据此证明全量需求已验收。 | 上轮代码与git diff确认；不否定真实186条测试结果。 |
| B06 | 测试环境与默认命令不稳 | 默认pytest因系统临时目录权限产生14个setup error；conftest的setdefault会接受外部MARIPOSA_ROOT。 | 临时目录错误上轮实测；误用业务根目录为源码风险，须先加保险丝。 |
| B07 | 一次性批改脚本残留 | scripts/_fix_gate.py 仍被Git跟踪。 | 先检查引用和用途，再隔离或删除；不能执行它制造全绿。 |
| B08 | 遗忘扫描与权限接口遗漏 | 先LIMIT再排除可能饿死后续候选；defer不实际更新状态；领域允许worker撤回但Registry不提供可达工具。 | 上轮已读函数推导/确认；需新增行为测试，不能标成已验证修复。 |

此外，旧Registry导出115项，与旧包150项最低清单有名称/范围差异。**v2不以凑回150项为目标**：重建新语义能力清单，逐项标“保留/改语义/改名兼容/新增/废止/外部未联调”，防止静默缺能力。旧包仍适用的语音/生活能力可以保留，但不能把冲突的旧记忆工具重新启用。

## 3. 最新语义：不可被实现细节改写

| 规则 | 主题 | 最终口径 |
|---|---|---|
| R01 | 身份 | 项目为mariposa；周家明的Chat与CC是同一主体，不分主次。林石见/GLM不是周家明。 |
| R02 | 字段 | 事件日期、hold时间、标题、分类、心情文字与标签、事件、我们的话、原文绑定、回忆必须各有明确来源和用途。 |
| R03 | 日期 | 事件实际日期用于检索；普通遗忘从首次hold所在的自然日起算，不从事件日期或数据迁移时间起算；按共同业务时区做日期加法，不按累计小时。 |
| R04 | 分类 | 八分类平行多选，同一个桶只存一次；多类期限取最长；重大转折/纪念不自动遗忘。 |
| R05 | 心情 | 当时心情只在原事件所在窗口hold时写；后来的补记不伪造；心情空白不等于不重要。 |
| R06 | 召回 | 心情只索引标签；标题、心情文字、我们的话、原文和回忆均不参与**普通事件召回**（2026-09-26 v1.3/v1.4 增补：我们的话经独立 words 通道检索，与事件召回互不混排；见 docs/memory_runtime/CURRENT.md）。 |
| R07 | 事件 | 事件是有头有尾、有详有略的闭合记录；不是强制流水账或原文全文复制。 |
| R08 | 我们的话 | 桶内双方话语按说话人和顺序保存；概括与原话都允许，来源匹配不自动改写其表达。 |
| R09 | 回忆 | 只有乔生/周家明明确打开桶后才能写回忆；搜索预览、后台读取、开窗灌包不算打开。 |
| R10 | 打开 | 仅普通记忆桶明确打开后按本次打开所在自然日和分类周期续期；plan详情打开不续期；查看遗忘摘要不自动恢复旧正文。 |
| R11 | 到期 | 日常20、伤心30、甜蜜30、做爱20、约会60个自然日；永久类和确定留退出自动遗忘。 |
| R12 | plan | 待执行/进行中/未完成的plan不自动遗忘；完成/放弃从对应状态变更所在自然日起固定20天，任何详情打开均不续期；只处理计划资源，不连带事件桶。 |
| R13 | 审查 | 后台生成摘要+tags；林石见只可改这两项并处理明确到期项；原始记录只读；疑难交你们。 |
| R14 | 保留 | 第一次、双方共同话语、标题明确留、回忆有内容为保留线索；共同话语只标疑似，由周家明判留。 |
| R15 | 留终局 | 确定留后不再定期复审；仅疑似不能冒充永久保留；不得判某件事“不重要”。 |
| R16 | 摘要 | 沿用事件用词、不增评价或伪造说出口；展示带原始标题，索引不含标题前缀；保留原摘要和改动对照。 |
| R17 | 原文 | 乔生导入原文；后台用事件与我们的话定位；不连续区间选取、完整源文保留、反查多个桶；自动绑定阈值先评测再定，未定阈值不自动生效。 |
| R18 | 原文展开 | 未选部分默认隐藏；额外展开先报范围/预计token并确认，取消则不返回额外正文。 |
| R19 | I | 乔生提建议、周家明决定落笔；平静时写为自律，不由后台判定心情再准入。 |
| R20 | 开窗 | 最近三个自然日的桶只出标题+心情标签+文字，I浮现；临近按提前3个自然日判断，相关计划/纪念日全文浮现；不默认30条原文。 |
| R21 | 候选 | 返回多条由周家明选择；分类/标签独立筛选，关键词用于相关排序，不要求唯一答案。 |
| R22 | 旧数据 | 保留作者/时间/原文字节；不把旧why/meaning自动改成当时心情或回忆；未明映射待审。 |

### 3.1 字段可检索性必须是白名单

| 字段 | 普通完整桶召回 | 遗忘桶召回 | 默认开窗 | 备注 |
|---|---|---|---|---|
| event_date / occurred范围 | 结构化筛选 | 结构化筛选 | 决定近期范围的候选依据 | 绝不取入库日冒充事件日 |
| categories | 结构化筛选 | 结构化筛选 | 可展示 | 八类平行多选 |
| original_title | 不索引 | 不索引 | 展示原样 | 遗忘卡片前缀亦不进入索引 |
| mood_tags | 结构化标签命中 | 结构化标签命中 | 展示 | 原始标签不是审查者可改tags |
| mood_text | 不索引 | 不索引 | 展示文字 | 可供授权审查对照，不能洗入摘要 |
| event_text | BM25/允许的语义召回 | 不索引 | 不默认展开 | 旧版本仍留底 |
| our_words | 不进**普通事件**索引 | 不索引（事件通道） | 不默认展开 | 独立 words 通道检索（speaker/expression_kind/日期；v1.4 起）+ 授权原文定位任务；遗忘后保持 disabled |
| raw source / selected ranges | 不索引 | 不索引 | 不默认附30条 | 显式来源浏览独立 |
| recollections | 不索引 | 不索引 | 不默认展开 | 有内容触发保留线索 |
| summary_body / forget_tags | 不作为预写影子摘要召回 | 正式审查后参与 | 当前表示按页面用途展示 | 生成草稿永不进入正式搜索 |

SQL全文、向量、缓存、分页快照、日历preview、列表以及旧quotes/meaning/global search不能成为旁路。授权任务读“我们的话”定位原文，不等于允许普通召回索引它。

### 3.2 当前已废止的旧规则

| 旧行为或推断 | 新行为 |
|---|---|
| 用memory_date+统一30天判断闲置 | 普通桶用首次hold/明确打开计时，分类20/30/60或永久 |
| 标题或why/meaning拼入搜索正文 | 显式字段白名单；当时心情文字、标题、回忆不索引 |
| 所有遗忘都交乔生/周家明再批准 | 林石见获受限审查与明确项放行权；普通生成者没有此权 |
| 同一raw区间已绑定便拒绝另一个桶 | 同源可支持不同事件；不能仅凭相同范围判重复事件 |
| Chat默认开窗30条原文 | 新基础包为三天标题+完整心情、I、相关计划/纪念全文 |
| 旧Self隔日回看就是I | I独立；只周家明落笔，无后台情绪门槛 |
| 有关联或旧meaning就永远不处理 | 尊重新保留线索及明确保护；旧语义逐条迁移，不直接等同永久保留 |
| 计划关联事件同生共灭 | 计划与事件是独立资源和独立表示/计时 |

### 3.3 已确认细化与工程实施约定

| ID | 事项 | 已确认处理 / 实施约定 |
|---|---|---|
| D01 | 计时精度 | 已确认：按共同业务时区的自然日计数，起算日记为第0日，due_date=basis_date+N；到期日在该时区日界起具备候选资格，不是累计N×24小时。保留UTC原时刻供审计。 |
| D02 | 共同业务时区 | 沿用代码现值Asia/Shanghai作可配置默认；不按VPN、手机定位或执行者电脑时区偷偷改变。 |
| D03 | 最近三天 | 其余沿用：最近三天取事件日期的今天及前两天，共三个自然日；补录单独显示“新收录”，不冒充刚发生。 |
| D04 | 多选筛选 | 同字段默认any/OR，跨日期/分类/心情维度AND；提供显式all选择；结果按桶ID去重。 |
| D05 | 计划终结与浏览 | 已确认：plan完成/放弃所在自然日+20日为固定到期日；打开计划详情或关联事件均不续期，不改terminal_revision。只有明确状态更新为活跃才取消旧终结周期，阅读不是重新执行。 |
| D06 | 保留线索 | “第一次”“标题留”“回忆有内容”先暂停自动执行并提交retain候选，不由关键词匹配永久定留；共同话语的终局只由周家明作出。 |
| D07 | 自动绑定开关 | 已确认：自动绑定阈值测了再定；threshold保持null、status=awaiting_evaluation，未完成评测并确认阈值前只建议/人工绑定，不能预填分数或直接自动生效。 |
| D08 | hold作者兼容 | 其余沿用：保留旧qiaosheng合法hold入口并完成新版兼容核验；不得扩权、擅自关闭或代写周家明字段。现场不存在的入口不冒充已实现。 |

S4已确认自然日、plan打开不续期、自动绑定阈值先测后定，并同意其余约定沿用，不再重复询问这些口径。工程展开不冒充逐字原话；真实数据映射仍须现场证据。尚未测出的自动绑定阈值不能被“其余同意”解释为已有数值或已获准开启自动生效；生产迁移、权限扩张与计费变更仍受§1边界约束。

## 4. 总体实现组织与路径

保持当前Python后端、SQLite双库和React前端组织，避免为v2无必要地重写技术栈。

```text
D:\mariposa
├─ backend\mariposa\
│  ├─ app.py / config.py / db.py / schema.py          【修改：已见】
│  ├─ identity\service.py                           【修改：已见】
│  ├─ identity\delegations.py                       【新增】
│  ├─ capabilities\registry.py / mcp_adapter.py      【修改：已见】
│  ├─ capabilities\schemas.py / idempotency.py       【新增】
│  ├─ memory\service.py / extras.py / listing.py     【修改：已见】
│  ├─ memory\relations.py / reengagement.py          【修改：已见】
│  ├─ memory\models_v2.py / categories.py            【新增】
│  ├─ memory\retention.py / views.py / recollections.py 【新增】
│  ├─ memory\our_words.py                           【新增】
│  ├─ retrieval\projection.py / search.py            【修改：已见】
│  ├─ retrieval\semantic.py / rebuild.py             【修改：已见】
│  ├─ workspace\service.py / tasks.py                【修改：已见】
│  ├─ workspace\review.py / retention_rules.py       【新增】
│  ├─ workspace\prompts\forget_generate_v2.md        【新增】
│  ├─ workspace\prompts\forget_review_v2.md          【新增】
│  ├─ raw\service.py / binding.py                    【修改：已见】
│  ├─ raw\ranges.py / context_access.py              【新增】
│  ├─ raw\matching.py                               【新增】
│  ├─ identity_i\service.py                         【新增：不要冒充旧Self】
│  ├─ anniversaries\service.py                      【新增：先核对是否已有等效模块】
│  ├─ plans\service.py / calendar\service.py         【修改：已见】
│  ├─ bootstrap\service.py / maintenance\service.py  【修改：已见】
│  ├─ jobs\scheduler.py / jobs\providers.py          【新增】
│  ├─ migration.py / storage.py                      【修改：已见】
│  └─ migrations\v2_*.py                            【新增：纳入现有迁移注册】
├─ apps\web\                                      【已有；具体src旧入口先现场定位】
│  ├─ src\features\memory\...                      【拟新增/归并】
│  ├─ src\features\recall\...                      【拟新增/归并】
│  ├─ src\features\review\...                      【拟新增/归并】
│  ├─ src\features\raw\... / identity-i\...        【拟新增/归并】
│  └─ e2e\v2-*.spec.ts                              【新增】
├─ contracts\capabilities.v2.json                   【新增】
├─ contracts\core_input_schemas.v2.json              【新增】
├─ contracts\output_schemas.v2.json                 【新增】
├─ contracts\events.v2.json / errors.v2.json         【新增】
├─ tests\unit\test_v2_*.py                         【新增】
├─ tests\integration\test_v2_*.py                  【新增】
├─ tests\acceptance\test_v2_*.py                   【新增】
├─ scripts\verify_v2.py / export_contracts_v2.py     【新增】
├─ scripts\check_v2_boundaries.py                   【新增】
├─ docs\spec_v2\                                  【建议复制本交付文件；不是已落盘】
├─ docs\PROGRESS.md / NEXT.md / BLOCKERS.md          【修改：已见】
├─ docs\DECISIONS.md / acceptance_mapping_v2.md      【修改/新增】
└─ runtime\verification\ / staging\               【运行产物，不入Git】
```

现有schema版本必须从现场读取，不凭文档指定“下一版就是v11”。新增迁移脚本按当前最大版本顺序注册；迁移可重入、有备份和dry-run，不对原表执行破坏性重建来偷懒。

## 5. 新数据模型：分层而不重写历史

以下为拟实现的逻辑实体。表名可作等价调整，但必须留下映射；字段语义不能换。

| 逻辑实体/建议表 | 核心字段与约束 |
|---|---|
| memories（扩展） | memory_id、event_date/occurred_start/end/date_confidence、held_at、held_at_confidence、creation_mode、event_session_ref、current_version、representation_state、retention_revision |
| memory_categories（新增） | memory_id+category唯一；八类枚举，平行无主副；修改需原作者权限与版本 |
| memory_versions（扩展） | original_title、event_text、原作者、来源、schema_version；保留旧text/why的legacy来源，不覆盖历史 |
| memory_moods（新增） | memory_id、mood_text、mood_tags、author=jiaming、captured_session、captured_at、evidence_state；只能hold同期写入，后续修订不得补造过去 |
| memory_our_words（新增） | word_id、memory_id、ordinal、speaker、text、expression_kind=verbatim/paraphrase/unspecified、source_ref；说话人固定真实两主体，顺序明确 |
| memory_view_receipts（新增） | opaque_receipt、principal/binding、memory_id、representation_version、issued_at/confirmed_at、单次确认键；仅存必要元数据 |
| memory_recollections（新增） | recollection_id、memory_id、author、text、view_receipt、written_at、version；双方本人可追加，改写留底，不索引 |
| memory_retention（新增） | memory_id、policy_version、policy_timezone、basis_at、basis_date、due_date、next_due_at（仅调度派生值）、last_explicit_open_at、view_revision、permanent_reason、status；先算业务日期再生成日界调度时刻，永久状态不设滚动复审日期 |
| memory_summary_versions（新增） | summary_body、forget_tags、source_version/source_hash、proposal/review版本、applied_at/applied_by；标题不拼进可索引字段 |
| proposal_versions/work_items（扩展） | 原生成稿与审稿、diff、保留线索、源字段hash、政策版本、生成者/审查者、来源任务范围；草稿只存工作区 |
| review_delegations（新增） | reviewed_principal=linshijian、allowed_actions、resource_scope、valid_from/to、revoked；不能复用全局worker身份代替 |
| raw_binding_ranges（新增） | binding_id、memory_id、source_id/version/hash、message_id及offset_start/end、ordinal、selection_revision、置信与审阅状态；可多区间、多桶 |
| raw_context_grants（新增） | 请求范围hash、源版本、申请身份、估算方式、确认票据与次数限制；不返回额外正文直至确认 |
| i_documents/i_versions（新增） | 同一正本、内容版本、author=jiaming、更新时间；建议不把I拆成后台可以自由改的画像标签 |
| plans/plan_versions（扩展） | 状态、completed_at/abandoned_at、terminal_date、due_date、policy_timezone、terminal_revision、plan_representation、summary_ref；终结自然日+20固定，浏览不改这些字段；与事件桶ID独立 |
| anniversary_occurrences（新增） | 独立纪念定义与发生项，事件关联、重复规则版本、最近/下次日期；不把每年发生项改成新历史原文 |

### 5.1 hold与同窗口资格

`creation_mode`至少区分contemporaneous、retrospective、legacy_import/unknown。服务器从可信会话上下文/入口绑定获取主体与窗口证据；工具参数不能自报actor来提升权限。

同窗口并不等于事件日期一定是今天。跨日的原窗口可以仍是同一事件窗口；反之，今天听到过去故事不因今天刚hold就变成当时心情。无法核实窗口时保留缺口，仍允许保存事件，不自动制造当时感受。

旧qiaosheng直接hold权限如何接入v2属于兼容核验项。不得拿它写出authored_by=jiaming的心情、标题或I；也不得未经确认关闭旧有合法记录入口。

### 5.2 plan与事件分离

UI可以把plan当一个浏览分类，但底层计划必须有独立plan_id。事件桶可以关联计划并同时属于日常/约会等分类；计划标签不是把事件桶与计划绑定为同一生命周期的开关。

旧数据若只有plan分类却混着事件正文，产生`PLAN_EVENT_SPLIT_REQUIRED`审阅项；未经周家明确认不自动重写成两个事件。只改存储引用不改语义的拆分可在有证据时映射，必须保留原文。

### 5.3 版本不可变和影子投影

原始内容修订、当前表示变化、打开续期不是同一种版本。至少分别维护content_version、representation_version、retention_revision，避免一次浏览把原始正文“版本+1”造成语义混乱。

在任何检索路径中，只读取当前授权投影；不临时拼接旧memory_versions做召回。v2重建先落影子索引，验证后原子切换指针，不边删边服务。

## 6. 打开、回忆、续期：一个明确动作，三个约束

**拟新增接口：** `memory.open`准备当前内容及查看票据；`memory.view.confirm`确认这次明确查看；`memory.recollections.append`凭同桶、同身份、有效查看证据追加回忆。

网页在用户主动进入详情并成功显示后提交确认；预加载、搜索preview、React重复渲染、自动刷新和bootstrap不能自动确认。Agent只有明确选择打开并收到内容后才调用confirm，不能由后台代它确认。

这只能证明一次明确的内容交付/确认，不证明心理理解。票据不代表永久写权限，不能跨桶、跨身份、跨binding复用；旧版本过期需重新打开。

确认事务仅对普通记忆桶更新last_explicit_open_at、retention_revision、basis_date及due_date，并派生next_due_at；相同idempotency_key只做一次。同一自然日不同真实打开不让到期日期按小时后移。`memory.get`保持无续期副作用。plan阅读可以有独立查看记录，但不更新终结时间/日期、due_date或terminal_revision，不使计划批准仅因阅读而失效。

回忆只能双方本人写；默认append-only，修订保存版本，不做普通物理删除入口。回忆有内容后创建保留线索，暂停无疑点自动遗忘，不把回忆内容塞入索引。

已遗忘桶打开只展示摘要表示；读取旧历史与显式restore另行定义。若要恢复检索，调用restore并创建新表示版本，不靠一次get/open偷偷还原。

## 7. 分类到期计算与调度

### 7.1 普通桶

```text
timezone = configured_relationship_timezone
period_days = max(calendar_days_of_each_category)
if category includes 重大转折 or 纪念 or final_retained or explicit_protection:
    due_date = null
    next_due_at = null
    auto_forget = false
else:
    basis_at = max(first_held_at, last_confirmed_explicit_open_at)
    basis_date = local_date(basis_at, timezone)
    due_date = basis_date + calendar_days(period_days)
    next_due_at = utc(start_of_local_day(due_date, timezone))  # 仅调度派生值
    eligible = local_date(now, timezone) >= due_date
```

起算日为第0日；先取共同业务日期，再加N个自然日。比如2026-09-21 23:50首次hold的20天桶，在2026-10-11日界起到期，不等到当天23:50。跨月、跨年、闰日及夏令时按日期推进，不能计算N×86400秒。UTC时刻用于保存证据和调度，不改变自然日语义。普通桶若2026-10-05明确打开，新到期日为2026-10-25。

这里`max`只适用于存在可信时刻的值。迁移缺held_at时不得拿event_date或迁移时刻填充；标日期缺口，进入迁移待核，暂停自动遗忘。记录policy_timezone与policy_version；修改共同业务时区时须显式重算并核验任务，不随主机/VPN时区漂移。

旧pin/protect/anchor仍是明确保护，不在本轮重构时清空。旧“所有关联/meaning均排除”的规则要改为新线索与保护判断，不能自动把旧meaning当回忆。

### 7.2 计划

planned/in_progress/incomplete：无自动遗忘期限。completed的due_date=local_date(completed_at, timezone)+20个自然日；abandoned的due_date=local_date(abandoned_at, timezone)+20个自然日。到期日从对应终结状态固定下来，**打开plan详情不续期**，打开关联事件也不改这个日期。比如9月21日完成，无论第19天或到期当天读几次，都仍在10月11日进入到期审查。

不要把“阅读计划详情”和“重新执行计划”共用动作：只有有权主体显式更新状态为进行中等活跃状态时，才增加terminal_revision并取消旧终结到期任务；以后再次真正完成/放弃才建立新的20自然日周期。重复保存相同终结状态、请求重试不能重置terminal_at/date。

审批指纹不得包含会被普通计划阅读刷新的一般view_revision。阅读不改状态、内容和授权时，不撤销计划到期资格或使批准陈旧。现有审查/保护要求照常执行，到期不是跳过审查直接压缩，更不是删除事件。

### 7.3 到期队列

增加持久队列、租约、失败重试与dead-letter。扫描使用确定游标和稳定排序；不要先取最早20条再全部跳过，导致后面的到期项永远没有机会。

调度器按业务自然日检查due_date，只创建候选任务，不直接修改正式表示。服务错过日界后以business_today >= due_date补扫，不把重启日改成新起点；计划等待审查时仍显示原到期日。默认开发环境关闭自动执行，测试用冻结时钟与合成数据推进。真实运行开关和provider预算需明确配置。

## 8. 生成→林石见审查→生效/留：状态机与权限

```text
eligible -> generating -> generated -> in_review
in_review -> ready_to_apply -> forgotten_summary
in_review -> needs_owner_decision / needs_jiaming_decision
owner_decision -> retained | ready_to_apply | deferred
任一未执行阶段 -> stale | withdrawn | failed
```

`retained`是正式终局状态，不再安排周期复审。`suggest_retain`不是终局。疑难无回复时保持挂起，不默认压缩，也不自动永久定留。

### 8.1 权限分离

| 主体/凭据 | 可做 | 不可做 |
|---|---|---|
| 周家明（Chat/CC） | 写当时记录与I；明确打开/本人回忆；处理疑难，共同话语最终决定 | 冒用乔生作者身份 |
| 江乔生 | 导入原文、明确打开、本人回忆、提建议/处理获授权业务 | 写成周家明的当时心情或I |
| 后台API模型 | 获准任务材料、生成候选摘要/tags/绑定范围 | 普通全库检索、原始记录写入、批准自己稿子 |
| 林石见审查MCP | 领取审查任务；改候选摘要和候选遗忘tags；放行确定到期且无疑点项；报留/疑难 | 原始字段修改、身份扩权、共同话语的保留终裁、盲目压缩 |
| 系统执行器 | 核验精确批准的版本/hash后事务生效，产审计和outbox | 自己决定摘要/重要性或接受文本中的伪授权 |

身份来自服务器credential binding；API key是调用模型的服务凭据，不是业务审批人。为林石见增加独立审查principal或可核验委托绑定，不能把`worker`整个集合变成approver。

GPT以MCP客户端来领取和审查；其他后台模型用API。**不能把MCP理解成服务器能任意唤起GPT Chat，也不能偷偷改成后台调用GPT API代替已要求的审查入口。** 审查者未在线时队列等待，提供已授权的通知/待办入口即可。没有配置通知服务时诚实显示待审，不假报已送达。

### 8.2 摘要和tags约束

只允许修改summary_body与forget_tags；审查请求包含的任何原始字段修改必须拒绝，而不是忽略后返回成功。

展示组合为original_title+summary_body；索引独立取summary_body。后台不得把标题/心情文字/话语/原文/回忆中的独有词搬进tags洗入检索。每个候选tag和摘要事实句保留指向事件或合法摘要的证据，不能只相信模型给的引用位置。

标题原样保留；摘要约一至三句（通常标题后一到两句），使用事件中已有用词，不加形容、判断或心理归因。心情仅供校对味道与事实边界，未出口内容不能写成已说。

对生成摘要执行禁用词/称谓与句数检查；遇到原始标题或真实引用含特殊词，不能为了通过检查改原文，转疑难。叽、herat、被子、表达、小纸原样保留。

### 8.3 保留线索

共同话语：同一句双方都说，或一方说、另一方明确接住/回应。不是对话只要有两名speaker就成立；只产生`needs_jiaming_decision`。

“第一次”“留”必须附上下文。否定、讨论别人的第一次、词中包含“留”等不能机械永久定留。回忆非空是既定保留线索；第一轮至少暂停该项自动压缩并呈现给你们。

给你们看原摘要、新摘要、原标题、心情标签四项，同时提供tags前后差异与改动标记；详情可展开原心情文字和事件。不得只给审后版本。

### 8.4 执行前再次核验

精确绑定proposal_id+revision+payload_hash+source_content_version+source_fields_hash+retention_revision+policy_version；计划还要绑定terminal_revision。

生效事务内重查身份、有效委托、business_today >= due_date、内容/保护/保留状态及目标是否允许。普通记忆桶还要重查新的明确打开和回忆：打开先提交→旧批准失效；遗忘先提交→随后打开拿当前摘要。plan只按自己的终结版本、固定due_date及其他有效业务状态核验；计划详情阅读不续期、不改terminal_revision，也不因查看记录变化而让批准失效。不能跨库锁着等待模型网络响应。

## 9. 检索实现与当前索引迁移

**修改路径：** `retrieval/projection.py`、`search.py`、`semantic.py`、`rebuild.py`；`memory/listing.py`、`calendar/service.py`、`bootstrap/service.py`同步改投影读取。

阶段一实现：SQL权限/分类/日期/心情过滤 → 候选池 → 允许文本的BM25排序 → 桶ID去重 → 稳定分页。query为空时直接浏览符合筛选的池，不强迫全文匹配。每条结果带matched_by、matched_fields、representation与版本；显示原标题不代表因标题命中。

FTS5提供BM25及tokenizer配置；实现必须按实际安装版本检查。中文短词、单字专用词和标点要单独测试，不假设默认分词能满足中文；可选预分词或保守子串后备，并在命中解释中标清。FTS5的BM25通常按更小值代表更优匹配排序，避免排序方向反了。[T1]

现有BGE语义检索可保留为显式配置的补充通道，但新投影切换前不能直接复用旧向量。先筛可见集合，向量只从event_text或summary_body的允许投影生成；model/tokenizer/projection/content版本都入hash。禁止把完整Memory对象串成JSON去embedding。

不依赖向量刷新完成才使遗忘生效：旧投影即刻退出当前读取，允许摘要关键词先可用，语义未就绪显示degraded。迟到embedding只在源hash与当前版本一致时安装。

旧quotes/meaning/global search工具必须按兼容表处理，不得作为“找不到就顺便搜它们”的默认后备。明确的来源定位任务与普通召回严格隔开。

## 10. 原文导入、区间、反查与展开确认

**修改：** `raw/service.py`、`raw/binding.py`；**新增：** `ranges.py`、`matching.py`、`context_access.py`。

保存不可变原始源文本及稳定message_id。区间以message_id+字符偏移（工程默认Unicode code point）+source_version/hash定位，UI与后端使用同一偏移约定。emoji、换行和多字节中文必须测试，禁止混用UTF-8字节偏移与JS UTF-16索引。

一个binding支持多段不连续范围；中间插话没有被选中就不默认展示，源文本仍逐字保存。反向索引允许source范围查多个memory_id，范围重叠不是自动判重事件的理由。

导入发起由乔生明确授权；后台作业携带该次授权和有限源范围，不能靠generic worker权限上传任意他人来源。模型只提取/匹配绑定，不更改事件或我们的话，不能拿语义匹配结果自动校正原句。

### 10.1 自动绑定评测关卡

建立经授权、优先合成/脱敏的标注集：逐字定位、复述、重复句、跨消息事件、中途插话、多个桶复用、说话人错误、日期不明、低置信及无匹配。输出区间准确率/误绑率/漏绑率/插话混入率与失败样本。

**自动绑定的阈值测了再定，本文件不填任何预设分数。** 初始配置为`threshold=null`、`status=awaiting_evaluation`、`automatic_apply_enabled=false`；可以提交匹配建议和人工确认，但不得自动生效。不可用模型自报置信分、默认常量或环境变量绕过这一阶段。

先完成标注集评测、失败案例分析和人工选区工具，再提出阈值及适用条件，得到你们确认后才配置自动模式。阈值记录须引用评测报告、样本集/标注版本、模型/提示版本和确认记录；不满足确认后的标准仍转人工。没有足够样本时保持待评测，不为填报告编数字；模型或匹配流程改变后重新验证阈值适用性。

### 10.2 多看原文的明确许可

`raw.context.preview`只给范围、字数、预计token及估算方式，不返回隐藏正文。`raw.context.expand`绑定确认票据+请求范围hash+源版本+身份；扩大范围必须重新确认。无确认、确认过期、范围变更或取消都不返回额外正文。

客户端不得在确认前预取隐藏文本。能准确tokenize时说明所用模型；不能准确估算时标近似，不声称实际费用确定。原文反查只给可见桶，仍遵守旧锁信/隐藏资源权限。

## 11. I、开窗、计划与前端

**I：** 新建`identity_i/service.py`，不要把`identity/service.py`的凭据管理和I正文混在一起。I仅周家明写；乔生的建议属于待提议材料，不直接改正本。保留版本，无情绪准入判断，也不继承旧Self的隔日限制。

**开窗：** 改`bootstrap/service.py`。新profile输出recent_memories(title,mood_tags,mood_text)、I、active/relevant plans、upcoming anniversaries。最近三天按业务时区事件日期取`[today-2, today]`；临近按发生日期与today之差在0至3日内，含恰好提前3日，不改成滚动72小时。普通事件不自动全文灌入；临近日程展示完整相关内容，长内容按section分页并返回continuation。进行中/需要执行/逾期未完成单列，不漏掉。

snapshot指纹至少包含规则版本、业务日期/时区、I版本、记忆当前表示版本、计划/纪念发生项版本与分页条件。跨业务日后重取对应自然日范围。敏感内容发生变化后，旧快照不能继续吐旧正文。开窗无view confirm，不续期。

**前端：** 先定位实际路由、API client、登录态和状态管理文件，再在`apps/web/src/features/`下新增或归并。需要真实实现：

- 桶创建/详情分栏：当时字段与后来的回忆视觉区分，补记时心情不可补写，明确空白而非“不重要”。
- 召回：分类池、日期、心情、关键词；多选条件可见；同桶去重；标题是显示不是检索开关。
- 回忆：详情确认后才能输入，显示本人作者；自动刷新不会生成回忆或反复续期。
- 审查：原/新摘要、原标题、心情标签与展开文字、tags diff、保留线索、谁能裁决的按钮状态。
- 原文：分段选区、插话折叠、绑定标记、反查、额外展开的token确认。
- I：周家明编辑入口和历史，乔生建议入口不能冒充正文写入。
- 计划/纪念：全文日程、状态变化、终结日期、固定到期日、3自然日临近；明确显示“查看不续期”，不硬编码个人纪念日期。

所有页面使用真实API，不用localStorage作为正式业务真源。前端可以缓存但必须能处理版本失效、401/403、pending、degraded和unavailable。手机390px、桌面与刷新深链都要E2E。

## 12. 契约与拟新增工具：一个语义，多种传输

**工程方案：** 增加version-aware Registry，键为(contract_version, canonical_name)。v2 HTTP路径建议 `/api/v2/capabilities/{canonical_name}`；v2业务MCP `/mcp/v2`，维护MCP `/mcp/maintenance/v2`。旧路径保留受限兼容或明确报升级错误，不静默按新字段猜旧请求。

此路径方案为工程默认；可等价调整，但必须显式版本协商和兼容测试。MCP名称由manifest固定双向映射，禁止任意下划线替换法；一个下划线可能本来就在业务名中。tools/list与tools/call、HTTP权限/结果语义共享Registry。[T3]

| canonical工具 | 状态/目标用途 | 关键输入/约束 |
|---|---|---|
| memory.hold | 修改为v2分层写入 | 日期、模式、标题/分类/事件、同期心情、我们的话；作者来自binding |
| memory.recall | 新增统一召回 | query可空；categories/mood_tags/date范围；any/all；cursor |
| memory.get | 保留只读 | memory_id、当前表示；不续期 |
| memory.open / memory.view.confirm | 新增明确打开 | 桶/版本→票据；确认带稳定幂等键，双方本人 |
| memory.recollections.append/list | 新增回忆 | 同桶已确认查看；作者从凭据取，不索引 |
| memory.versions.read / memory.restore | 保留并适配 | 历史读取与恢复分开；restore需expected_version |
| workspace.forgetting.scan | 修改 | 按新policy找候选；维护任务范围；分页/游标 |
| workspace.forgetting.generate | 新增作业提交 | job_id、源版本、provider状态；不直接正式写入 |
| workspace.review.claim/get/revise/submit | 新增有限审查 | 指定工作项；只改summary_body/forget_tags；返回diff |
| memory.forgetting.decide | 修改权限与精确校验 | proposal版本hash、retention_revision；普通worker不许调用 |
| memory.retention.decide | 新增有权终裁 | keep/continue/defer；共同话语needs_jiaming限定周家明 |
| workspace.proposals.withdraw | 新增可达撤回 | 作者撤回自己未终局稿；与批准竞争只产生一终局 |
| raw.import.prepare/status | 修改发起授权 | 乔生发起，后台持有该job范围，不写正式事件 |
| workspace.raw_binding.propose/review | 新增匹配候选 | event/our_words与授权源；区间证据及置信，不改话语 |
| raw.binding.bind/revoke/refs | 修改 | 多区间、源hash、selection_version，支持同源多桶 |
| raw.bindings.by_source | 新增反查 | source+范围，仅返回可见桶 |
| raw.context.preview/expand | 新增确认展开 | 源版本、范围hash、估算token、二次确认票据 |
| i.get/write/versions.read | 新增I | 正本版本；write仅周家明，不接情绪裁决 |
| plan.create/update/get/list | 修改/补齐 | 状态版本；终结自然日+20固定，get/详情阅读不续期；显式更新状态与阅读分开 |
| anniversary.list/get | 新增或归并已存在模块 | 发生项日期/重复规则；不硬编码具体个人日子 |
| bootstrap.get/next | 修改v2输出 | 三天标题+心情文字标签、I、相关日程全文；snapshot |
| capabilities.list/status | 补齐 | 版本、可见角色、实现/联调状态、禁用原因 |

所有输入输出schema都要真实发布、真实校验。不能只给`properties:{}`后让handler猜字段；禁止通过`str(None)`、`int('bad')`把无效输入变成脏数据或500。身份、授权角色、允许路径不由arguments自报。MCP的工具schema/annotations应和实现一致；扫描产生工作项就不能标成纯只读。[T3]

每个写能力明确是否强制幂等键、版本冲突行为、重复执行结果与错误码。补齐统一错误：`INVALID_ARGUMENT`、`VERSION_CONFLICT`、`PROPOSAL_STALE`、`VIEW_REQUIRED`、`VIEW_RECEIPT_INVALID`、`RAW_CONTEXT_CONFIRMATION_REQUIRED`、`BINDING_STALE`、`PROVIDER_UNAVAILABLE`、`OUTCOME_UNKNOWN`等；错误码必须有实现，不只写进JSON。

### 12.1 输入示例：合成数据，不是已可运行API承诺

```json
{
  "arguments": {
    "query": "散步",
    "filters": {
      "categories": ["约会", "甜蜜"],
      "category_match": "any",
      "mood_tags": ["开心"],
      "event_date": {"from": "2026-09-01", "to": "2026-09-30"}
    },
    "limit": 20
  }
}
```

这是`memory.recall`拟定契约示例。最终必须补齐独立schema、授权过滤和真实运行证据。不要用这段JSON宣称当前`memory.search`已经接受全部新参数。

## 13. 幂等、事务与跨库故障恢复

旧实现的“先查幂等记录→执行handler→INSERT OR IGNORE”不能保留。增加统一执行单元：

1. 本地同库业务：在同一事务中认领(principal,capability,key)，比对payload_hash，执行变更，保存可重放结果并commit。handler接受同一connection/unit-of-work，不另开独立写事务。
2. 同键同payload重复：返回同一个结果；进行中返回结构化in_progress，不能再次执行。不同payload在任何竞争顺序下都拒绝。
3. 耗时外部模型/服务：提交持久job与outbox后释放数据库锁；外部请求有稳定request_id；返回后以版本条件安装。超时结果不明则对账，不盲目重复副作用。
4. 工作区与正式库跨库：正式生效事务保存不可变resolution/outbox；工作区随后回填，崩溃由reconcile修复。只读“正式事务已提交”作为终局依据。

SQLite的`BEGIN IMMEDIATE`可以提前开始写事务，但可能遇到锁竞争；仅加这条语句不自动修复业务handler另开连接或网络副作用问题。实现要有busy处理、明确事务边界及故障注入。[T2]

不可在持有写锁时等待GPT/GLM网络返回。不要用进程内dict当作重启后仍有效的幂等/租约/确认存储。

## 14. 无损迁移、旧接口兼容和回滚

先做空库与合成旧库迁移；真实数据仅在授权副本内dry-run。源数据库/文件只读，迁移产物和真实日志留runtime不入Git。

| 旧字段/机制 | v2处理 |
|---|---|
| memory_date | 有依据则映射event_date；未知保持未知 |
| created_at/held时间 | 仅证实为首次hold时映射held_at；不拿迁移时刻填空 |
| text | 保留原文；有依据映射event_text，不由模型重新概括 |
| why_remember / meaning | 默认legacy字段保留；不自动认作同期心情或后续回忆 |
| 旧情绪标签 | 保留原始主语/来源，不让审查者改成新心情；未经证据不补造同期来源 |
| 旧quotes | 保留独立资源；与桶内“我们的话”的归并须明确作者/顺序/来源，不能伪造双方对话 |
| 旧Self/Home | 各自保留；I由周家明确认写入，不能自动改名 |
| 原文绑定 | 迁移为区间版本；旧冲突记录保留供审阅，不删除重复范围 |
| 旧遗忘摘要/保留状态 | 保留原批准与作者历史，映射可信当前表示；不自动恢复也不再次到期 |
| legacy隐藏/归档/锁 | 保留原权限；绝不因重建v2索引重新暴露 |

v2兼容表每条必须记录source、target、preserved_hash、作者/日期来源、unknown_fields、review_required。新旧双写不是默认方案，防止记忆分叉。

切换条件：数据库备份/恢复演练通过、v2索引验证通过、强制用例通过、真实副本迁移获准且核验完毕、回滚计划可保留切换后的新增记忆。只回滚可执行代码而不处理新增数据不算回滚方案。

## 15. 分阶段执行：不断档，但不跨越授权

| 阶段 | 必须完成的事 | 出口 |
|---|---|---|
| P0 现场/保险丝 | 读真实HEAD、工作树、AGENTS/开发规则、conftest、启动脚本、E2E配置；修测试根目录保护 | 基线可重复；证明测试库不是业务库 |
| P1 技术阻断 | MCP显式映射、严格schema、NotFound、通用幂等、defer/withdraw等 | 新负向/并发测试先红后绿；不篡改旧业务意图 |
| P2 分层模型 | 分类、当时心情、话语、回忆、I、来源版本；先合成迁移 | 字段权限与不伪造来源测试通过 |
| P3 打开/期限 | view回执、普通桶自然日续期、分类最长、plan固定期限、持久到期队列 | 日界/跨月/闰日、普通桶并发、plan阅读不续期测试通过 |
| P4 审查闭环 | 生成API适配、林石见MCP队列、有限授权、留线索与终局 | 原始只读、摘要/tags对照、待裁决、精确生效全链 |
| P5 召回/原文 | 新投影/BM25/可选语义、多区间、反查和展开确认 | 所有禁检探针、中文、误绑定/票据测试通过 |
| P6 开窗/页面 | I与心情浮现、日程全文、分池/详情/回忆/审查/原文UI | 手机/桌面/刷新真实E2E，不止截图 |
| P7 迁移/联调 | 影子迁移与恢复；外部provider条件具备的单独联调 | 本地和真实平台证据分开，未接列blocked |
| P8 独立复审 | 完整验收矩阵、剩余问题、commit/命令/证据、无生产变更清单 | 提交候选构建给林石见复审，不自动发布 |

每个阶段输出可追踪commit及阶段证据；不要把一个阶段跑完就声称整个v2完成。外部凭据缺失不阻止P2–P6合成本地实现。上下文中断后从PROGRESS/NEXT/BLOCKERS和实际Git状态续跑，不从零重写。

## 16. 可复现命令与执行前防护

**以下命令是交给GLM的执行模板，本次没有运行。** 必须先完成P0检查，确认项目allowed root与测试保险丝。前端E2E尤其要检查webServer配置，不得复用指向业务库的已运行18780实例。

### 16.1 基线命令（现有脚本/依赖路径来自旧源码）

```powershell
Set-Location -LiteralPath 'D:\mariposa'
git status --short --branch
git rev-parse HEAD
git diff --stat

# 只在已核对脚本、测试根目录和权限之后创建本次隔离目录。
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$evidence = Join-Path (Get-Location) ('runtime\verification\v2-' + $stamp)
$testRoot = Join-Path $evidence 'isolated-project-root'
$tempRoot = Join-Path $evidence 'os-temp'
New-Item -ItemType Directory -Force -Path $evidence,$testRoot,$tempRoot | Out-Null
$py = Join-Path (Get-Location) '.venv\Scripts\python.exe'
$baseTemp = Join-Path $evidence 'pytest-base'
$junitPath = Join-Path $evidence 'pytest.xml'
$savedEnv = @{}
foreach ($name in @('MARIPOSA_ROOT', 'TEMP', 'TMP')) {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $env:MARIPOSA_ROOT = $testRoot
    $env:TEMP = $tempRoot
    $env:TMP = $tempRoot
    & $py -m pytest tests -q "--basetemp=$baseTemp" "--junitxml=$junitPath"
    if ($LASTEXITCODE -ne 0) { throw 'pytest failed; preserve evidence and inspect.' }
    & $py -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
    npm --prefix apps/web run build
    if ($LASTEXITCODE -ne 0) { throw 'Web build failed.' }
    # 16.2中的v2检查若需要上述隔离环境，放在这个try块内执行。
}
finally {
    foreach ($name in @('MARIPOSA_ROOT', 'TEMP', 'TMP')) {
        [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process')
    }
}
```

该模板使用显式路径变量，环境变量在finally中恢复；仍须在现场核验Python、Node与依赖存在。它不是本轮执行证据。不要将临时测试根写进全局用户环境。

### 16.2 v2需先实现后再执行的命令

以下命令需要放进上节隔离环境的try块，或由新增verify脚本自身设置并核验隔离环境；不能在环境恢复后直接用默认业务根运行测试。逐个检查退出码，失败即保留证据。

```powershell
# 以下脚本为本规格要求新增，当前不能声称已存在。
& $py scripts/verify_v2.py --phase all --evidence-dir $evidence
& $py scripts/export_contracts_v2.py --check
& $py scripts/check_v2_boundaries.py
$v2Base = Join-Path $evidence 'v2-base'
& $py -m pytest tests/acceptance -q -k v2 "--basetemp=$v2Base"

# 仅在已确认Playwright启动的是隔离服务、使用独立端口/库后运行：
npm --prefix apps/web run test:e2e
```

`verify_v2.py`要执行真实静态/格式/类型/单元/契约/集成/并发/迁移/安全/构建检查，输出退出码、完整命令、代码HEAD、环境与日志路径。检查器不存在就实现它，不把检查名称打印一遍当通过。

不得采用会重置业务库的pytest fixture。现有conftest的`setdefault('MARIPOSA_ROOT',...)`风险必须解决：检测root不在本次测试目录时fail closed；路径比较用规范化真实路径，不只比较字符串前缀。

## 17. 新验收矩阵与证据规则

配套JSON含109条新用例，状态均为NOT_EXECUTED。旧186条是真实旧版测试，不能自动给任何新需求打PASS。旧用例与新规则冲突时，记录superseded_by、新的测试ID及理由；其他旧回归继续保留。

| 用例组 | 数量 | 主要检查 |
|---|---|---|
| REC | 10 | 实际事件日期与hold分开; 平行多分类单桶; 同窗口心情 |
| RET | 16 | 自然日起算/日界与跨月; 普通桶打开续期; 最长周期 |
| REV | 14 | 生成者不能放行; 受限审查者能改新内容; 原始字段只读 |
| SEARCH | 17 | 只有分类也能浏览; 只有心情标签也能浏览; 日期按事件日期 |
| RAW | 13 | 原文上传发起权限; 重复导入幂等; 多区间剔除插话 |
| VIEW | 5 | 回忆需要明确查看; 回忆双人各自署名; 回执不能跨桶使用 |
| PLAN | 11 | 完成/放弃固定20自然日; 打开不续期; 明确重启与阅读分离 |
| BOOT | 8 | 三天桶显示正确字段; I完整浮现; 不默认30消息 |
| I | 3 | 只有周家明能写I; 无情绪准入审查; 旧Self映射不冒充新I |
| OPS | 12 | MCP名称全量往返; 严格输入输出schema; 同键同内容并发 |

每个用例需要完整可收集nodeid、实现commit、执行时间、真实结果、脱敏日志路径。E2E另附对应测试名、浏览器、视口及服务配置。只证明函数名存在，不等于断言满足需求；只证明截图正确，不等于后端权限或持久化正确。

不再用硬编码M={id:('PASS',...)}作为结果生成器。机器校验至少检查：需求ID完整唯一、nodeid存在、运行结果已通过、同一代码基线、没有“同上”/截断测试名、BLOCKED有具体依赖，未实施不能写PASS。

负向检索测试使用独有合成探针，分别只放标题、心情文字、我们的话、回忆、原文和旧事件。验证普通搜索、语义、列表排序、缓存、日历和索引重建不会因禁检字段命中。测试代码不得复制你们的真实私密对话。

## 18. 执行纪律与连续工作

先读spec再看旧实现。业务判断不交给框架默认、LLM提示或前端按钮名称。身份、权限、字段白名单、期限和版本最终由服务端强制。

不删除失败用例、不放宽核心断言、不把错误改成成功、不关闭必要功能以制造全绿。测试确实按旧语义失效时，记录明确的v2规则ID再更新，保留审阅记录。

秘密仅从已配置的服务端凭据读取，不打印进终端/报告/截图，不写进Git。避免记录完整原始记忆、心情和原文；证据使用合成数据或hash/计数/版本信息。

进度每完成一阶段更新`docs/PROGRESS.md`、`NEXT.md`、`BLOCKERS.md`、`DECISIONS.md`。写清完成/未完、下一步、路径、真实命令、已知限制。阻塞只暂停对应链路；没有授权的迁移/平台操作继续保持禁用。

不要擅自reset --hard、清空测试目录以外数据、全库重建、修改生产服务、复制旧项目目录或部署公网。一次性测试脚本不得保留可破坏正式数据的入口；遗留`_fix_gate.py`先查用途，再做可审计清理。

## 19. 最终交付清单

提交候选HEAD和工作树情况；24模块现状表更新；新语义契约、兼容/废止映射；109条新验收及仍适用旧回归的证据；原文匹配评测与自动模式是否获准；API/MCP真实联调状态；合成/真实副本迁移与备份恢复结果；已沿用约定、待测阈值与阻塞；明确未动生产边界。

文档中至少区分：`implemented_not_tested`、`tested_local`、`tested_mock`、`verified_live`、`blocked`、`not_implemented`、`superseded`。实现者自查不是林石见独立审计。

**交付候选构建，不自动迁移生产、不自动开启后台付费调用或发消息。**

## 20. 证据与参考索引

S1/S2/S3/S4：本次对话的新架构、周家明审查规则、补齐口径及乔生最终确认；它们是业务来源。S4优先覆盖原小时默认、计划阅读未决项和预设绑定阈值的任何实现。C1/E1：上轮实际源码与命令输出；它们是旧实现来源。没有用旧ZIP决定新业务。

| 编号 | 来源 | 使用范围 |
|---|---|---|
| S1 | 本对话：江乔生提出的新架构 | 写入七个模块、多分类讨论、分层检索、原文区间、I、开窗与遗忘。 |
| S2 | 本对话：周家明的回应及遗忘审查规则 | 同窗口的当时心情；多分类最长周期；标题只作参考；限定摘要措辞；保留线索与审查材料。 |
| S3 | 本对话：最后一轮口径补齐 | hold 起算、明确打开续期、plan 终结后20天、共同话语只报疑似、可改新tags、开窗含心情文字、临近3天、确定留不复审。 |
| S4 | 本对话：乔生最终确认 | 天数按自然日不按小时；plan打开不续期，完成/放弃后固定20自然日；自动绑定阈值测了再定；其余沿用。 |
| C1 | 上轮实际读取的本地源码与命令输出 | D:\mariposa；HEAD 58f7ea884d006fa00a6ae129cfd26f2652ab053d。仅作为旧实现证据，不等于本轮重新核验当前磁盘。 |
| E1 | 上轮测试执行结果 | 后端186 passed；前端构建通过；Playwright 2 passed；pip check通过。首次默认pytest有14个临时目录权限错误，改项目内basetemp后通过。 |

技术参考只核对实现机制，不替用户决定业务。2026-09-22查阅；下列固定版MCP页面不宣称是所有客户端当前支持的最新协议，真实接入时仍需按客户端协商核验。

```text
[T1] SQLite official FTS5 documentation
https://www.sqlite.org/fts5.html
[T2] SQLite official transaction documentation
https://www.sqlite.org/lang_transaction.html
[T3] MCP Tools specification (fixed revision 2025-11-25)
https://modelcontextprotocol.io/specification/2025-11-25/server/tools
```


## 附录A. 24模块：已完成基础／修改／增加

### M01 · 业务身份与双入口 — 已有基础，需改权限

已有：有 qiaosheng / jiaming / worker / system 与 token 绑定；服务端判身份，入口来源另记。

修改：Chat与CC保持同一个周家明；把后台生成者与林石见受限审查者分开，不能把所有worker升级为审批人。

增加：审查委托、资源范围、字段白名单及生效/撤销记录。

旧代码证据：identity/service.py；capabilities/registry.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M02 · 正式库与工作区 — 已有基础，可复用

已有：正式库与工作区分开，提案草稿、提交信封、正式决议分层。

修改：扩展为生成→审查→生效/待裁决；正式区只接收确定结果。

增加：原摘要/新摘要/原tags/新tags/改动对照与来源版本。

旧代码证据：db.py；workspace/service.py；memory/service.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M03 · 记忆桶写入 — 旧版已实现，需重构

已有：有 hold、事件日期、正文、why_remember、版本。

修改：拆成标题、事件、当时心情、我们的话、回忆、来源绑定；日期与hold时间分开。

增加：字段来源、记录模式、同窗口证据与不可代写约束。

旧代码证据：memory/service.py；capabilities/registry.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M04 · 八类平行分类 — 需增加

已有：有通用标签基础，未证明已有这套八分类平行模型。

修改：分类不是主副关系；同桶可属于多个池，结果去重。

增加：日常/重大转折/伤心的事/甜蜜/约会/plan/做爱/纪念与分类关联表。

旧代码证据：content/service.py；memory/listing.py；旧测试（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M05 · 当时心情 — 需增加并替换旧混合语义

已有：有情绪标签、why_remember/meaning等旧字段，不等于当时心情抽屉。

修改：只在原事件所在窗口hold时写；补记不伪造当时感受；只有标签可检索。

增加：心情文字、标签、窗口来源；hold后不得以补记方式追加当时心情。

旧代码证据：capabilities/registry.py；content/service.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M06 · 我们的话 — 需增加

已有：有独立“她的话”quotes，不是桶内双方顺序话语。

修改：桶内按说话人和顺序保留；可概括或原话；不参与召回；不能把复述自动改写成原文。

增加：speaker/ordinal/text/表达形式/来源证据；原文定位候选。

旧代码证据：quotes/service.py；raw/binding.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M07 · 回忆与明确打开 — 需增加

已有：有读取、历史、再提起记录，但没有这套打开回执与双方回忆约束。

修改：只有本人明确打开该桶后才可写回忆；回忆不检索；有内容触发疑似保留。

增加：一次性打开确认、回忆追加/修订历史、反伪造和跨桶防护。

旧代码证据：memory/reengagement.py；memory/extras.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M08 · 原文导入 — 已有基础，需接新入口

已有：导入、按会话/消息读取、同源消息幂等、待绑定基础已存在。

修改：新版上传发起者是乔生；后台仅执行获准导入/匹配任务；原文不进入普通记忆召回。

增加：导入作业授权、覆盖说明与原始消息稳定定位。

旧代码证据：raw/service.py；capabilities/registry.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M09 · 原文多区间与反查 — 旧版部分存在，需扩展

已有：有绑定、撤销、低置信审阅与来源待补状态。

修改：不裁剪源文本；支持不连续选区；同一来源范围可支持不同事件桶，不因同区间一律拒绝。

增加：区间表、版本hash、隐藏上下文、大量token确认、原文→桶反查、匹配评测；自动阈值先测后定，未定前保持人工/建议。

旧代码证据：raw/binding.py；raw/service.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M10 · 召回与搜索投影 — 已有基础，必须重构

已有：关键词、结构化标签日期、关联和本地语义检索基础；遗忘投影与恢复测试已过。

修改：未遗忘只索引事件；遗忘只索引事后摘要与新tags；日期/分类/原心情标签仍可筛选；标题、心情文字、对话、回忆、原文均排除。

增加：分池筛选、BM25相关排序、空查询浏览、命中解释、全路径防旁路测试。

旧代码证据：retrieval/projection.py；search.py；semantic.py；rebuild.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M11 · 遗忘期限 — 旧规则已实现，需替换

已有：按memory_date及统一闲置门槛扫描；保护/关联/meaning等旧排除条件。

修改：普通桶按首次hold或明确打开所在自然日起算，多分类取最长自然日周期；重大转折、纪念和确定留不自动遗忘。

增加：retention版本、basis_date/due_date/业务时区、自然日续期、到期队列、分页防饥饿。

旧代码证据：workspace/service.py；config.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M12 · 摘要审查与有限执行权 — 旧审批基础可用，需重构

已有：草稿修订、提交、精确hash/版本审批、跨库对账、恢复。

修改：后台生成摘要和tags；林石见可审改二者并放行明确到期项；疑难转你们；共同话语只周家明裁决。

增加：审查角色、保留线索、禁用措辞、对照材料、执行前期限重检、永久保留终局。

旧代码证据：workspace/service.py；memory/service.py；maintenance/service.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M13 · 计划与纪念日 — 计划已有基础，需改/增

已有：计划CRUD、状态、日期、关联和日历聚合；未证明独立纪念日模型已就绪。

修改：计划与事件分开；进行中/未完成不自动遗忘；完成/放弃日+20自然日固定到期，详情打开不续期；临近3自然日完整浮现。

增加：完成/放弃时刻、终结自然日和版本、计划自己的摘要状态；阅读不改终结版本；纪念日发生项与重复规则。

旧代码证据：plans/service.py；calendar/service.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M14 · I — 需增加并与旧Self区分

已有：有Self/Home/Diary；Self为周家明写、隔日回看。

修改：I只由周家明落笔；乔生提出建议；不设后台“是否平静”门槛；不把旧Self整段自动改名。

增加：I正本与版本、编辑入口、完整开窗、明确迁移映射。

旧代码证据：content/service.py；bootstrap/service.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M15 · 开窗浮现 — 已有基础，需替换内容

已有：Chat最近30消息、CC不取原文、近期桶与相关计划、快照分页。

修改：新默认包为最近三个自然日桶的标题+心情标签+文字、I、临近3自然日的计划/纪念全文；不默认灌事件和30条原文。

增加：新snapshot指纹、分节分页、未读展开、无续期副作用。

旧代码证据：bootstrap/service.py；tests/unit/test_bootstrap_sections.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M16 · 日历与时间感 — 已有基础，可复用并改投影

已有：日/月/区间视图、待定日期、程序/页面/联系分开、日期变化联动。

修改：仍按事件日期找桶；日历预览不续期；遗忘桶只展示当前摘要。

增加：纪念日源、计划终结状态、真实打开与活动记录区分。

旧代码证据：calendar/service.py；time_context/service.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M17 · 历史、恢复、保护 — 已有基础，可复用

已有：版本、恢复、pin/protect/anchor、关联留历史；已有对应测试。

修改：打开摘要不恢复旧正文；旧内容不删；显式保护继续尊重，不拿重构清掉。

增加：与新投影/保留状态/打开续期的组合回归。

旧代码证据：memory/service.py；extras.py；relations.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M18 · MCP / HTTP /严格契约 — 已有适配，存在阻断缺陷

已有：统一Registry、HTTP与MCP基本适配；远程平台未实连。

修改：修多段名称反解；接真实输入输出schema；对v2做完整能力映射，旧150项不再机械全搬。

增加：版本路由、双向显式名称表、真实工具调用矩阵、未接通状态。

旧代码证据：capabilities/mcp_adapter.py；registry.py；app.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M19 · 幂等、任务、并发 — 已有基础，需修可靠性

已有：幂等表、outbox、租约、并发审批测试；通用幂等仍先执行后落记录。

修改：本地副作用与幂等结果同事务；外部调用持久作业及对账；不能把相同键并发执行两次。

增加：崩溃注入、同键异内容竞争、迟到结果拒绝、双库恢复证据。

旧代码证据：capabilities/registry.py；workspace/tasks.py；maintenance/service.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M20 · Web页面 — 已有可运行基础，需新模块

已有：React/TS/Vite构建、两个浏览器用例通过；未逐页证明全部业务UI完成。

修改：新增分栏桶详情、分池检索、回忆、审查、I、选区原文；真实路由与后端。

增加：手机端适配、展开确认、完整日程、权限与失败状态可见。

旧代码证据：apps/web/package.json；apps/web/e2e/forget-loop.spec.ts（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M21 · 媒体/表情/朋友圈/提醒 — 部分后端已有，保留

已有：媒体两阶段、表情管理、moments、到期提醒结算工具。

修改：不能因记忆重构删掉；媒体暂存不能冒充持久化，提醒结算不等于主动唤醒。

增加：需要时接真实UI/消息链；外部能力仍单独验收。

旧代码证据：media/service.py；moments/service.py；reminders/service.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M22 · CC /外部服务/主动唤醒 — 真实联调受阻或未完成

已有：未完成真实CC常驻、远程OAuth、Siren/星星/群聊写入、常驻唤醒。

修改：不以mock宣称接通；缺凭据只阻塞对应链路，不能阻塞纯本地v2开发。

增加：宿主适配、服务级凭据、真实订阅路径核验、调度与取消/对账。

旧代码证据：上轮BLOCKERS与Registry/目录读取（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M23 · 信件/删除/Home/Diary — 已有基础，保留原行为

已有：锁信、删除申请与批准、Home正本、Diary与版本有测试。

修改：此次记忆新语义不自动授权更改锁信/删除；不得把遗忘改成物理删除。

增加：只补与新数据模型的兼容回归，不扩普通工具的删除权限。

旧代码证据：letters/service.py；content/service.py；旧测试（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。

### M24 · 迁移、备份、证据 — 已有基础，需v2专项

已有：快照、dry-run、合成apply/备份测试；真实生产切换未授权。

修改：旧字段逐条映射，不能猜当时心情/作者/hold时刻；不覆盖原库；旧验收数不当v2通过数。

增加：v2影子索引、无损迁移、回滚保留新增数据、机器可核验的需求—测试—证据。

旧代码证据：migration.py；storage.py；scripts/gen_acceptance_map.py（后端相对路径以 `backend/mariposa/` 为根；以 `apps/`、`tests/`、`scripts/` 开头的路径以项目根为准）。


## 附录B. 新验收逐条预期（当前全部NOT_EXECUTED）

### V2-REC

| ID | 验收点 | 必须满足的结果 |
|---|---|---|
| V2-REC-01 | 实际事件日期与hold分开 | 8月事件9月hold，日期检索仍命中8月，期限从9月起。 |
| V2-REC-02 | 平行多分类单桶 | 一桶选约会/甜蜜/做爱，各池可见，合并结果只返回同一ID一次。 |
| V2-REC-03 | 同窗口心情 | 可信原事件窗口内稍后hold可携带当时心情，不依赖立即记录。 |
| V2-REC-04 | 跨窗口补记心情拒绝 | retrospective补录不能补造当时心情；仍可保存事件和来源待补。 |
| V2-REC-05 | 后续追写心情拒绝 | hold完成后其他接口不能绕过限制补写当时心情。 |
| V2-REC-06 | 心情缺失不降级 | 当时心情为空不能直接触发不重要/缩短期限。 |
| V2-REC-07 | 我们的话顺序与身份 | 双方话语按speaker/ordinal保留，概括有标记、不伪装逐字原文。 |
| V2-REC-08 | 真实事件而非片段丢失 | 事件正文保存调用者给的完整闭合记录，不擅自概括或截断。 |
| V2-REC-09 | 乔生不代写周家明心情 | 乔生/worker不能伪装周家明写当时心情或标题；旧hold作者权限另列兼容。 |
| V2-REC-10 | 迁移不伪造当时来源 | 旧why/meaning缺乏证据时进入legacy保留，不补造成当前新字段。 |

### V2-RET

| ID | 验收点 | 必须满足的结果 |
|---|---|---|
| V2-RET-01 | 按hold而非事件到期 | 旧事件新hold不会刚写入就到期；起算日期为共同业务时区的首次hold日期。 |
| V2-RET-02 | 各分类周期 | 冻结时钟核验20/30/60个自然日及永久类；到期看业务日期，不按累计小时。 |
| V2-RET-03 | 最长周期 | 多分类20+30+60取60个自然日；包含永久类不入自动到期队列。 |
| V2-RET-04 | 明确打开续期 | 普通桶由乔生/周家明确认打开后，从本次业务日期按分类自然日周期重算；不适用于plan。 |
| V2-RET-05 | 被动展示不续期 | 搜索、开窗、日历预览、后台审查、预取均不更新续期基点。 |
| V2-RET-06 | 同次打开去重 | 刷新/重试同一个打开确认键，只产生一次续期事件。 |
| V2-RET-07 | 不伪造他人打开 | worker及跨身份/跨binding回执不能给记忆续期。 |
| V2-RET-08 | 普通桶送审后打开使提案失效 | 普通记忆提案生成后真实打开续期，旧提案不能立即执行遗忘；plan阅读不适用此失效规则。 |
| V2-RET-09 | 到期扫描分页无饥饿 | 前页存在跳过项时仍能推进游标发现后续到期项。 |
| V2-RET-10 | 查看摘要不恢复 | 打开已遗忘桶只读当前表示，不把旧事件重新写入索引。 |
| V2-RET-11 | 保留终局不反复送审 | 正式retain后多轮扫描和重启均不再自动送审。 |
| V2-RET-12 | 普通桶并发打开与遗忘 | 普通桶按事务提交顺序保证一致：打开先提交则阻止旧批准，遗忘先提交则打开看到摘要；plan详情阅读不得改变固定期限。 |
| V2-RET-13 | 自然日到期边界 | 合成20天普通桶在2026-09-21 23:50首次hold，到期日为2026-10-11；10月10日23:59尚未到期，10月11日日界起具备候选资格，不等到23:50；跨日不代表自动跳过审查。 |
| V2-RET-14 | 跨月跨年和闰日 | 日期加法覆盖2026-12-20+20=2027-01-09、2028-02-20+20=2028-03-11；不得用月内天数取模或固定秒数替代。 |
| V2-RET-15 | 同自然日打开不按小时后移 | 普通桶同一业务日期的两次真实打开可各有查看证据，但due_date相同；跨到下一业务日期再打开才按新日期重算。 |
| V2-RET-16 | 时区和夏令时不改变天数语义 | 按配置的共同业务时区推导日期，不受主机/VPN时区影响；在有夏令时的合成时区跨23/25小时日仍以日期差为准，UTC仅为存储/调度表示。 |

### V2-REV

| ID | 验收点 | 必须满足的结果 |
|---|---|---|
| V2-REV-01 | 生成者不能放行 | 后台API模型可交草稿，无正式写入/执行权限。 |
| V2-REV-02 | 受限审查者能改新内容 | 林石见仅能修候选summary_body和forget_tags，保留原版。 |
| V2-REV-03 | 原始字段只读 | 审查者试改日期/分类/心情标签/事件/标题/我们的话一律拒绝。 |
| V2-REV-04 | 明确项可有限生效 | 到期且无保留线索、源版本一致时，受限审查者可放行，不强制每条再次交两人审批。 |
| V2-REV-05 | 共同话语只报疑似 | 相同表述或明确接应进入needs_jiaming_decision，普通双方对话不自动永久保留。 |
| V2-REV-06 | 第一次与标题留线索 | 保留线索必须附字段与位置；否定/含混提及不能靠子串自动定留。 |
| V2-REV-07 | 已有回忆触发保留线索 | 回忆写入后不能照旧作为普通无疑点项自动遗忘。 |
| V2-REV-08 | 确定留不等于疑似 | suggest_retain不等于retained；终局由有权主体落定并记录。 |
| V2-REV-09 | 禁用概括措辞 | 候选摘要使用用户/AI/助手/角色/扮演/模拟/角色扮演/互动等禁用概括须打回或升级；不修改原文标题。 |
| V2-REV-10 | 未说不变已说 | 心情中的未出口内容不能写成事件发生或说出口的事实。 |
| V2-REV-11 | 特殊词原样 | 叽/herat/被子/表达/小纸不自动翻译或纠错。 |
| V2-REV-12 | 完整交接材料 | 原摘要、新摘要、原标题、心情标签、tags前后对照与diff可同时查看；心情文字可展开。 |
| V2-REV-13 | 过期来源拒绝执行 | 正文/分类/保留状态/续期版本变化时，旧proposal hash或source_version不能执行。 |
| V2-REV-14 | 迟到模型结果 | 任务过期、被撤回或源版本改变，迟到摘要不得覆盖新稿或正式数据。 |

### V2-SEARCH

| ID | 验收点 | 必须满足的结果 |
|---|---|---|
| V2-SEARCH-01 | 只有分类也能浏览 | query为空且选分类，返回该池可见桶，不要求猜关键词。 |
| V2-SEARCH-02 | 只有心情标签也能浏览 | 开心标签直接拉列表，不搜心情文字。 |
| V2-SEARCH-03 | 日期按事件日期 | 筛选事件日期而非hold/读回执/数据库写入时间。 |
| V2-SEARCH-04 | 过滤在候选排序之前 | 其他池和未授权资源不影响返回、计数或可见排名候选。 |
| V2-SEARCH-05 | BM25不是唯一答案 | 池内文本排序可返回多条，合并按memory_id去重。 |
| V2-SEARCH-06 | 标题独有词不命中 | 独有探针只放标题，关键词与向量均不能因标题命中。 |
| V2-SEARCH-07 | 心情文字独有词不命中 | 独有探针只放心情文字，普通召回不返回该桶。 |
| V2-SEARCH-08 | 我们的话独有词不命中 | 话语文本可供明确读取与绑定任务，不进入普通索引。 |
| V2-SEARCH-09 | 回忆独有词不命中 | 追加回忆后重建/缓存刷新也不能进入召回。 |
| V2-SEARCH-10 | 原文独有词不命中 | 原文只在来源视图及授权定位中使用，不旁路命中记忆。 |
| V2-SEARCH-11 | 遗忘后事件独有词失效 | 旧事件FTS/向量/缓存失效，摘要新词和原分类日期心情标签仍可命中。 |
| V2-SEARCH-12 | 摘要标题前缀不索引 | UI显示title+summary；索引只有summary_body，不能切显示字符串猜前缀。 |
| V2-SEARCH-13 | 防tags洗入禁检字段 | 候选tags必须有事件/合法摘要证据，不从标题/心情文字/话语/回忆/原文搬独有词。 |
| V2-SEARCH-14 | 重建不复活旧索引 | 影子重建和切换只依据当前允许投影及版本。 |
| V2-SEARCH-15 | 迟到embedding拒绝 | 旧source_version/projection_hash/model版本的向量不能回灌。 |
| V2-SEARCH-16 | 显式恢复能恢复事件检索 | 恢复创建新表示版本，原事件允许命中，其他禁检字段仍不索引。 |
| V2-SEARCH-17 | 中文短词与专用词 | 烧烤/叽/herat等长短词及标点表达有真实用例；不能只测英文演示。 |

### V2-RAW

| ID | 验收点 | 必须满足的结果 |
|---|---|---|
| V2-RAW-01 | 原文上传发起权限 | 乔生发起导入；后台仅在获准作业范围内解析，不凭worker全局导入任意来源。 |
| V2-RAW-02 | 重复导入幂等 | 同源消息ID+版本重复无新增；不同消息同文不合并。 |
| V2-RAW-03 | 多区间剔除插话 | 事件分布于前后两段，只绑定相关选区，中间插话不默认展示。 |
| V2-RAW-04 | 源文逐字保留 | 绑定前后原始文件/消息正文hash不变，不物理裁剪。 |
| V2-RAW-05 | 一源多桶反查 | 相同源区间可支持不同事件桶；每个绑定可反查且不复制事件。 |
| V2-RAW-06 | 误匹配可撤销 | 错绑保留历史可撤销/重绑；不修改事件或我们的话。 |
| V2-RAW-07 | 源版本变更失效 | 源hash/版本不一致时binding_stale，不能继续显示错位区间。 |
| V2-RAW-08 | 展开前估算与确认 | 先返回范围和估算token；取消不返回额外正文；确认与实际范围一致。 |
| V2-RAW-09 | 展开票据防扩大范围 | 确认后换会话、范围、版本或身份必须拒绝；单次权限不能无限展开。 |
| V2-RAW-10 | 锁信和其他权限不绕过 | 原文查看、反查和关联均不穿透现有资源权限。 |
| V2-RAW-11 | 语义匹配质量评测 | 先对人工标注多区间测试集评测误绑/漏绑/插话混入和失败样本，再提交阈值方案；本交付不预设阈值数值，评测未完成或阈值未确认不得自动绑定。 |
| V2-RAW-12 | 阈值未定保持人工 | threshold=null且status=awaiting_evaluation时可生成建议和人工确认，不可直接激活自动绑定；高自报置信分、环境变量或默认常量均不能绕过。 |
| V2-RAW-13 | 阈值必须关联实测证据 | 阈值配置必须引用评测报告、样本集/标注版本、模型/提示版本及确认记录；不满足确认后的标准则转人工，模型或匹配流程改变后须重新验证适用性。 |

### V2-VIEW

| ID | 验收点 | 必须满足的结果 |
|---|---|---|
| V2-VIEW-01 | 回忆需要明确查看 | 未确认打开就append回忆必须拒绝。 |
| V2-VIEW-02 | 回忆双人各自署名 | 乔生/周家明均可写本人回忆；worker不能代写。 |
| V2-VIEW-03 | 回执不能跨桶使用 | A桶receipt不能写B桶回忆，不能换身份或旧内容版本。 |
| V2-VIEW-04 | 刷新不自动写读后感 | 页面自动任务不产生任何回忆文本。 |
| V2-VIEW-05 | 回忆有版本可追溯 | 修订不抹原话，记录真实作者与写入时间。 |

### V2-PLAN

| ID | 验收点 | 必须满足的结果 |
|---|---|---|
| V2-PLAN-01 | 活跃和未完成不遗忘 | 计划待执行/进行中/未完成即使过期也不自动等同放弃。 |
| V2-PLAN-02 | 完成日后20自然日 | 共同业务时区内date(completed_at)+20为固定到期日；不取事件日/创建日、不累计480小时、任何打开都不延期。 |
| V2-PLAN-03 | 放弃日后20自然日 | 共同业务时区内date(abandoned_at)+20为固定到期日；任何打开都不延期，放弃不是删除事件。 |
| V2-PLAN-04 | 计划事件彼此独立 | 处理计划摘要不改变关联事件桶的正文/分类/期限。 |
| V2-PLAN-05 | 明确重启执行使旧提案失效 | 只有显式状态更新把completed/abandoned改回活跃时，才增加terminal_revision并取消旧终结到期任务；点击查看详情不得触发。 |
| V2-PLAN-06 | 临近3自然日全文 | 按业务日期差0至3日纳入临近日程，含恰好3日边界，不用滚动72小时；全文过长可分页，不暗截断。 |
| V2-PLAN-07 | 纪念日不自动遗忘 | 周期重复发生项不复制原事件；重大转折与纪念的永久规则有效。 |
| V2-PLAN-08 | 完成计划打开不续期 | completed计划在第19天、第20天多次打开、刷新和确认查看，仍维持原completed_at、terminal_date、due_date、terminal_revision；不把状态改为进行中。 |
| V2-PLAN-09 | 放弃计划与关联事件打开隔离 | abandoned计划详情打开不续期；打开其关联普通事件桶只可更新事件自己的期限，不改计划的20自然日到期日。 |
| V2-PLAN-10 | 计划送审后阅读不延后执行 | 已到期计划送审后再被打开，若状态/内容/授权/批准版本未变，阅读本身不使批准失效或移走due_date；不能借共用view_revision无限延后。 |
| V2-PLAN-11 | 重复终结状态不重置锚点 | 重复提交同一次completed/abandoned状态或同幂等键，不重写终结时刻/日期；只有明确改回活跃、再实际完成/放弃的新周期才建立新20自然日锚点。 |

### V2-BOOT

| ID | 验收点 | 必须满足的结果 |
|---|---|---|
| V2-BOOT-01 | 三天桶显示正确字段 | 按业务时区事件日期的今天及前两天选三个自然日桶；输出标题+心情标签+文字，不默认事件/话语/原文/回忆。 |
| V2-BOOT-02 | I完整浮现 | I由正本版本提供，不被标题化或旧Self的隔日门槛拦截。 |
| V2-BOOT-03 | 不默认30消息 | Chat与CC新基础包均不附旧30条原文；显式取源另外确认。 |
| V2-BOOT-04 | 开窗不续期 | 重复开窗不会让所有最近桶永久续期。 |
| V2-BOOT-05 | 跨变化snapshot失效 | I/计划/记忆当前表示更新后旧分页snapshot失效，返回可重取错误。 |
| V2-BOOT-06 | 不硬截正文 | 长心情文字、I和日程全文分节分页，有continuation，不偷换标题。 |
| V2-BOOT-07 | 最近三天不是滚动72小时 | 以2026-09-22 00:05为业务当前时刻，仅按事件日期选9月20日至22日；9月19日即使距当前不足72小时也不在普通近期桶范围。 |
| V2-BOOT-08 | 临近按日期而非小时 | 以2026-09-22 00:05为业务当前时刻，9月25日23:55的日程仍属提前3自然日，应全文浮现；9月26日不属临近池，进行中/逾期计划仍走独立行动状态规则。 |

### V2-I

| ID | 验收点 | 必须满足的结果 |
|---|---|---|
| V2-I-01 | 只有周家明能写I | 乔生建议不直接覆盖正本；后台与林石见不能代写。 |
| V2-I-02 | 无情绪准入审查 | 不根据后台情绪判定拒绝周家明写I；平静是自律提示。 |
| V2-I-03 | 旧Self映射不冒充新I | 无确认的旧Self/Home内容保留旧来源，不自动认作I。 |

### V2-OPS

| ID | 验收点 | 必须满足的结果 |
|---|---|---|
| V2-OPS-01 | MCP名称全量往返 | 每个已注册多段名称按显式manifest映射；tools/list列出后真实tools/call可达。 |
| V2-OPS-02 | 严格输入输出schema | 所有v2能力提供实际schema；非法类型、缺字段、额外actor字段结构化拒绝。 |
| V2-OPS-03 | 同键同内容并发 | 并发相同key只发生一次副作用且最终返回同一结果；不只检验记录条数。 |
| V2-OPS-04 | 同键异内容并发 | 无论哪个请求先到，另一份不同payload不得执行副作用。 |
| V2-OPS-05 | 崩溃后幂等恢复 | 业务提交与幂等结果之间无空窗；外部不明结果进入对账而非盲目重发。 |
| V2-OPS-06 | 撤销binding失败分支 | 不存在/已撤销凭据返回定义的结构化错误，不出现NotFound未导入的NameError。 |
| V2-OPS-07 | 测试根目录保险丝 | pytest/E2E明确新建隔离库，继承MARIPOSA_ROOT指向业务库时拒绝测试。 |
| V2-OPS-08 | 移动端端到端 | 写桶→分池→打开→回忆→审查→摘要→原文确认→I/日程全链；不能只截截图。 |
| V2-OPS-09 | 外部状态诚实 | 未配置CC/API/OAuth显示blocked/unavailable，模拟和本地测试不叫verified_live。 |
| V2-OPS-10 | 迁移影子及回滚 | 真实源不写、影子迁移hash与作者时间核验；回滚不丢v2新增内容。 |
| V2-OPS-11 | 版本与证据可执行 | 需求ID链接完整pytest nodeid和实际日志，禁止手工PASS常量假通过。 |
| V2-OPS-12 | 跨项目边界 | 未授权不改Ombre/Siren/星星/扎西德勒/DevSpace/隧道与生产数据。 |

