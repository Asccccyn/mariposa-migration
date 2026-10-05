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
| E1 | coverage"gap>0 一律不签 complete" | **部分采纳** | 修复中途发现与 S19 收口裁定（江乔生指示、test_closure_s19_pool 三条测试固化）冲突：无分类桶=非参与成员不构成不完整。已按原裁定保留；实质修复保留在 DataGap 桶纳入扫描（v1 桶现在可被检索）。若重审认为 gap 应降级 coverage，需先推翻 S19 裁定 |
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
