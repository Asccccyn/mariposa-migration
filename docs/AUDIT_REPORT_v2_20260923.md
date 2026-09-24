# mariposa v2.0.1 独立审计与修复报告（第二批）

> 出具：ZCode 独立审计 · 2026-09-23
> 审计基线 HEAD：`ba49465af23aef53da52554a7c28fa2d2d9b0f46`（与
> 《DELIVERY_REPORT_v2_batch1.md》对应）
> 性质：**独立审计**（不复述实现方自查结论，全部声明重新取证）+
> 当场修复。测试全部使用隔离合成数据根（runtime/verification/），
> 未触碰业务库、生产服务、真实数据与任何外部平台（AGENTS.md 约束）。

---

## 1. 执行取证（独立复跑）

| # | 检查 | 结果 |
|---|---|---|
| A1 | 分批 `pytest`（隔离 MARIPOSA_ROOT=runtime/verification/audit-20260923，项目内 basetemp，分 8 批） | **289 passed**（复现报告声明） |
| A2 | E2E `playwright test`（自启隔离实例 18799，独立库/token） | **2 passed**（复现） |
| A3 | 修复后全套件 | **313 passed**（289 + 24 条新增审计回归）+ E2E 2 passed |
| A4 | 覆盖注记核对 | 测试内 V2-XXX 注记采用 `01/02/03`、`01..14` 紧凑写法；机器化 nodeid 映射确未做（NEXT #5 声明属实） |

报告 §4 的"约 70 已测/16 部分/23 未覆盖"经逐组源码级抽查**大体成立**，
但个别"已测"仅在 v1 路径成立（见 §3 F-RET-09），且 BLOCKERS B8 有一处
**不实声明**（见 §2）。

## 2. 不实/误导声明清单（审计更正）

| # | 声明 | 核实结果 |
|---|---|---|
| F-DOC-1 | BLOCKERS B8 / DELIVERY_REPORT M08-09 备注："表结构已建（raw_binding_ranges/raw_context_grants 迁移 11）" | **不实**。两表不存在于 schema.py 任何迁移（1..11 均无）与全部代码；运行库实测仅有 raw_conversations/raw_messages/memory_raw_refs/import_jobs。仅 errors.py 预埋 BindingStale/RawContextConfirmationRequired 两类从未 raise。已在 BLOCKERS.md 更正。 |
| F-DOC-2 | §4 表 RET-09 标"已测" | **误导**。防饥饿游标仅 v1 闲置扫描有实现与测试（workspace/service.py）；v2 到期队列 `due_items` 是 `LIMIT 50` 无游标，跳过项占满首页后其后到期项饥饿，且无任何测试。本批已修（见 F-A2）。 |
| F-DOC-3 | §4 表 REV-13 标"已测" | **半真**。release 路径 `_reverify` 完整；owner-continue 终裁路径只比对版本号，缺字段 hash/到期/线索/禁词核验——打开续期后仍可凭旧候选执行遗忘（RET-08 在该路径失效）。本批已修（F-A3）。 |

## 3. 真实缺陷清单（全部合成数据复现确认后修复）

### A 组 · 审查闭环（P0）

| # | 缺陷 | 位置 | 修复 |
|---|---|---|---|
| F-A1 | keep 终局后到期队列行残留（`mark_retained` 不同步队列）→ 后续 generate 撞 RETAINED_FINAL **整批崩溃**（RET-11/RET-09 双违反；复现脚本确认） | memory/retention.py、workspace/review.py | `mark_retained` 终局时同步清队列；generate 批处理对终局目标记 `skipped` 不中断（显式点名仍如实抛错） |
| F-A2 | v2 到期队列无游标分页：55 条不可建项占满首页即永久阻塞后续到期项（RET-09） | workspace/due_queue.py | `due_items(after=)` keyset 分页；generate 翻页消费（上限 20 页），回归测试用 55 条阻塞行+1 条真实桶验证 |
| F-A3 | `_owner_continue` 核验缺失：打开续期（RET-08）/分类改永久后仍可执行遗忘（复现确认：due=10-13、is_due=False 仍 forgotten） | workspace/review.py | continue 与 release 同标准：`_reverify_source`（版本+字段hash+到期）+ 快照外新线索拦截 + 禁词复检 |
| F-A4 | stale/failed/withdrawn 状态从不写入：源变化/续期后审查项永久卡 in_review，`_eligible_target` 又拒绝新建 → 同桶审查**死锁**（规格状态机"未执行→stale"未实现） | workspace/review.py | submit 失败分支（PROPOSAL_STALE/NOT_DUE）落 stale；generate 前自愈扫描 `_stale_superseded` |
| F-A5 | 共同话语终裁可被路由绕过：林石见可选 escalate_owner→乔生 keep/continue（REV-05 边界依赖审查者选对升级目标） | workspace/review.py | 双保险：submit 拒绝对 jiaming-owned 线索 escalate_retain/owner；decide_retention 按线索（而非仅状态）限定周家明 |
| F-A6 | v2_review_items 状态回写无前置条件（跨库非原子窗口可产生 forgotten 被覆盖等矛盾态） | workspace/review.py | 回写带 `AND state=...` 守卫，失效即 REVIEW_STATE_MOVED |
| F-A7 | 委托 allowed_actions 从不被校验（窄权限委托形同虚设）；默认委托缺 escalate_jiaming | workspace/review.py | revise/submit 按动作校验委托；默认委托就地升级（仅认旧默认清单） |
| F-A8 | 死代码：`decide_retention` 空壳存根（651d647 重构残留，被后定义覆盖）；memory/service.py 头部整段重复定义 | 两文件 | 删除 |
| F-A9 | `_check_summary_terms` 大小写敏感（"ai"绕过"AI"）；revise 不校验 summary_body 类型（int→500） | workspace/review.py | lower() 比对 + 类型拒绝 |

