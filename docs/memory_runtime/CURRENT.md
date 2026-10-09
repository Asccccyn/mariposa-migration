# Mariposa 记忆运行时·当前语义（CURRENT）

**性质：仓库内唯一现行语义正本入口（v1.7 §0：不保留多套并列有效语义）**
生效：2026-10-04｜现行修订：2026-10-08（`MANUAL_HANDOFF_JUDGE_SWITCH_V1`，江乔生裁定：人工判断总开关、关闭模式全候选分页、Round2 通用 provider 门禁、Codex SDK provider 槽位）｜基线 commit：`f734a81`（2026-10-04 裁定批）｜
历史版本（v1.3/v1.4/v2.0.1 及一切旧报告）只作代码定位与证据，
**凡与本文件及"域→现行正本清单"所列文件冲突的旧句一律作废**。
2026-10-08 修订直接改写 §1/§4/§7/§11 相关条款并新增 §4.1；修订所涉新结构（recall_judge_policy 政策表、结果集快照、memory.recall.page、codex_sdk provider）由本轮施工落地，落地前不得宣称已实现。

## 0. 正本地位声明（审计入口条款）

- 上游执行包（v1.7/v1.4 正本文档与 `LATEST_DECISIONS.json`）**不在
  本仓库**。因此：**本文件 + 所引现行文件 + 仓库实现与测试 = 操作
  正本**。审计与实现不得引用仓库外材料推导新合同；发现实现与本文件
  冲突时，以本文件为准并按缺陷处理。
- 只字未在仓库落档的口头/会话裁定，已在 2026-10-04 全部收编进本
  文件（§4/§5/§6/§8/§9）。此后新裁定必须先落本文件再动代码。
- **数据状态（2026-10-04 乔生确认）**：mariposa 至今没有真实
  用户数据——她从未提供过任何真实原文；本机曾存在的
  `~/Data/live-20260925/mariposa-runtime`（51 条等）全部是各轮
  测试/审计写入的合成数据，已整体删除。审计与实现不得把库内
  拟真内容当真实数据处理；正式数据根当前为空，一切库内容均为
  隔离测试根随跑随删。
- 现行验收映射：`docs/memory_runtime/ACCEPTANCE.json`（v1.7，
  requirement → 当前 nodeid → 状态；`NOT_RUN_REAL_MODEL` 如实标注）。
  v1.4 历史证据在 `ACCEPTANCE_v1.4_history.json`，不参与当前验收。

## 1. 一句话

不遗忘、不压缩、不生成摘要；记忆按九分类整数周期在 WIDE→MID→CORE
间**现算**淡出（可检索字段逐层收窄，不是删除）；找话是全量 our_words
专项（scope 内 BM25 评分）；原文（Source 层）是二轮深搜与证据展开层，
永不进第一轮普通召回；**判断层由人工总开关政策（§4.1）管辖：开启＝
所选 provider（Jev 或 Codex）判断后有界交付，关闭＝零判断调用、本次
检索候选全集分页交付由周家明自行判断**；provider 之间不串联接力。

## 2. 分层规则（现行）

- **Recall Session 正本只在 Mariposa**（`runtime/recall/`）；estómago/CC
  只带 session ref 换窗重取。七动作（start/refine/reject/accept/
  navigate/status/close）与预算约束沿用 v1.4 §9 机制，幂等语义见 §4。
- **删除链已退役（v1.7）**：自动遗忘、二次压缩、摘要生产/审查/发布、
  林石见审查角色整链删除；历史 forgotten_summary 表示读侧标
  `LEGACY_CONTENT_GAP`，恢复走一次性离线迁移，无在线 restore。
- **明开回温**：`memory.open` → `view.confirm` 的服务端确认时刻写入
  `memories.last_explicit_open_at`（取 max 防倒退，同票据幂等不刷新）；
  命中/Jev/hydrate/bootstrap/预览/accept 都不算打开。查看票据同时
  绑定签发时刻的表示版本与**内容版本**（formal 迁移 28）；内容更新
  后旧票据不得确认/追加，重开+确认后的票据必须放行。

## 3. 阶段策略（`recall/phase_policy.py`，每次查询现算、永不持久化）

- 九分类必填：daily=20 / sex=20 / sad=30 / sweet=30 / date=60 /
  **reloplay=7（工程初值）** 天；milestone/anniversary 永久；plan 由
  计划资源状态自管。多选取最大 H，不累加；k=100（改 k 需两位主体同意）。
