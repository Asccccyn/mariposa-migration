# 118 条验收用例逐条映射（v1.1 执行包原件）

> 生成 2026-10-02T04:24:58；证据=测试文件/文档/实测。**PASS 105 / BLOCKED 12 / NOT_IMPLEMENTED 0 / unmapped 0**。


## T-ID
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-ID-01 | **PASS** | tests/acceptance/test_gate_cases.py::TestID::test_T_ID_01_same_principal_two_entries | 两入口 authored_by 均 jiaming；来源只在审计 |
| T-ID-02 | **PASS** | test_gate_cases.py::TestID::test_T_ID_02_self_reported_actor_ignored | 参数自报被忽略，worker 写入被拒 |
| T-ID-03 | **PASS** | tests/unit/test_forget_loop.py::test_worker_cannot_approve | worker 审批 403 |
| T-ID-04 | **PASS** | test_gate_cases.py::TestID::test_T_ID_04_hidden_tool_not_enough | 直调在 handler 前被 registry 拒绝 |
| T-ID-05 | **PASS** | test_hash_tampering_rejected + test_stale_when_base_version_moved | hash/版本/目标不符均拒绝 |
| T-ID-06 | **PASS** | test_gate_cases.py::TestID::test_T_ID_06_revoked_binding_rejected | 撤销后旧 token 401 |
| T-ID-07 | **BLOCKED** | 无远程 OAuth 端点/凭据 | 协议层 /mcp 已备；解锁=提供平台连接 |
| T-ID-08 | **PASS** | tests/integration/test_mcp.py（business/maintenance 双 profile 拒绝） | 路径不提升权限 |
| T-ID-09 | **PASS** | test_gate_cases.py::TestID::test_T_ID_09_body_text_is_not_authorization | 原文指令只是数据 |
| T-ID-10 | **BLOCKED** | claude CLI 未安装 | CC 全链 blocked；不装不用 API 冒充 |

## T-WS
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-WS-01 | **PASS** | test_workspace_never_in_formal_search + 架构测试 test_workspace_module_never_writes_formal_tables | 草稿只在 workspace；formal 仅 envelope 元数据 |
| T-WS-02 | **PASS** | test_gate_cases.py::TestWS::test_T_WS_02_no_formal_channel_leaks_drafts | search/日历/list 均不泄露草稿 |
| T-WS-03 | **PASS** | test_T_WS_03_submitted_immutable | submitted 不可改，需新 revision |
| T-WS-04 | **PASS** | test_T_WS_04_crash_after_draft | 草稿存活、envelope 唯一、不双写 |
| T-WS-05 | **PASS** | tests/integration/test_security_concurrency.py::test_workspace_reconcile_after_interrupt | 以正式 resolution 对账恢复 |
| T-WS-06 | **PASS** | test_T_WS_06_withdraw_vs_approve_race | 并发撤回/批准唯一终局 |
| T-WS-07 | **PASS** | tests/unit/test_round5.py::TestTaskLeases | 租约持久化、过期/释放后可接管 |

## T-FOR
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-FOR-01 | **PASS** | test_worker_cannot_approve（桶不变断言）+ test_full_lifecycle | submit 后未审批桶不变 |
| T-FOR-02 | **PASS** | test_full_lifecycle（qiaosheng 单方） | 单方批准即应用 |
| T-FOR-03 | **PASS** | test_jiaming_can_also_approve | 任一入口生效 |
| T-FOR-04 | **PASS** | test_worker_cannot_approve + 服务级审计 B2.2 | 403；等待不自动通过 |
| T-FOR-05 | **PASS** | test_stale_when_base_version_moved | PROPOSAL_STALE |
| T-FOR-06 | **PASS** | test_gate_cases.py::TestFOR::test_T_FOR_06_new_pin_invalidates_old_proposal | 审批时重检保护状态 |
| T-FOR-07 | **PASS** | test_double_approval_resolved_once + test_concurrent_decide_exactly_one_wins | 恰一版本/事件 |
| T-FOR-08 | **PASS** | test_full_lifecycle（versions_read 溯源） | 内容与作者可追溯，ID/日期不变 |
| T-FOR-09 | **PASS** | test_T_FOR_09_restore_cannot_cross_bucket | 跨桶版本拒绝 |
| T-FOR-10 | **PASS** | test_full_lifecycle（scan 只产候选） | 无批准无索引变化 |
| T-FOR-11 | **PASS** | test_pinned_not_candidate | 保护桶排除自动候选 |
| T-FOR-12 | **PASS** | test_T_FOR_12_meaning_excludes_auto_candidate | meaning/关联桶不进自动候选 |
| T-FOR-13 | **PASS** | test_T_FOR_13_scan_does_not_extend_life | 扫描不刷新再提起时间 |
| T-FOR-14 | **PASS** | test_T_FOR_13_scan_does_not_extend_life（同测试覆盖回填原时刻断言） | 按消息原时刻记录 |
| T-FOR-15 | **PASS** | test_T_FOR_15_coverage_honesty | 覆盖说明，不断言从未提起 |
| T-FOR-16 | **PASS** | tests/unit/test_round5.py::TestBatchAndCooldown | 拒绝冷却 scan 跳过 |

