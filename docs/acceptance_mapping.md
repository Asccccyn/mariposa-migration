# 验收映射表

> 本表把 01 工程文档的关键场景映射到实际测试文件/测试名与执行命令，并如实标注
> 未实现/阻塞/预留项。**不把 reserved/blocked 计入完成**。
> 执行命令：`.venv\Scripts\python -m pytest tests --basetemp=.pytest_tmp -q`

## 1. 遗忘闭环（Phase 2 出口，§8.5 标志性测试）

| 场景 | 测试 | 状态 |
|---|---|---|
| 工具人候选永远不是正式记忆（审批前桶不变） | `test_forget_loop.py::test_worker_cannot_approve` | ✅ |
| 蓝瓷小钥匙：遗忘后旧正文/why 不可搜 | `test_forget_loop.py::test_full_lifecycle` | ✅ |
| 摘要词命中且 matched_by=summary_keyword | 同上 | ✅ |
| get 默认只返回批准摘要表示 | 同上 | ✅ |
| 历史版本明确可读（旧笔迹保留） | 同上 | ✅ |
| 恢复后旧词重新可搜 | 同上 | ✅ |
| 双方任一可批；重复审批 PROPOSAL_ALREADY_RESOLVED 且只产生一个压缩版本 | `test_double_approval_resolved_once` / `test_jiaming_can_also_approve` | ✅ |
| hash 篡改拒绝 | `test_hash_tampering_rejected` | ✅ |
| base_version 移动 STALE | `test_stale_when_base_version_moved` | ✅ |
| pinned/近 30 天/日期未知不入候选 | `test_pinned_not_candidate` 等 3 项 | ✅ |
| FTS 注入惰性（'" OR 1=1 --' 等） | `test_fts_injection_is_inert` | ✅ |
| 语义未配置显式 degraded | `test_semantic_unavailable_is_explicit` | ✅ |
| 幂等重放/冲突 | `TestIdempotency` 2 项 | ✅ |
| worker 无检索/版本读权限 | `TestWorkspaceIsolation` 2 项 | ✅ |

## 2. 原文 / quotes / handoff（Phase 3 出口）

| 场景 | 测试 | 状态 |
|---|---|---|
| 同源同消息 ID 导入幂等不重复 | `test_import_idempotent` | ✅ |
| 30 条按消息计、时间正序 | `test_recent_30_cap_and_order` | ✅ |
| 原文不进 memory 投影；raw.search 标 source=raw | `test_raw_search_independent_of_memory` | ✅ |
| quotes 仅周家明保留/撤下不物理删/独立检索 | `TestQuotes` 2 项 | ✅ |
| handoff 仅周家明写、72h 过期不删除 | `TestHandoff` 2 项 | ✅ |
| 后台语义校对管线 | — | 🔒 reserved：受控 job 未接真实模型，不伪造 semantic_status 变更 |

## 3. 计划 / 日历 / bootstrap / 时间（Phase 4 出口）

| 场景 | 测试 | 状态 |
|---|---|---|
| 计划乐观锁/记忆链接 | `test_plan_lifecycle_and_links` | ✅ |
| 日历聚合 memory+plan；遗忘桶 preview=forgotten_summary | `test_calendar_aggregates_and_forgotten_preview` | ✅ |
| bootstrap 计划过滤（open 全取/7 日 planned/无日期不算/done 排除） | `test_bootstrap_plans_filter` | ✅ |
| Claude Chat = 30 条原文+三天桶+计划 | `test_claude_chat_profile` | ✅ |
| CC = 无 raw | `test_cc_profile_no_raw` | ✅ |
| entry/profile 不匹配拒绝；worker 冒充拒绝 | `test_profile_mismatch_rejected` | ✅ |
| 三天桶按事件自然日（旧桶晚导入不进近三天） | `test_three_day_bucket_uses_event_date` | ✅ |
| 三时间线分离（访问≠对话；工具不冒充人类） | `TestTimeContext` 2 项 | ✅ |

## 4. 信件与删除（Phase 5a，现场行为优先）

