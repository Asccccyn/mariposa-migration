# 2026-10-05 第三方全量审计：修复自审与 Codex 交接单

- **审计基线**：`01edb28`（=当日生产运行代码，工作树干净）
- **修复提交**：`b4b3a06`（+本文档与 UI 文案微调的补充提交）
- **测试**：全量 1069 通过 / 1 跳过 / 0 失败（unit 966 + integration 36 + acceptance 67）；新增回归 `tests/unit/test_audit_20261005_fixes.py` 19 条
- **方法**：第三方审计（不采信既往结论），三路并行深读全部业务模块；所有 P1/P2 发现由主审人回源码二次复核后确认；修复后逐条 grep 验证落地点位（见本文档 A 表）

给 Codex 的话：每一项都给了 文件:行 与回归测试锚点；"勘误"节列了**没有修**的项与理由——审我们时先看勘误，别把已声明保留项当漏修。

---

## A. 修复清单（逐条落地证据）

### P1（5/5 修复）

| # | 发现 | 修复落点 | 回归测试 |
|---|---|---|---|
| P1-1 | `source.import` 接受宿主任意路径（绕过上传隔离；无大小预检） | 圈禁：`capabilities/registry.py` `_source_import`（暂存目录对全 owner；目录外宿主路径仅 qiaosheng，`SOURCE_PATH_FORBIDDEN`）；大小预检：`source/importer.py` `import_file`（stat 先于复制，超 `SOURCE_UPLOAD_MAX_BYTES` 结构化 413） | `TestSourceImportConfinement` 4 条 |
| P1-2 | words 索引读路径非原子重建（并发撞主键 500） | `retrieval/words.py`：`rebuild_words_index` 未持事务时自包 `BEGIN IMMEDIATE`；`ensure_index_current` 改专用写连接 + 锁内二次核指纹（`_index_fresh`） | `TestWordsIndexConcurrency`（6 线程×12 搜索） |
| P1-3 | v1 存量桶（held_at NULL）被跳过且 coverage 虚签 complete | `recall/pipeline.py:308` 批量 facts + `DataGap → CORE 最小允许集纳入扫描`（与 `retrieval/search._allowed_field_kinds` 同语义）。**coverage event 维持 S19 裁定语义**（完整=池取尽；PolicyError 桶=非参与成员，见勘误 E1） | `TestCoverageHonestSignature` |
| P1-4 | 删除申请/撤回/驳回、revoke_binding、handoff_write 无审计 | 同事务 `audit.record`：`deletion/service.py`（requested/withdrawn/rejected）、`identity/service.py`（binding.revoked）、`time_context/service.py`（handoff.written） | `TestAuditTrailOnSensitiveActions` 2 条 |
| P1-5 | recall/search 热路径逐桶 N+1 连接 | `recall/phase_policy.py` 既有 `phase_from_facts` 补 `now` 默认值并成为单入口（`phase_of` 默认路径委托）；`pipeline.py:308` 与 `search.py:23` 均改 `facts_for_many` 批量装载 | 既有 966 unit 全绿（行为等价）+ 冒烟 |

### P2（8/8 修复）