- 分支顺序：作者 keep > 永久类别 > plan（未终态 WIDE / done|cancelled
  即时 CORE）> 整数年龄（basis = max(首次 hold 上海日, 最近有效明开
  上海日)；D<H→WIDE，H≤D<2H→MID，D≥2H→CORE）。
- 字段矩阵：WIDE 6（标题/日期/分类/心情标签/事件/我们的话）→ MID 5
  （去我们的话）→ CORE 4（再去标题）。mood_text 与回忆全阶段全检索
  禁用；raw 不属于 6/5/4。

## 4. 召回管线与请求幂等（`recall/pipeline.py` 已接主线）

- **Round 1**：结构过滤前置 → 每桶按当前事实现算阶段 → AllowedFields
  内在分字段索引执行词法检索；dense 路同 where 前置；RRF 融合 →
  判断层按 §4.1 政策执行（开启＝所选 provider 判断 + 代码门控 0-3 卡
  有界交付；关闭＝不构造/不调用任何 provider，冻结候选全集走 §4.1
  分页交付）→ 首轮真实执行完成自动签发 `ROUND1_COMPLETE` 回执。
  旧"`DisabledJudge` 显式 unavailable"语义保留给**未配置 provider**，
  不再承担"人工关闭直出"的语义——关闭只能来自明确的人类政策记录。
- **Round 2**（`memory.recall.round2`）：gate 全部服务端事实
  （同 session/revision、ROUND1_COMPLETE 回执、raw 授权
  = RECALL_RUNTIME+RAW_FALLBACK 开关且 owners、预算、理由属五值闭集）；
  判断条件按 §4.1 政策分形：**开启**＝当前所选 provider 自身的
  source_excerpt 外发许可（provider 无关接口，不再 isinstance
  TypeSafeJevJudge）；**关闭**＝无外发故不要求任何 provider 许可、
  `judge_required=false`，但首轮完成事实与覆盖披露仍必须真实。
  通过后在 Source 层（published=1，human/assistant）深搜，候选标
  `raw_verbatim`；出站按当前模式（开启＝判断后有界交付，关闭＝
  本批候选全集分页）。Raw 每批命中先保证本批全部可交付再推进上游
  游标；交付续页与 Raw 检索续页两类游标分槽。
  - **delivery 的正式定义（2026-10-04 裁定）**：
    `EXPLICIT_REJECT_AFTER_DELIVERY` 里的"交付" = 候选真实进入过
    模型侧可见的**出站交付包**（有交付回执且绑定出站轮
    `recall_receipts.revision`，runtime 迁移 12）。judge 看过、
    status=seen、内部检索/候选池出现都不算；不限定当前 revision
    （可拒绝前一轮真实交付过的候选），但必须能证明在哪一轮出站。
- **find_words**（`memory.find_words`）：跨阶段全量 our_words 专项
  （受 `MARIPOSA_WORDS_RECALL_ENABLED` 开关）；verbatim 不足可按
  §6.5 升级 raw 深搜。已知 `source_ref` 的定点展开是证据读取，不是
  检索 round，不需 Jev 重判。
- **request_ref / continue_request_ref / 预算（2026-10-04 裁定）**：
  - `request_ref` = 一次具体请求的幂等身份。幂等键 = **主体 +
    capability/action（如 recall.start / recall.refine）+
    request_ref**——`operation_id` 只是 transport/operation 回执
    身份，`session_id` 只是路由参数，**二者都不得改变 request_ref
    所定义的逻辑请求身份**（改变任一不能绕开冲突检测）。
  - payload 相同 → 重放**同一已完成 operation/result identity**：
    不重复业务执行、不新建 session、不重复消费预算、不产生第二份
    逻辑结果；**实际出站内容仍按当前权限、开关、版本与 Judge 状态
    重新校验**——Judge 已关闭时返回同一 operation 的
    unavailable/空正文，不重新释放旧正文（政策**切换**场景自
    2026-10-08 §4.1 起为结构化 `RECALL_POLICY_CHANGED` 拒绝——
    保存时模式与当前政策模式不一致即 fail-closed，不同形态混不进
    同一结果集；本句适用于政策关闭态下新生成的 unavailable 结果，
    D-1=A 2026-10-09 裁定）。payload 不同 →
    `REF_REUSE_MISMATCH`。未显式给 `operation_id` 时由 request_ref
    派生幂等键（schema 二选一）。
  - `continue_request_ref` 只表示"接着哪一个已交付的 revision 往下
    走"，**不承担幂等身份**。session 是线性状态机不支持分叉。合同
    要求：**服务端必须验证 continue_request_ref 与当前 session 最新
    已交付且可继续 revision 的绑定关系**——已过时的 ref、从未签发
    过的任意字符串都不能授予 burst；同一 continuation 不得重复领
    burst；同 request_ref 网络重试走幂等重放，不二次消费。具体
    token/映射/签名实现（ref→revision 表、continuation receipt 等）
    **不作合同规定**。（现行实现参考：burst 授予与 ref 消费同事务
    `recall_continue_refs`，runtime 迁移 11。）
  - 预算 = session 内成功轮次派生 COUNT，事务提交前重数；客户端
    ref 不发放额度。
  - 三者不混用：request_ref 管"是不是同一次调用"，continue_ref 管
    "从哪一轮继续"，session COUNT 管"用了多少预算"。