| 场景 | 测试 | 状态 |
|---|---|---|
| 删除申请=审批流非直接删；reason 必填/pending_exists/限额 10/5/withdraw/superseded/mismatch | `test_letters_deletion.py` 13 项 | ✅ 与 main2.5 v2.17.11 源码核验行为一致 |
| 锁信：LOCKED_RESOURCE、列表 metadata-only、过期读时归一、仅作者编辑 | `TestLetters` 5 项 | ✅ |
| 2026-09-07 写锁 / 2027-07-09 解锁日期用例 | `test_timed_lock_and_unlock`（合成正文） | ✅ |
| 旧“测试桶豁免直删”通道 | — | ❌ 不迁移（安全决策，非缺失） |
| 旧 pending/approved 记录重放 | — | 🔒 Phase 5 真实迁移时处理（快照未获准） |

## 5. 平台接入（Phase 6）

| 项 | 状态 |
|---|---|
| MCP 协议层（initialize/tools/call、双 profile、传输名映射、tool error 路径） | ✅ `test_mcp.py` 8 项 + 真实服务验证（39 工具/9 工具） |
| 远程 MCP OAuth / Claude Chat / GPT Chat 真实连接 | ⛔ blocked：无远程端点与凭据，未联调 |
| CC Host（订阅 CLI） | ⛔ blocked: claude_cli_missing（本机未安装 claude CLI）；未做任何 API/`--bare` 替代 |

## 6. 迁移 / 备份（Phase 5 工具层）

| 场景 | 测试 | 状态 |
|---|---|---|
| inventory 只读元数据（正文不进输出） | `test_inventory_metadata_only` | ✅ 对真实生产目录执行过一次（481 文件/1 锁信，正文未读取） |
| dry-run 结构映射；锁信正文 <restricted> 占位 | `test_dry_run_mapping_and_lock_protection` | ✅ |
| verify 检出篡改 | `test_verify_detects_tampering` | ✅ |
| 真实数据防误触（>50 文件拒绝） | `test_real_data_guard` | ✅ |
| backup 一致性 + verify-only；篡改检出 | `TestStorage` 2 项 | ✅ |
| 真实快照 apply/cutover | ⛔ blocked: 未获准（按 §20 边界） |

## 6b. Home/Self/Diary/情绪标签/Snapshot（第 2 轮新增）

| 场景 | 测试 | 状态 |
|---|---|---|
| Home 唯一正本版本化、乐观锁、历史留底 | `test_content.py::TestHome` | ✅ |
| Self 仅周家明写；pending=隔日回看；同日拒/隔日可；retired 不浮现历史可查 | `TestSelf` 2 项 | ✅ |
| Diary 独立检索 source=diary 不反向算记忆命中；covers 区间进日历；仅作者可隐藏 | `TestDiary` 2 项 | ✅ |
| 情绪标签 whose 必填；双方同情绪两项 | `test_whose_required` | ✅ |
| 遗忘桶按情绪仍可查（结构化入口不因压缩消失） | `test_forgotten_bucket_still_findable_by_tag` | ✅ |
| bootstrap SNAPSHOT_STALE（资源变化/未知 snapshot） | `TestBootstrapSnapshot` 2 项 + 真实服务验证 | ✅ |
| bootstrap 入口与 profile 不匹配拒绝（claude_chat 绑定调 cc profile） | 真实服务验证 FORBIDDEN | ✅ |

## 6c. 第 3 轮新增（分页/校对管线/React Web/E2E）

| 场景 | 测试 | 状态 |
|---|---|---|
| bootstrap 分页：75 条三页全量可达不重叠不截断 | `test_bootstrap_paging.py` 4 项 | ✅ |
| 续取时资源变化 SNAPSHOT_STALE | `test_page2_stale_when_resources_change` | ✅ |
| 语义校对：无 provider 挂起零写动作 | `test_no_provider_suspends_without_write` | ✅ |
| 撤下 quote 校对不复活 | `test_withdrawn_never_revived` | ✅ |
| 双步一致 material_conflict 狭窄修正（旧版本留底+审计） | `test_material_conflict_applied` | ✅ |
| 双步不一致 uncertain 挂起；auto_apply=false 转人工 | 2 项 | ✅ |
| 浏览器级完整遗忘闭环（React 版 /app） | `apps/web/e2e` 2 项 | ✅ |
| 外部 provider 只读核验 | `docs/provider_contracts.md` | ✅ 核验（接入待凭据） |

## 6d. 第 4 轮新增（能力 55→86）