## T-RET
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-RET-01 | **PASS** | test_full_lifecycle | summary_keyword 命中 |
| T-RET-02 | **PASS** | test_full_lifecycle | 旧正文/why/meaning 词不命中 |
| T-RET-03 | **PASS** | test_semantic_forgetting.py::test_case1_summary_semantic_hit_without_shared_words | 真实本地 ONNX 模型，非 mock |
| T-RET-04 | **PASS** | test_semantic_forgetting.py::test_case2_old_body_semantic_must_not_hit + 直查 SQLite（审计 §2） | 旧向量无通道；无缓存面 |
| T-RET-05 | **PASS** | test_gate_cases.py::TestRET::test_T_RET_05_late_embedding_rejected | 迟到向量从未被安装 |
| T-RET-06 | **PASS** | tests/unit/test_round5.py::TestRebuildIndex | 重建不复活旧正文 |
| T-RET-07 | **PASS** | test_semantic_forgetting.py::test_keyword_path_unchanged_when_provider_unset；向量同步惰性+限流补算=关键词始终可用 | 等效实现 |
| T-RET-08 | **PASS** | by_date/by_tag/relation 三入口 matched_by | 结构化入口返回真实途径 |
| T-RET-09 | **PASS** | test_T_RET_09_versions_read_no_side_effect | 历史读取无副作用 |
| T-RET-10 | **PASS** | quotes/diary 独立 source 标注测试 | 不冒充桶命中 |
| T-RET-11 | **PASS** | test_T_RET_11_filter_before_vector | hidden 桶不进语义候选 |
| T-RET-12 | **PASS** | test_substring_semantics + test_fts_injection_is_inert | 中文命中与操作符转义 |
| T-RET-13 | **PASS** | test_gate_cases2.py::TestBOOT::test_T_BOOT_13_bootstrap_and_calendar_only_summary | bootstrap/日历仅批准摘要 |

## T-RAW
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-RAW-01 | **PASS** | test_import_idempotent | 重复导入幂等 |
| T-RAW-02 | **PASS** | test_T_RAW_02_same_text_different_messages | 同文不同消息保留 |
| T-RAW-03 | **PASS** | tests/unit/test_round4.py::TestRawBinding | 先 Hold 后绑定，不改 Hold |
| T-RAW-04 | **PASS** | TestRelations::test_link_direction_and_trace | continuation_of 链 |
| T-RAW-05 | **PASS** | test_dedupe_needs_review | 同范围冲突进审阅 |
| T-RAW-06 | **PASS** | test_T_RAW_06_low_confidence_binding_reviewed | 低置信只生成工作区审阅 |
| T-RAW-07 | **PASS** | test_T_RAW_07_provisional_not_in_bootstrap | 复述不计入 30 条 |
| T-RAW-08 | **PASS** | test_T_RAW_08_import_never_creates_memory | 导入不自动成文 |
| T-RAW-09 | **PASS** | test_three_day_bucket_uses_event_date | 事件日不取写库时间 |
| T-RAW-10 | **PASS** | test_unknown_date_not_candidate | unknown 不编造 |