- **scoped BM25（2026-10-04 裁定，S05/S06 覆盖 words）**：任何不在
  当前检索可见候选宇宙中的文档不得参与 BM25 corpus statistics/IDF。
  event 与 words 都用 `scoped_bm25` 池内评分（owner/会话域/speaker/
  可见性/专项入口范围在评分前生效）；追加 scope 外话语不得翻转可见
  候选排名。
- **lexical_terms 合同（2026-10-04 确认，F17）**：`lexical_terms[]`
  每项是一个独立 lexical clause/phrase（组内有序且相邻），**不是**
  隐含 OR 的关键词袋；要 OR 传多项；自然语言近似交给 dense/semantic
  路径。keyword-mode 输入框在明确入口做拆词适配，不动底层合同。
- **Jev 证据角色（2026-10-03 审计 F05）**：证据门按候选类型核对
  必要主体角色——普通事件必须含 `event_evidence`，words/raw 必须含
  `primary_evidence`；标题不能替代事件主体。许可不足时明确
  unavailable，不放行未经主体判断的正文。query 投影与缓存身份含
  `lexical_terms`（词法目标变化构成新判断）；HTTP 中途断流
  （IncompleteRead 等）结构化降级 `JudgeUnavailable`。
- 生产开关默认全关：`MARIPOSA_RECALL_ENABLED` /
  `MARIPOSA_WORDS_RECALL_ENABLED` / `MARIPOSA_RAW_FALLBACK_ENABLED`
  ——**无例外**（fresh/continuation/operation replay 同权执行
  当前开关）。judge 通道自 2026-10-08 起改为 §4.1 的持久政策记录：
  **政策缺失/损坏/未知 provider＝未配置（阻断正文），不等于关闭**；
  旧部署升级时有效 Jev 配置保留开启、旧 disabled 保留阻断语义
  （显示 enabled=true/provider=null），不得升级时偷偷放宽；env 只作
  首次导入/凭据引用，不与数据库形成两套运行时优先级。

### 4.1 判断总开关与关闭模式全量分页（2026-10-08 裁定，`MANUAL_HANDOFF_JUDGE_SWITCH_V1`）

**政策正本**：正式库 `recall_judge_policy`（revision、enabled、provider、
model_id、allowed_data、updated_by、updated_at），部署级单记录、两位主体
共用政策但查询权限不合并。estómago 不存副本。

| 有效政策 | 实际执行 |
| --- | --- |
| enabled=true，provider 就绪 | 所选 provider 单独判断 → 原有有界交付（JUDGE_CAP=40 送判、DELIVERY_LIMIT=3） |
| enabled=true，provider 缺失/超时/非法输出/无许可 | 结构化 unavailable/partial；不暗切 provider、不暗改关闭、不假报"没有相关记忆" |
| enabled=false（明确人类记录） | 零判断构造/调用/缓存读取/网络/子进程；本次候选全集分页交付，`judgement_status=bypassed_by_user` |

- 人类网页登录才可写（`maintenance.recall_policy.get/update`，
  expected_revision + 幂等键）；主模型与 judge MCP 白名单**无更新权**。
  GET/刷新不调用模型；关开关不销毁所选 provider 配置。