### B 组 · 计划（P0/P1）

| # | 缺陷 | 修复 |
|---|---|---|
| F-B1 | `plans.update` 的到期队列写（replace_pending）在 BEGIN IMMEDIATE **之外**——plans 回滚后队列与真源不一致 | 移入事务 |
| F-B2 | `plans.create(state=done/cancelled)` 落锚点但**不入队**——直建终态计划永不进到期审查 | 事务内入队；回归测试 |

### C 组 · v1/v2 双轨隔离（P0）

| # | 缺陷 | 修复 |
|---|---|---|
| F-C1 | v1 遗忘闭环可处理 v2 分层桶：v1 扫描不识别 retention 行、v1 审批不查任何 v2 保留线索（回忆/共同话语/关键词）——乔生单人即可经 v1 批准遗忘带共同话语的 v2 桶（REV-05/06/07 双轨漏洞） | 三层封堵：v1 `_skip_reason` 跳过 v2_managed；v1 `submit` 拒绝 V2_MANAGED_TARGET；`apply_forget_approval` 生效前拒绝。v1 闭环仅服务无 retention 行的旧桶（D17 祖父条款一致） |
| F-C2 | v1 `workspace.decide(defer)` 无权限检查——worker 可挂起任意 submitted 提案 | defer 纳入 _APPROVERS |

### D 组 · 检索（P1/P2）

| # | 缺陷 | 修复 |
|---|---|---|
| F-D1 | `search()` related_of 命中不与关键词命中去重（SEARCH-05 违例） | seen 集合去重 |
| F-D2 | matched_fields 标注失真：v1 桶 why/meaning 层命中一律标 `event_text` | 投影层新增 `whitelist_body` 列（迁移 12），检索按实际命中成分标注 event_text/summary_body/forget_tags/legacy_projection；保持"检索只读投影表"架构门禁 |
| F-D3 | `occurred_start/end` 参数被静默吞掉（schema 接受、从不落库、不参与筛选） | 迁移 12 落列；hold 持久化；`memory.get` 返回；日期筛选支持 occurred 区间重叠（按日期前缀比较） |
| F-D4 | 空 query 走语义通道可产生无依据"伪命中" | phrase 为空不做语义匹配 |
| F-D5 | v1 遗忘投影不含 forget_tags（与 v2 路径不一致）——维持现状：v1 payload 无 tags 概念，D21 声明仅覆盖 v2；结构化分类/日期/心情标签命中不受影响 | 记录为已知差异（不修） |

### E 组 · 开窗（P1）

| # | 缺陷 | 修复 |
|---|---|---|
| F-E1 | snapshot 指纹缺**业务日期**成分：23:58 取包→00:05 重放返回 unchanged 薄响应，跨日窗口错位；next_page 跨日用新窗口拼旧快照（BOOT-05/07 违反） | 指纹加入业务日期/时区/规则版本/分页常量；快照行存 business_date；next_page 跨日抛 SNAPSHOT_STALE |
| F-E2 | 指纹声称含记忆表示版本但 SELECT 后丢弃（死代码）；纪念日"定义"改名不失效 | representation_state 与 anniversary_definitions 入指纹 |
| F-E3 | I 写入 expected_version=None 盲覆盖（并发一致性弱点；权限无影响） | 记录为已知限制（不修，I-01 权限边界完好） |

### F 组 · 幂等/契约/入口（P1/P2）