| # | 发现 | 修复落点 | 回归测试 |
|---|---|---|---|
| P2-1 | reconcile 可判死并发中的写占位→双写；无审计 | `maintenance/service.py`：服务端下限 `MIN_RECONCILE_STALE_S=600`；UPDATE 事务内写 `maintenance.idempotency.reconciled` 审计 | `TestReconcileFloorAndLimitClamps` 前 2 条 |
| P2-2 | 数值参数无钳制（负 limit=LIMIT -1 全表倾倒；非数字=500） | `capabilities/registry.py` `_int_arg`（非数字→`INVALID_ARGUMENT` 400；越界钳制）——替换全部 17 处裸 `int(a.get(...))` | 同上后 2 条 |
| P2-3 | 同步 handler 阻塞事件循环（幂等待 8s / DB busy 30s 冻结全服务） | `app.py` invoke 与 `capabilities/mcp_adapter.py` tools/call 改 `asyncio.to_thread(registry.invoke, ...)` | 既有 HTTP/MCP 测试全绿（TestClient 走真实线程池） |
| P2-4 | OAuth：开放注册无上限；登录页不显示回调；PKCE 可选 | `oauth.py` `MAX_REGISTERED_CLIENTS=500`（超限 503 `REGISTRATION_CAPACITY`）；`app.py` 登录页展示 redirect_uri；authorize 无 `code_challenge` → 400 | `TestOAuthHardening` 2 条 |
| P2-5 | recall 运行库清理不删子行（无界增长） | `recall/store.py` `purge_expired`：超 TTL×2 的 session 连子行（rounds/revisions/attempts/receipts/candidates/round1_receipts）按 FK 序物理删除 | `TestRecallPurgeDeletesChildren` |
| P2-6 | 一条漂移绑定毒死 `open_for_memory` 全部 | `source/binding.py`：`Forbidden(SOURCE_RANGE_TOO_LARGE/NOT_PATH)` 降级单绑定 `unresolvable`（带 reason），其余照常 | `TestOpenForMemoryToleratesDrift` |
| P2-7 | workspace 库死表（引用 runtime 库才有的 recall_sessions） | `schema.py` WORKSPACE_MIGRATIONS 追加 (7)：DROP 全部死 recall_* 表（fresh 与既有库均收敛） | `TestWorkspaceDeadTablesGone` |
| P2-8 | 91MB 模型缓存以字面 `D:\` 路径提交进 git（610317c）；测试根因未修 | `git rm --cached` + 删工作树副本 + `.gitignore` 防复发；根因：`tests/acceptance/test_gate_cases.py` 两处 `setdefault(D:\...)` 改 `monkeypatch.setenv` 指本地 canonical 缓存；`tests/conftest.py` 全局 `FASTEMBED_CACHE_PATH` 兜底 | 全量跑完 `D:\` 未再生成；`.gitignore` 含转义路径 |

### P3（12/12 计划项修复；另 5 项小观察声明保留，见勘误）

| # | 发现 | 修复落点 |
|---|---|---|
| P3-1 | `get_message` 裸 sequence 比较（重号时邻居不确定） | `source/query.py`：`(sequence, id)` 双键决胜 + 稳定排序（确定性修复；完整 parent-path 语义见勘误 E8） |
| P3-2 | `navigate` 写候选缺 F03 锁内复验 | `recall/service.py`：`require_session_in_tx` + 状态机 + 归属复验进 `BEGIN IMMEDIATE`，revision 取 fresh |
| P3-3 | live ingest 聚合时间戳漏 `live_superseded=0` | `source/ingest.py` `_refresh_aggregates`：MIN/MAX 子查询补过滤（与 message_count 口径一致） |
| P3-4 | continuation 在输出预算后注入（绕过 24KB 上限） | `recall/service.py` start 与 `round2.py` 返回前各补一次 `_enforce_output_budget` 终检 |
| P3-5 | recall_receipts 混用两种时间戳格式 | `recall/pipeline.py` `mark_round1_complete_tx`：`datetime('now')` → ISO+tz 参数 |
| P3-6 | `memory.get` 版本行缺失 → TypeError 500 | `memory/service.py`：结构化 NotFound（带 memory_id/version_no） |
| P3-7 | 4 处 `source_msg:` 解析 `fetchone` 无撞号守卫 | `memory/our_words.py` ×3 + `retrieval/words.py` ×1：`fetchall` + 多行命中 `SOURCE_MESSAGE_AMBIGUOUS`/invalid（fail-closed） |
| P3-8 | media `_staging` 字典只懒清不扫 | `media/service.py` `upload_prepare` 顺手全量清扫过期项 |
| P3-9 | `_record_failed_import` upsert 缺 completed 守卫 | `source/importer.py`：`DO UPDATE ... WHERE status <> 'completed'` |
| P3-10 | gate XFF 取首元素（可伪造轮换 IP） | `gate.py`：取末位元素（可信代理追加位） |
| P3-11 | 失败状态淘汰按插入序非 LRU | `gate.py`：失败计数更新时 pop-reinsert 移尾 |
| P3-12 | `issue_stream_token.py` pbcopy 整段重复 + `.env.example` 停留 v1.1 | 去重；模板按现行 `config.py` 重写 |

### 有意行为变更（既有测试相应更新，均为断言级更新、无删除）

1. `tests/unit/test_gate_access.py`：XFF 回退期望末位元素（原首元素）。
2. `tests/unit/test_oauth_dynamic.py`：refresh rotation 流程补 PKCE challenge/verifier。
3. `tests/unit/test_md_transcript.py`：filename=None 用例改 qiaosheng 直读（jiaming 现被圈禁拒）。
4. `tests/unit/test_p1_fixes.py`：对账用例种子年龄 120s→700s（服务端 600s 下限）。
5. `tests/acceptance/test_gate_cases.py`：FASTEMBED 指本地缓存；顺带删除一处逐字重复的断言（测试质量审计 F5 提及）。

---

## B. 勘误：没有修的项与理由（审计时请从这里开始）

| # | 项 | 状态 | 理由 |
|---|---|---|---|
| E1 | coverage"gap>0 一律不签 complete" | **部分采纳** | 修复中途发现与 S19 收口裁定（江乔生指示、test_closure_s19_pool 三条测试固化）冲突：无分类桶=非参与成员不构成不完整。已按原裁定保留；实质修复保留在 DataGap 桶纳入扫描（v1 桶现在可被检索）。若重审认为 gap 应降级 coverage，需先推翻 S19 裁定。**严重度校准（2026-10-05 晨，所有者确认后复核）**：`memory.hold` 服务层强制 `CATEGORY_REQUIRED`（v1.7 §3.2，"没有未分类默认值"），且 categories 必填 ⇒ 恒走 v2 路径 ⇒ held_at 必写。全库唯一 `INSERT INTO memories` 在 hold 链路。故**无分类桶与缺 held_at 桶经正式写路径均不可能产生**——两类"坏桶"只存在于测试夹具裸 SQL 与假设性的旧版存量导入。因此：① S19 语义与审计激进版在生产数据形态下等价（真实库 gap 恒 0），保留裁定零成本；② P1-3 修复的实际定位是**防御性**——为将来导入真实旧数据预铺，当前库无存量对象，实际严重度低于评估的 P1 一档；③ E1 的"冲突"实质是测试造脏行场景之争，不影响生产语义 |
| E2 | `recall/replay.py:260` 仍逐桶 `phase_of` | 保留 | 一次性 replay 低频路径，N+1 影响可控；主线（pipeline/search）已批量 |
| E3 | words 检索性能（每次全表指纹扫描 + 全池进内存 BM25） | 保留 | P1-2 只修正确性（并发崩溃）；性能优化是独立工作项，当前数据规模（个人库）不构成故障 |
| E4 | 缺失的 input schema 本体未补（activity.list/media.list/memory.list/source.search 等） | 部分 | 以 handler 侧 `_int_arg` 钳制收口（防全表倾倒+结构化 400 已达成）；schema 声明补齐留给契约整理批次 |
| E5 | OAuth authorization code 明文入库 | 保留 | TTL 300s + 一次性消费 + PKCE 强制后暴露面极小；哈希化需换表结构，收益不成比例 |
| E6 | `verify_password` 双身份同密码时静默映射 qiaosheng | 保留 | 需要产品裁定（提示 or 拒绝），不宜代理决策 |
| E7 | `/health`、`/oauth/*` 计匿名档（成功也占额度） | 保留 | 部署形态（tunnel 后单用户）下的设计选择 |
| E8 | `get_message` 跨快照 sibling-branch 邻居语义 | 部分 | 已做确定性修复（不漂移）；完整 parent-path 邻居 = open_range 级改造，另立工作项 |
| E9 | git 历史仍含 91MB（610317c） | 待裁定 | 彻底清除需历史改写+强推 origin，影响所有克隆；等所有者批准后执行 |
| E10 | 生产服务未重启（仍跑 01edb28 旧代码） | 待裁定 | 无人值守夜班不重启生产；重启命令一行，迁移 7 为 DROP IF EXISTS 死表（测试库验证低风险） |
| E11 | 测试结构性弱项（judge 全局假实现 / 稠密向量全 mock / 真实导出 ingest 默认 skip） | 保留 | 属于产品验收策略（spec 声明"结构级用 fake"），非本批缺陷修复范畴；见 WP07 消融欠账 |
| E12 | `oauth_clients` count-then-insert 竞态（上限近似） | 保留 | 超限量 ±并发数级别，个人部署无实害 |

---

## C. Codex 复审建议入口

1. `git show b4b3a06 --stat` 总览；逐文件 diff 对照 A 表行号。
2. 高价值复审点（我们自己认为最值得挑战的）：
   - `ensure_index_current` 的双连接模式（调用方连接只读检测 + 专用连接写）有无死锁/幻读窗口；
   - `_int_arg` 钳制语义（越界钳制 vs 拒绝）是否与既有 schema 校验约定冲突；
   - `to_thread` 化后 `monkeypatch` 类测试的有效性（线程内读模块属性的可见性——实测全绿，但值得双查）；
   - workspace 迁移 7 在"生产 workspace 库死表**非空**"场景的行为（DROP 直接成功，无数据保留——死表按定义无代码写入，但值得确认）；
   - pipeline `facts_for_many` 对 2 万桶 IN 子句的 SQLite 参数上限（3.12 自带 sqlite ≥3.45，上限 32766——已核，留档）。
3. 勘误表 E1（S19 语义冲突）是本批唯一的中途方向修正，请独立判断取舍。


---

# 追加：b4b3a06 之后的批次清单（Codex 全量审计范围更新，2026-10-05 深夜）

**当前审计基线：`4dc6c04`（main）**。⚠️ 本仓 git 历史已于 909d535 改写（清除 91MB 模型缓存路径）——**旧克隆必须重新 clone，不能 pull**；全量备份在 `~/Data/mariposa-pre-rewrite-backup-20261005.bundle`。测试约束见 AGENTS.md（前台/分批/资源有界）。

## 后续批次清单（全部含"她的裁定→落地"因果，审我们=审裁定执行是否走样）

| 提交 | 内容 | 审计重点 |
|---|---|---|
| f81d083 | 回滚心情窗口擅自放宽 | V2-REC-04 原样恢复；该放宽本身是越权产物（已向她认错）——验证无残留 |
| 443524f | why_remember 全链删除+标题必填30+心情窗口放宽（后撤）+编号初版 | why 在 schema/服务/transport/迁移列/离线脚本零残留；标题双门 |
| 2d66282 | 三直达入口（by_category 新增/by_date/by_emotion 重写，标题优先）+编号纯五位数 | keyset 分页正确性；标题卡片不带正文 |
| a596395 | memory.versions.read/list 删除 | 内部版本行保留但无对外可见面；i.versions.read 不受影响 |
| fe73747 | semantic provider 故障降级 | 关键词主路径存活+degraded 如实；不伪装 hybrid |
| f42d6bb | mood.write 整体删除（终裁：不能补写） | 全库无事后写心情路径（含导入侧） |
| d14a24e→b810ba1→75378ab | 心情词表三轮演进→终版 | 见下方冻结语义 |
| aa624c9 | estómago 内置绑定脚本 | 白名单边界（泄漏面=hold+vocab）；发币顺序约束写进脚本头（旧代码运行中发币=migrate 提前致坏）|
| 4dc6c04 | 修复两个恒真空转的 dense 禁检测试 | 探针真实存在（mood_note/title/words 三源） |

## 冻结语义（审计判据，偏离即发现）

1. **证据边界**（她的冻结句）："Mariposa 记录当时留下的证据，不替过去的人补写内心。对过去的解释只能作为后来发生的新记忆保存。"当时层=日期/分类/当下心情/事件/我们的话/标题/原文；后来层=回忆（带作者+写入时间，不反向篡改当时层）。
2. **心情**：标签槽只存 7 大类（开心/爱/生气/吃醋/悲伤/渴望/不安），仅建桶当下、仅周家明、≤3 个、词表外 MOOD_CATEGORY_REQUIRED 拒；子心情=mood_note 自由文字可写可不写、永不参与任何检索（BM25/dense/RRF/直达筛）；不能补写（mood.write 已删）；延后写桶心情留空；词表正本=memory.mood.vocab。
3. **编号**：纯五位 00001-99999 全局号段，bucket_id_counters 持久计数永不复用（删除不回卷——曾有缺陷已修，见回归 test_no_reuse_after_delete）；旧 mem_ 并存。
4. **直达检索**：by_date 按 memory_date（真实发生日期）；by_category/by_emotion 标题优先+keyset；by_emotion 空标签=全量；召回 mood_tags 过滤=大类直筛同语义。
5. **dense**：向量=whitelist_body（仅正文）；投影哈希绑定旧向量排除；生产已按五步验收启用。

## 已申报勘误（勿当新发现，欢迎复核修复质量）

- E1-E12（第一版勘误表）全部仍有效；
- 恒真测试事件（4dc6c04 修复）：删 why 连带删探针致两个 dense 禁检测试空转——她的"不能假设旧问题已消失"指正抓出；欢迎扫其余测试是否同类；
- "why 进全文索引"系早期误报（build_full 零调用死代码）。

## 建议挑战点（我们自己最想被攻的位置）

- _next_bucket_id 在 BEGIN IMMEDIATE 内的并发正确性与 99999 上限语义；
- estómago builtin binding 的 allowlist 与 registry.invoke 检查顺序（allowlist 在 require_any 之后——jiaming 主体+白名单的交集行为）；
- 标题更新与投影哈希：title 不在向量内，但 retrieval_documents.search_text_hash 是否覆盖 title 变更——字段投影与向量哈希口径是否自洽；
- git 历史改写后仓库完整性（fsck / 备份 bundle 可恢复性抽查）；
- 全套件 1095 绿的真实性（按 AGENTS.md 分批跑）。

---

# 追加：审计 1005B 根因修复批交接（2026-10-06，基线推进至 479a16d）

**当前审计基线：`479a16d`（main，已 push）**。上表基线 4dc6c04 之后的批次（审我们=审修复是否忠实于根因）：

| 提交 | 内容 | 审计重点 |
|---|---|---|
| 91aead4 | 第一批：心情窗口宁拒不猜/MCP 面默认 compact_v1/词表单一事实源/compact 字段名 bug/白名单补 hold.status/清扫化石 | 见下"修复声明"R1-R5 |
| estómago 36fd6df | 接口根因适配：classifyError 按httpStatus 优先/mood 前置拦截/词表动态拉取 | estómago 仓（本地无远端）；记录见其 docs/specs/IMPLEMENTATION_MAP.md 2026-10-06 段 |
| 479a16d | 第二批：MCP 信封协商/WORKSPACE 迁移链压缩→单迁移 8/死符号 27 项/recall 四函数拆分/tools/list 隐占位 | 见下 R6-R9 |

## 修复声明（根因视角，每项含回归锚点）

- **R1 词表单一事实源**：MOOD_CATEGORIES 之外的全部副本（5 处文案、input_schemas 两处枚举、上限 3）改为动态生成/常量同源（MOOD_TAGS_MAX）；vocab 加 `version` 字段。曾发生"词表 7 类、文案写固定 8 个"。锚点：test_root_cause_20261006.py::TestMoodVocabCopyConsistency
- **R2 compact 字段名**：_JUDGE_DROP 的 confidence_kind 曾误写 provider_confidence_kind（删除是空操作）；恒 null 的 provider_confidence 一并删。锚点：TestCompactJudgeDrop
- **R3 MCP 面默认 compact_v1**：invoke 增 default_output_profile（仅 mcp_adapter 注入 compact_v1；HTTP 面 legacy 不变）；显式 profile 语义不变；默认提升对不支持能力静默回退。此前 compact 纯 opt-in 且无消费者传参=瘦身从未生效。锚点：TestMcpDefaultCompact（5 条）
- **R4 心情窗口宁拒不猜**：mood 给了而 creation_mode 缺省→MOOD_WINDOW_REQUIRED（不再默认 contemporaneous——防补记心情绕过 V2-REC-04）；不做 memory_date 交叉校验（按日期猜会误伤"晚上记当天早上"）。锚点：TestMoodRequiresExplicitCreationMode（4 条）
- **R5 estómago 白名单**：脚本+护栏注释+生产库 bdg_2c478128e36c8ea01d36 事务内更新（白名单=[hold, hold.status, vocab]，每请求读库即时生效）。锚点：TestEstomagoAllowlistRecovery
- **R6 MCP 信封协商**：params._meta.content_envelope="single" 省略 content 完整 JSON 副本（留指针行）；默认双份保兼容（不赌客户端读取面）；出站统一紧凑分隔符。锚点：test_envelope_and_toolslist_20261006.py::TestEnvelopeNegotiation
- **R7 WORKSPACE 迁移链压缩**：[4,2,1,6,7]（fresh 库建了又删净 0 表+70 行 DDL 与 RUNTIME 迁移 1 重复）→单迁移 8=DROP 并集。三形态验证等价：fresh（applied=[8] 零死表）/生产（[1,2,4,6,7]+8 落库档案延续）/半途模拟（[1,2]+死表→一次收敛）。旧编号退役不复用（迁移 1 曾被就地追改的教训）。锚点：TestWorkspaceMigration8（子进程）
- **R8 死符号 27 项**：全仓词边界 grep+AST 双验证零调用后删（9 月两次 refactor 拆尾）。**ProviderUnavailable 删后恢复**——T-EXT-02 验收锚定的合同预留类不是死代码（教训已写入类注释）。content_role_banner 未删：contracts/recall_runtime.v1.schema.json 引用（契约引用≠零引用）
- **R9 recall 拆分**：_run_round_compute→_words_dense_fuse/_judge_pass；_round2_body→_round2_resolve_offset/_round2_judge_cards。逐字搬移零语义变更，裁定注释随行；提交事务段内聚不拆。**拆分过程曾引入 3 个边界 bug（漏 return/变量漏传）均被测试当场抓住**——等价性由 1116 全量背书，欢迎对照 91aead4 前原文逐块攻

## 已申报勘误（勿当新发现）

- **有意保留**：memory.list/by_tag 全文内嵌、source.search 全文不截断、capabilities.list 17KB、memory.get 微字段——瘦身机会但**需契约裁定**（改 handler 返回体同时影响 HTTP 消费面：网页/estómago/CC），未经她批准不动
- **信封 single 模式当前收益=0**：无客户端传 _meta.content_envelope（协商机制就绪，等客户端升级）——不删默认双份是防周家明客户端只读 content 的兼容风险，属"不赌"决策
- estómago AUTH_LOCKED 判 retry_later：锁定期（指数退避）可能长于其有界重试窗口——语义上"可等"优于"判死"，如认为应 dead 请提裁定
- 生产 mariposa 已重启载 479a16d（pid 35588）；estómago 无常驻服务，下次启动自动生效

## 建议挑战点（我们自己最想被攻的位置）

1. **迁移 8 等价性**：用更多库形态攻（如 applied=[4] 单独、applied 含 3/5 等未存在编号、schema_migrations 有 8 但表残留）
2. **MCP 默认 compact 的影响面**：compact 白名单（recall 家族+source.search+bootstrap）删的字段清单 vs 周家明实际消费——compact.py 只删诊断/可推导字段，欢迎逐字段挑战"这个真没用吗"
3. **_meta 协商不污染业务**：content_envelope 只在 mcp_adapter 层消费（不进 arguments/schema 校验/payload 哈希）——验证它与 output_profile 的剥离路径等价
4. **recall 拆分等价性**：git diff 91aead4..479a16d -- recall/ 逐块对照搬移（我们声明零语义变更）
5. **estómago classifyError 优先序**：httpStatus 在前 code 在后——找"状态码可重试但结构化码语义是永久冲突"的组合（如有，优先序应反转）
6. 全量 1116 通过+1 跳过真实性（按 AGENTS.md 分批前台跑）


---

# 追加：联合审计返修 B3 批（2026-10-06，GLM 执行，基线推进至 cd66e52）

**当前审计基线：`cd66e52`（main）**。上一批基线 5f91caf（=联合审计冻结快照）。依据=联合审计包（`~/Projects/devspace-workspace/joint-audit-20261006/`）+ 林石见返修任务书 B3 批。**既有合同→本次实现修正**（非新裁定落地）：

| 提交 | 内容 | 审计重点 |
|---|---|---|
| cd66e52 | B3 七项：purge/reset 三子表（F-J-04）/raw_msg 读侧 invalid（F-J-05）/直达 NULL 三态游标（F-J-19）/placeholder 大类（F-J-20）/迁移工具拒无分类（F-J-23）/删除幂等落穿结构化（F-J-26）/continuation 合并提示（F-J-27） | 回归 tests/unit/test_joint_audit_b3.py 15 条；test_product_align 冻结断言按 F-J-23 新语义更新（applied=0+categories_missing，非自动 daily） |