- 每次查询冻结 policy_revision/模式/provider 版本；切换后旧 revision
  续页/重放返回 `RECALL_POLICY_CHANGED`，不混模式、不自动另起查询。
  关闭时取消排队判断任务、尽力中止已发任务；已发消耗照实留账。
- **全量分母**＝本次查询经既有检索范围/阶段/结构过滤/显式 rejected/
  合法去重后的候选全集；不是前 3/20/40，也不是整库无条件输出。上游
  Top-K/partial 必须披露 `retrieval_coverage`（`scope_exhaustive=false`
  +实际窗口），"结果集翻完"≠"数据库穷尽"。BM25/Dense 宽度、UNION_CAP、
  WIDE/MID/CORE 不因关闭而擅改。
- **`memory.recall.page`**：首页由 start/refine/words_recall/round2 各自
  现有入口生成并冻结结果集；续页以服务端签名的非透明 cursor 读取，
  不重新检索、不重新判断、不加 COUNT 轮、不重发副作用；重复同页在
  版本/权限/政策未变时结果稳定。快照存运行库（随 session 到期清理、
  纳入 purge/reset，无孤儿），绑定主体/scope/session/查询与政策
  revision/候选身份与顺序/获授权投影/游标状态。
- **页合同**：每页 ≤10 条目、完整响应 ≤24576 UTF-8 字节（含元数据与
  信封）；按实际字节装页；放不下的候选顺延下页，不得从全集 pop。
  单条超长按码点分片（start/end_char、content_complete），拼回与获授权
  投影逐字一致；片段数不得充当候选数。未经完整交付 has_more 不得为
  false。出站新增 `judge_mode`/`judge_policy_revision`/
  `judgement_status`/`pagination`/`retrieval_coverage`，加入白名单与
  宿主解包校验。关闭路径**不经过** `_enforce_output_budget` 旧裁剪器
  （600/4000/24576-pop 仅保留给判断开启的有界路径）。
- 关闭模式每卡不伪造 judge 分值：judged_count=0、confidence 省略、
  delivery_action=needs_validation；不自动开 AUTO_TOP1。宿主不替周家明
  一次翻尽全部页；未读页不得标成已审阅。

## 5. 「留」keep（v1.7 §5.5）

唯一入口：`recollection.append(keep_wide=True)` 同事务绑定本次新写
的回忆；hold 不能留、非本人回忆不能留、后台模型无权。谁留谁撤
（`memory.keep.revoke` 幂等）；双作者独立 OR；撤销不重置年龄。

## 6. Source 原文层（证据层）

Raw Archive 只读母本（`runtime/source/raw/`，chmod 0444）→ 严格 JSON
流式导入（单元素限额按**当前元素自身 UTF-8 字节数**，跨读取块边界
不误判）→ 发布门禁（published）→ 专项检索（`source.search` 独立于
普通 Recall）。失败批次默认不可见且**清场限本批**（历史合法空会话
与不可变快照受保护）；同 UUID 内容变化留不可变版本。
`memory_source_bindings` 绑定消息区间（parent 路径校验 + code point
半开区间偏移（邻接不重叠、锚定消息须属绑定会话）+ content_hash 防漂
移；边界序号上的同号 sibling 须消息身份一致才算覆盖）。

**md 对话转写导入（2026-10-04 四，乔生裁定）**：

- `source.import` 接受 `.md` 对话转写；两种方言：**claude 导出**
  （`## User`/`## Claude` 说话人标题、`**ISO Z**` 时间戳行、头部
  `**Created:**`/`**Link:**` 元数据、`### Thinking` 围栏块）与
  **gemini 导出**（`# you asked`/`# gemini response` 标题、
  `message time:` 行、`> From:` 来源 URL）。
- 时区：claude 的 Z 时间戳=UTC 直接用；gemini 裸时间按 **UTC+8**
  转 UTC 存储（两种导出的真实本地时间均核对为 +8）。
- 时间戳行是元数据不是内容：解析进 `created_at` 后**从原文剥离**。
- 出站时间只到**分钟**（年月日时分，按上海显示），秒/毫秒/Z 不出。
- 说话人映射：human=qiaosheng、assistant=jiaming（与 JSON 导入同）。
  Thinking 块与 JSON 导入同口径分离（标志+证据，不进正文）。
- 消息 id 确定性合成（内容锚定，重导幂等）；解析产出与 Claude JSON
  同形的元素契约——母本归档/发布门禁/失败清场/绑定全链复用。