| 场景 | 测试 | 状态 |
|---|---|---|
| flag 排除自动候选；update 新版本+重索引；版本列表无正文 | `TestMemoryExtras` 3 项 | ✅ |
| 关联：方向/反向/trace 链；relation 检索途径；custom 标签规则；detach 留历史 | `TestRelations` 4 项 | ✅ |
| raw_pending→bind（不改 Hold）→revoke 留历史；同范围他桶 DEDUPE_NEEDS_REVIEW | `TestRawBinding` 3 项 | ✅ |
| 提醒 CRUD+日历+幂等结算；outbox drain/status；审计查询 | `TestRemindersAndCalendar`/`TestMaintenance` | ✅ |
| media 两步上传/hash 去重/白名单/尺寸校验 | `TestMedia` 3 项 + HTTP 鉴权 | ✅ |
| moments post/comment/react | `TestMoments` | ✅ |
| 批量决议逐项冻结+单项失败隔离；拒绝冷却 scan 跳过 | `TestBatchAndCooldown` | ✅ |
| emotion/listening reserved 契约（默认禁用不伪造） | `TestReservedContracts` | ✅ |
| XSS 按数据返回；media 穿越/鉴权；跨主体幂等隔离 | `TestSecurity` 5 项 | ✅ |
| 4 路并发 decide 恰一生效；崩溃对账修复+幂等 | `TestConcurrency` 2 项 | ✅ |
| 迁移 apply 演练（落库+置顶/锁保留+大报告拒绝） | `TestMigrationApplyDrill` 2 项 | ✅ |

## 6e. 第 5 轮新增（§17.3 必需能力补全 + 勘误）

| 场景 | 测试 | 状态 |
|---|---|---|
| memory.list/by_date/by_tag（namespace/whose 过滤） | `TestListing` | ✅ |
| meaning 追加纳入投影可搜；替换归档留底不再可搜；遗忘桶 meaning 不可搜；仅周家明 | `TestMeanings` 3 项 | ✅ |
| **管理员重建索引后遗忘桶旧词不可搜（§8.5 末条）** | `TestRebuildIndex` | ✅ |
| 任务租约：冲突/仅认领人释放/重领；inspect 授权材料 | `TestTaskLeases` 2 项 | ✅ |
| hold(raw_refs) 同源重复返回已有记录不新建 | `TestHoldDedupe` | ✅ |
| quotes.get/by_memory；diary.read/revise（版本+作者校验） | `TestQuotesDiaryExtras` | ✅ |
| sticker add/list/search（引用已上传媒体，hash 校验） | `TestStickersAndTwoPhaseImport` | ✅ |
| 两阶段导入 prepare/status/complete + raw.read | 同上 | ✅ |
| 事件补齐：emotion.changed / reminder.due / proposal.resolved | `TestEventsBackfill` 2 项 | ✅ |
| Web 媒体库（两步上传）与设置页（只读快照） | E2E 回归保持 2 绿 | ✅ |

> 勘误：第 4 轮曾报告"可做项已清零"，经逐节重查不实；上表为补齐项。

## 7. 未实现（如实清单，非失败）

- Web React/Vite 版（当前为后端直出的功能页，真实 API 驱动）
- 表情发送进聊天流：blocked（依赖 chat/CC）
- 自动唤醒 wakeup.*：not_started（AUTO_WAKEUP_ENABLED=false；提醒结算已可手动/可接 scheduler）
- 情绪系统算法：reserved（`memory.by_emotion` 未实现；情绪标签 whose 字段未启用）
- 语义 embedding provider：blocked（未配置；关键词/日期/标签/关联路径完整可用）
- Web UI：朋友圈/提醒/任务租约/她的话校对工作区等新能力页签（核心六页签+内容三页签+媒体库+设置已就绪）
- Chat/CC 会话流：blocked（依赖 CC 前置）
- Siren/Superposition/扎西德勒 provider：blocked（未做只读契约核验）

## 8. 真实联调 vs 模拟的区分

- 真实：本地 HTTP 服务（127.0.0.1:18780）全能力冒烟（hold/search/calendar/quotes/bootstrap/MCP 双 profile）；migration inventory 对真实生产目录只读执行
- 模拟/合成：全部 70 项测试使用合成数据；无任何真实私人内容进入测试或 git