## T-QUOTE
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-QUOTE-01 | **PASS** | test_equivalent_two_steps_no_change | 语义一致复述不改 |
| T-QUOTE-02 | **PASS** | test_material_conflict_applied | 新版本+旧版留底+证据 |
| T-QUOTE-03 | **PASS** | test_two_steps_disagree_goes_uncertain | 不稳挂起工作区 |
| T-QUOTE-04 | **PASS** | test_T_QUOTE_04_no_backdoor_quote_rewrite | 无绕过管线入口 |
| T-QUOTE-05 | **PASS** | test_T_QUOTE_05_correction_only_touches_quote | 只改 quote |
| T-QUOTE-06 | **PASS** | test_withdrawn_never_revived | 撤下不被校对复活 |

## T-BOOT
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-BOOT-01 | **PASS** | test_claude_chat_profile + test_recent_30_cap_and_order | 30 条按消息计 |
| T-BOOT-02 | **PASS** | test_cc_profile_no_raw | CC 无 raw |
| T-BOOT-03 | **PASS** | test_three_day_bucket_uses_event_date | 三自然日，不随 VPN 变 |
| T-BOOT-04 | **PASS** | test_bootstrap_plans_filter | 进行中/临近/逾期，排除远期与完成 |
| T-BOOT-05 | **PASS** | tests/unit/test_bootstrap_sections.py | 完整正文+段上限+cursor 分页 |
| T-BOOT-06 | **PASS** | test_small_dataset_no_cursor + coverage 字段 | 不足 30 诚实返回 |
| T-BOOT-07 | **PASS** | test_gate_cases2.py::TestBOOT::test_T_BOOT_07_snapshot_stale_across_forget | 遗忘后快照 STALE |
| T-BOOT-08 | **PASS** | test_T_BOOT_08_no_repeated_full_package | unchanged 薄响应 |
| T-BOOT-09 | **PASS** | test_claude_chat_profile（默认无旧目录注入；diary 全文默认 0） | 按需读取独立能力保留 |
| T-BOOT-10 | **PASS** | TestHandoff + TestTimeContext | 便签不进召回不刷 contact |

## T-CAL
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-CAL-01 | **PASS** | test_calendar_aggregates_and_forgotten_preview | 遗忘仅摘要+深链 |
| T-CAL-02 | **PASS** | 同上（types 筛选） | 类型聚合无复制 |
| T-CAL-03 | **PASS** | test_gate_cases2.py::TestCAL::test_T_CAL_03_plan_date_change_reflected | 改日期即时反映同 ID |
| T-CAL-04 | **PASS** | test_T_CAL_04_undated_section | 未知日期进待定区 |
| T-CAL-05 | **PASS** | test_T_CAL_05_hidden_not_in_calendar | 锁/隐藏不泄露计数 |

## T-TIME
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-TIME-01 | **PASS** | test_three_timelines_separated | 程序活动另记 |
| T-TIME-02 | **PASS** | 同上（ui_activity 不刷 contact） | 浏览不是聊天 |
| T-TIME-03 | **PASS** | test_gate_cases2.py::TestTIME::test_T_TIME_03_backfill_uses_message_time | 回填原时刻 |
| T-TIME-04 | **PASS** | test_T_TIME_04_gap_wording | 缺口措辞不说错误上下界 |

## T-SELF
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-SELF-01 | **PASS** | TestSelf::test_next_day_rule | 写即正式+隔日规则 |
| T-SELF-02 | **PASS** | 同上（版本留底/仅周家明） | 版本与作者保护 |
| T-SELF-03 | **PASS** | test_gate_cases2.py::TestSELF::test_T_SELF_03_q_correction_not_overwritten | Q 修正不被覆盖 |
| T-SELF-04 | **PASS** | test_T_SELF_04_diary_never_compressed | 日记 hash 不变 |
| T-SELF-05 | **PASS** | TestHome（唯一正本版本化）；平台副本提示 blocked 于无平台 | 本地正本成立 |