- 切块为围栏感知（正文内代码块/标题不误切）；不识别任一方言的
  md 结构化拒收。测试只用合成夹具；真实文档由乔生自行导入。

**provenance 收紧（2026-10-04 裁定，优先项）**：

- `source_msg:<id>` 必须指向真实存在且**已发布**的 source message；
  不接受数据库不存在的 ID。
- `raw_msg:` 不作为任意字符串逃逸口：当前无正式 raw registry，
  hold/append 新写一律拒收；历史导入确需 raw source 的走导入入口
  ——先建可解析来源实体，再建 provenance edge。
- 新写入零 dangling；legacy 无法解析的来源读侧标 gap
  （`legacy_raw_prefix` / `invalid_or_missing`），**不作为有效
  provenance 污染删除硬门计数**。

## 7. 出站瘦身（compact_v1，2026-10-04 五——review4 方案）

- **纯展示投影层**（capabilities/compact.py）：业务操作、权限/开关/
  版本/Judge 重校验全部完成后才投影；canonical 结果、operation 回执、
  业务存储保留完整信息——精简包不写幂等回执、不喂检索/状态机。
- **profile 协商**：请求带 `output_profile: "compact_v1"`（MCP 参数
  或 HTTP query param），业务执行前剥离——不进 schema 校验/payload
  哈希/request_ref 载荷；同 operation 切换 profile 重放同一业务结果
  不重做。默认 legacy/full 不变。不支持的能力结构化拒绝。
- **白名单式只删诊断**：Bootstrap 的固定 policy/state_hash/估算值/
  当前页可推导 count；Recall 的 query_fingerprint/token_count/
  coverage 内部统计（_ 前缀、lexical_scorer/stage_filter/event_pool/
  judge_cache/dense_pending_vectors）/候选 scores/rrf_score/judge 的
  model/prompt 版本；excerpt 仅与首个 evidence.snippet 逐值相同时删
  （正文唯一完整副本在 evidence）。Source 检索同会话 ≥3 条时
  provider/会话 id 页级上提（小列表保持原形）。
- **不裁剪**：正文/证据链/ID/版本/receipt/budget/continuation/
  snapshot/分页游标/gap/truncated/false-null 语义；安全信封逐页
  保留。§4.1 新增出站字段（judge_mode/judgement_status/pagination/
  retrieval_coverage）同样不裁剪。不做短键缩写/位置数组/正文改写/
  机械删 null。实测首包 Recall −32%、Bootstrap −40%（合成小样本）。

## 8. 读侧安全语义（2026-10-04 裁定）

**安全语义统一，序列化形状不要求统一**：

- Recall：维持安全包装与 gap 标注（`content_role=retrieved_memory` /
  `instruction_authority=none`）。
- `memory.get`：补结构化 `content_gap`（legacy forgotten_summary 与
  空正文各标 `LEGACY_CONTENT_GAP`，reason 区分）；模型侧调用时正文
  不得被解释成系统指令。
- Bootstrap：不硬复制 Recall 外层字段，但内容进模型上下文时必须
  保证 memory 内容身份是 data/memory（顶层
  `content_role=bootstrap_memory_package` / `instruction_authority=
  none`）、legacy 缺口不伪装完整、不绕开 content-role/injection
  safety。I 与 Plan 长正文分节续取（`next_page` 的 `i` /
  `plan_content` 段）；mood 不是长内容载体不分节。
- **Bootstrap 续页（`bootstrap.next`）与首页同权**：每一页进入
  模型上下文边界时都必须能明确证明该页内容是 memory/data 且无
  instruction authority——页级 envelope 或统一 ContextAssembler
  包装均可；**不要求逐项复制 Recall 字段或 JSON 形状统一**。
- **I 开窗出站最小化（2026-10-04 三，乔生裁定）**：I 的条目/版本/
  历史/溯源机器在存储层全部保留，但开窗包（Bootstrap）的 i 段
  **只返回当前的话**，外加两个极简提示——有历史版本提示版本号
  （`has_history`）、有桶绑定提示存在（`bound_memory_count`）；
  条目指针/历史明细/绑定明细一律不出站，按需走 `i.items.list` /
  `i.item.history` / `relations.list` 显式读取。非必要信息不返回
  给模型侧。