| # | 缺陷 | 修复 |
|---|---|---|
| F-F1 | handler 抛 MariposaError（干净业务拒绝，事务已回滚）后幂等记录永滞 running：60s 内 Busy、之后 OUTCOME_UNKNOWN——把参数错误伪装成疑似崩溃逼人工对账 | except 分支落 failed（同 key 修正即可重试）；回归测试验证恰一记录+恰一副作用 |
| F-F2 | 结构化错误码从未到达客户端：`MariposaError(code=...)` 的 code 只进 detail，HTTP/MCP 响应一律报类默认（FORBIDDEN 等）——SCHEMA_VIOLATION/NOT_DUE/V2_MANAGED_TARGET 等全部不可编程区分（OPS-02 缺口） | code 提升为实例属性（保留 detail 兼容旧读法） |
| F-F3 | 校验器不查嵌套 required / 数组 minItems / 数值边界（B03"嵌套校验"声明偏乐观）：`our_words:[{"speaker":"jiaming"}]` 缺 text 可通过、`categories:[]` 可通过 | `_matches` 补齐（含 minProperties）；schema 中数组改用 minItems；新增 workspace.review.revise schema |
| F-F4 | app 入口非法 JSON/非对象 body 静默当 `{}` 执行 | 400 INVALID_JSON / INVALID_JSON_BODY |

### 已核对无误的重点声明（抽查）

RET-01/02/03/13/14/15/16 自然日语义（date+timedelta、业务时区、日界），
RET-05 被动读不续期（全后端写点排查），VIEW-01..05 回忆链路，REV-01/02/03/04
权限与材料，I-01/02/03，BOOT-01/02/03/04/07/08，OPS-01/03/04/06/09，
PLAN-01..05/08/09/11 核心锚点语义——均与规格一致（7 份组级审计详情见
各子任务；本报告仅列偏差）。

## 4. 覆盖率口径更正（对报告 §4）

- RAW ×13：**0 实现**（且"表已建"不实，见 F-DOC-1）。
- RET-09：v1 路径已测、v2 队列路径本轮补齐（F-A2）。
- REV-13：release 已测、owner-continue 本轮补齐（F-A3）。
- REV-05：字面检测+状态限定已测、路由绕过本轮封堵（F-A5）。
- SEARCH-05：recall 已测、search() 关联去重本轮补齐（F-D1）。
- 修正后直接测试证据约 **73/109**（新增 24 条回归中约 12 条对应矩阵条目）。

## 5. 修复清单（文件级）

- backend/mariposa/errors.py、schema.py（迁移 12：occurred 两列、
  snapshot.business_date、retrieval_documents.whitelist_body）
- memory/retention.py、memory/service.py（含重复定义清理）
- workspace/review.py、workspace/due_queue.py、workspace/service.py
- plans/service.py、retrieval/search.py、retrieval/projection.py、
  retrieval/rebuild.py、memory/extras.py、memory/listing.py
- bootstrap/service.py、capabilities/registry.py、
  capabilities/input_schemas.py、app.py
- tests/unit/test_audit_fixes_v2.py（新增 24 条，每条注明对应规则号）
- tests/unit/test_v2_recall.py（SEARCH-11 用例改走 v2 审查闭环，测试内
  注明 superseded；断言未放宽）
- docs/BLOCKERS.md（B8 更正）、docs/NEXT.md、本报告

## 6. 复跑（PowerShell，隔离环境）

```powershell
$env:MARIPOSA_ROOT = "D:\mariposa\runtime\verification\audit-20260923\isolated-root"
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.pytest_tmp\audit
Remove-Item Env:MARIPOSA_ROOT
npm --prefix apps\web run test:e2e   # 自启隔离实例 18799
```

## 7. 未做（第二批范围，按 NEXT 优先级）

1. RAW-01..13 全组（从零：表/偏移/票据/preview/expand/评测关卡；含
   B8 附注的两处自报置信直绑面收口）。
2. 语义 v2 投影切换（SEARCH-14/15 影子重建）。
3. v1→v2 迁移逐条映射（REC-10/OPS-10；注意 migration.py 目前不写
   date_gap retention 行——行为安全（is_due=False）但文档承诺未兑现）。
4. 前端 v2 页面 + 手机 390px E2E（OPS-08）。
5. 109 条矩阵机器化 nodeid 证据链（OPS-11）。
6. MCP 显式 manifest（当前正向映射仍是机械下划线替换，202 名实测无碰撞）。
7. OPS-02 的 173 项无 schema 能力渐进补齐（其中 ~110 项真实读写能力）。

## 8. 边界声明

- 未动业务库 runtime/formal（迁移 12 将在其下次启动时自动应用，
  全部为 ADD COLUMN 非破坏操作）；未连接外部平台；未迁移真实数据。
- 审计/测试全部运行于 runtime/verification/audit-20260923 隔离根。
- 未提交 git（修复在工作树，待复核后一并提交）。