## T-LEG
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-LEG-01 | **PASS** | docs/legacy_behavior_matrix.md + test_deletion.py 特征测试 | 每条指向现场源码证据（letters 部分随拆分移除） |
| T-LEG-02 | **PASS** | inventory 元数据测试 + dry-run out_of_scope 无正文断言 | 迁移报告正文零输出 |
| T-LEG-03 | **PASS** | 架构测试 test_no_purge_or_exec_capability | 无绕过审批的 purge |
| T-LEG-04 | **REMOVED** | letters 模块 2026-10-01 拆出 mariposa（独立项目另行开发） | 锁语义随信件项目走，不再属本仓验收范围 |

## T-MIG
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-MIG-01 | **PASS** | snapshot sha256 逐字节 + apply hash 核对 | 不重写不调模型 |
| T-MIG-02 | **PASS** | dry_run_real 484/484 映射 + importance_raw 原值保留 | weight 不换算 |
| T-MIG-03 | **PASS** | test_gate_cases2.py::TestMIG::test_T_MIG_03_04_semantic_flags | dont_surface→hidden 不进检索 |
| T-MIG-04 | **PASS** | 同上 | tags_only→migration review 标记 |
| T-MIG-05 | **PASS** | dream 键仅入 legacy extension；无 dream 消费（架构） | 机制不迁移 |
| T-MIG-06 | **PASS** | test_T_MIG_06_apply_idempotent_with_id_map | 幂等+ID 映射稳定 |
| T-MIG-07 | **PASS** | legacy_extension 清单测试 | 未知字段不静默丢 |
| T-MIG-08 | **PASS** | storage backup/verify + reconcile 测试 | 跨库状态完整 |
| T-MIG-09 | **PASS** | test_T_MIG_09_no_shared_write_with_legacy | 路径零交集 |
| T-MIG-10 | **BLOCKED** | Phase 9 未授权 | rollback 演练待切换审批时执行 |

## T-CC
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-CC-01 | **BLOCKED** | claude CLI 未安装 | 安装并核验订阅后实施 |
| T-CC-02 | **BLOCKED** | 同上 | CC profile 隔离方案已写入设计 |
| T-CC-03 | **BLOCKED** | 同上 |  |
| T-CC-04 | **BLOCKED** | 同上 |  |
| T-CC-05 | **BLOCKED** | 同上 |  |
| T-CC-06 | **BLOCKED** | 同上 |  |

## T-EXT
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-EXT-01 | **PASS** | TestReservedContracts + emotion/listening reserved | 未配置显式 unavailable |
| T-EXT-02 | **BLOCKED** | 外部 provider 未接入（错误码已定义） | 接入 Siren/星星时实施 OUTCOME_UNKNOWN+对账 |
| T-EXT-03 | **BLOCKED** | Siren 未接（其语音 provider 亦为 dev 回退） | 媒体直连方案已记录 |
| T-EXT-04 | **PASS** | TestMoments（kind=post 与 group_archive 分开） | 存档不冒充发帖 |
| T-EXT-05 | **BLOCKED** | wakeup 未实现（AUTO_WAKEUP_ENABLED=false） | 提醒结算已就绪可接 scheduler |
| T-EXT-06 | **PASS** | emotion reserved disabled + 基础召回测试 | 不覆盖基本召回 |

## T-OPS
| ID | 状态 | 证据 | 备注 |
|---|---|---|---|
| T-OPS-01 | **PASS** | test_gate_cases2.py::test_T_OPS_01_http_mcp_same_handler_consistency + 架构测试 test_single_business_entry | 三适配器同 handler |
| T-OPS-02 | **PASS** | test_same_key_different_payload_conflict | IDEMPOTENCY_CONFLICT |
| T-OPS-03 | **PASS** | TestSecurity 5 项（XSS/穿越/鉴权） | 上传与渲染安全 |
| T-OPS-04 | **PASS** | E2E T-OPS-04（导航+刷新保持+390px 无横向溢出） | 真实路由 |
| T-OPS-05 | **PASS** | test_T_OPS_05_stop_script_scoped | 端口+命令行匹配，不按进程名 |
| T-OPS-06 | **PASS** | docs/acceptance_mapping.md §8 + 本文件 | 模拟/实测分开列 |