### 8.1 memory_date 默认（江乔生裁定 2026-10-06，F-J-03/D1）

- **词表外标签错误码两层口径**（F-J-21，2026-10-06）：公开传输层
  （schema enum）返回 `SCHEMA_VIOLATION`；`MOOD_CATEGORY_REQUIRED`
  为领域层码（服务层直调/筛选入口）。两层并存是既定行为，不为统一
  错误名放松 enum 校验。
- **creation_mode 必填口径已定、分步实施**（F-J-22，2026-10-06 裁定口径）：
  ①迁移工具已显式 retrospective（历史导入不再是"当下记录"）；
  ②公开合同面必填（schema required+estómago 工具面）**已实施**
  （2026-10-08 收编）：memory.hold 的 required 集含 creation_mode
  （capabilities/input_schemas.py:891-897），且 V2_INPUT_SCHEMAS 为
  公开输入校验唯一运行时正本（schema_for 单源，:1339-1344；历史
  execution_pack v1.1 schema 不再回退）——双源假防护问题随之消除。
  estómago 绑定通道走同一 memory.hold 能力 schema，必填同权。
  当下/补记由调用方声明，宿主不猜；D1 当天自动日期保留不变。
- **当天记录自动日期**：`creation_mode="contemporaneous"` 且未显式给
  `memory_date` 的 hold，服务端在同一次正式提交事务内用 held_at 的
  上海业务日填充 `memory_date`（bootstrap 三日窗与 by_date 因此可见
  当天随手记）。显式日期不被覆盖；**手填日期属于补写**
  （retrospective）——补写未给日期的保持 NULL，不猜；date_confidence
  不因此改写（调用方主张什么就是什么）。同 op 幂等重放返回原事务的
  原日期（不跨午夜重算——原子写回执不变）。

## 9. 删除与破坏性幂等（2026-10-04 裁定）

- Relation/Deletion 语义按 **v2.0 封板**（见 §10 清单）执行；删除链
  退役指自动遗忘链，人类申请/决定/直删链现行。
- **deletion decide 幂等**：决定与 operation 回执同一事务；同
  idempotency key 崩溃/重试后恢复**同一个最终操作**，不重复执行
  破坏性副作用；直删（memory.delete）同性质（atomic_write）。
- 纠错类 atomic_write 能力的 transport 崩溃窗口按领域回执恢复外层
  （completed=补写并重放同一结果；无记录=零痕迹放行重试；不可判定
  才 OUTCOME_UNKNOWN）。

## 10. 域 → 现行正本清单（审计只读这些 + 本文件）

| 域 | 现行正本 |
| - | - |
| 记忆运行时总纲 | 本文件 |
| Relation/Deletion（v2.0 封板） | 根目录 `RELATION_DELETION_ACCEPTANCE.md`（2026-10-01 封板） |
| I 修订模型 | `docs/I_REVISION_MODEL_20260929.md` |
| Source 层 | `docs/SOURCE_LAYER_FIX_REPORT_v1.1.md`（叠加 v1.0 报告） |
| 召回幂等/崩溃安全 | `docs/mariposa-recall-idempotency-crash-safety-2026-09-29.md` |
| 检索/心情裁定 | `docs/mariposa-search-mood-ruling-2026-09-30.md` |
| 2026-10-03 审计与修复 | `docs/audit_20261003/`（REPORT/RESULTS/REMEDIATION） |
| 现行验收 | `docs/memory_runtime/ACCEPTANCE.json`（v1.7） |
| 存储/备份恢复集 | 本文件 §6 + `storage.py`（库+被引用母本+媒体对象） |

离线迁移工具 `migration.py` 属**维护工具链，非现行运行时主链**：
其缺陷按 P3/deferred 处置（2026-10-04 裁定）；当前无真实存量数据，
不影响正式运行，正式启用该工具迁移数据前再行提升优先级。

**其余一切语义类文档均为历史/考古**（含但不限于：
`docs/memory_runtime/BASELINE.md`、`PATH_MAP.md`、`EVALUATION.md`、
`docs/spec_v2/`、`docs/execution_pack_v1.1/`、`docs/closure/` 全部
交付记录、根目录各 AUDIT/FIX/DELIVERY/REVIEW 报告、
`docs/00_local_baseline.md`、`docs/DECISIONS.md`、各 legacy_* 矩阵）。
历史文件与现行冲突时一律以本文件与 §10 清单为准；审计不得从历史
文件推导现行合同。

## 11. 与旧文档的关系

v1.4 的 session 机制、证据分级八类（`authored_event/structured_fact/
word_verbatim/word_paraphrase/word_unverified/raw_verbatim/
relation_reference` + 遗留 `approved_summary`）、安全包装、幂等/审计
继续有效；其遗忘续期、retention 表、摘要通道、"raw 非直接通道"段已
由本文件覆盖。`POLICY_VERSION="mariposa_v1_7"`、
`RECALL_POLICY_VERSION="recall-v1.8"`（2026-10-08 起，§4.1 判断总开关
批；v1.7 及更早的 recall 政策版本为历史）。

## 12. 变更记录

- 2026-10-09（联合审计返修 WP-03，程知行 run-160858 执行包）：D-1=A
  裁定收编——§4 重放段加政策切换交叉注（切换场景=RECALL_POLICY_CHANGED
  结构化拒绝，off 态新生成 unavailable 语义保留）；§8.1 ②改「待下一批
  实施」为已实施+落点（creation_mode 公开面必填、V2 单源 schema）。
- 2026-10-08（`MANUAL_HANDOFF_JUDGE_SWITCH_V1`，江乔生裁定）：新增 §4.1
  判断总开关（持久政策正本、三态语义、关闭≠未配置、人类网页独占写权）；
  关闭模式全候选分页合同（memory.recall.page、冻结结果集、24576 字节/
  10 条页限、码点分片、retrieval_coverage 披露、旧裁剪器仅留判断开启
  路径）；Round2 gate 改 provider 无关外发许可判断（关闭时不要求任何
  provider 许可）；Codex SDK provider 槽位（开放接口进 JudgeProvider，
  与 Jev 同材料不串联、许可分立、缓存键分立）；RECALL_POLICY_VERSION
  → recall-v1.8。实施分批 WP0-WP7（联合审计包
  joint-audit-20261006/GLM_EXECUTION_PLAN_20261008.md）。
- 2026-09-28：v1.7 初版（删除链退役、明开回温、阶段现算）。
- 2026-10-03：并入周家明全量审计修复（票据内容版本绑定、开关重放
  无例外、锁内终态校验、心情留底、Jev 必要角色、批次清场、备份恢复
  集等 23 项，见 `docs/audit_20261003/REMEDIATION.md`）。
- 2026-10-04：收编五项裁定（request_ref 幂等/线性接续、provenance
  收紧、delivery 定义、words scoped BM25、读侧安全语义等价）+
  lexical_terms 合同确认 + deletion 幂等 + Plan 分节；确立本文件为
  唯一正本入口与历史文件降级清单（§0/§10）；确认并清除全部
  合成测试数据（§0 数据状态，§7 未决项随之归零）。
- 2026-10-04（五）：出站瘦身 compact_v1 落地（§7）——纯展示投影
  层 + profile 协商（业务前剥离、同 operation 切换不重做）；md 对话
  转写导入（§6）双方言落地；I 开窗最小化；深耦合拆分三批
  （registry 传输幂等层/recall replay+round2/bootstrap pages）。
- 2026-10-06：裁定落档 §8.1——contemporaneous hold 服务端自动填 held_at 上海日（F-J-03/D1 选项 A；当天记录自动日期，手填日期属于补写）。
- 2026-10-04（二）：按周家明对 Codex 22 条审计的复核修正合同措辞
  ——request_ref 幂等=同一 operation/result identity 而非原样重放
  旧正文（出站仍按当前权限/开关/Judge 重校验，与 RECALL-03 不再
  表面冲突）；幂等键明确为主体+capability/action+request_ref
  （operation_id/session_id 不改逻辑身份）；continue_request_ref
  合同=可验证绑定最新已交付可续 revision（实现不作规定）；
  bootstrap.next 页级安全语义等价（形状不统一）；migration.py
  定位为离线维护工具（P3/deferred）。材料见 docs/audit_20261004/。

## 13. 未决业务项

- forgotten our_words recall：保持 `disabled`。原"PENDING_OWNER_
  DECISION"所涉存量遗留表示已随 2026-10-04 测试数据清除归零
  （见 §0 数据状态）——该问题当前无实例；若未来有真实数据进入，
  是否允许话语跟随桶级遗忘状态检索再行拍板。
