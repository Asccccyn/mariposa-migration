# Remediation · Codex 78921d0 复审（11 条）

基线：78921d0（22 条修复批）→ 修复 commit 见 git log。全部 11 条
处置如下（8×P2 + 1×P2 复发 + 1×P2 复发 + 1×P3）：

| 条 | 处置 |
| - | - |
| RER-01 | 接续引用改为在记录 operation 结果**之前**铸造并入 packet（start/refine 事务内传入 _commit_round_effects 签发）——首次响应与同 operation 重放携带同一个仍有效 ref，不新发不丢 |
| RECALL-06(复发) | words.list（list_for）独立报告来源缺口：dangling 标 source_gap（legacy_raw_prefix/invalid_or_missing），与检索/直读同口径 |
| RER-02 | navigate 重放不套 event phase 字段过滤——时间轴字段（event_time/hold_time）结构卡仍有效时照常重放（身份/版本/可见性检查保留） |
| RE-MEM-01 | plan.memory.correct 入崩溃恢复面（_DOMAIN_IDEMPOTENT_CAPS + payload builder） |
| RE-MEM-02 | 删除申请/决定恢复按 request_id 重建 deletion_get 正常结果，不回放裸指针 |
| RE-MEM-03 | 恢复 payload 归一化与领域同口径：request reason strip、decide decision strip().lower() |
| SRC-01-R1 | validate_range 去掉首见 sequence 序预检——顺序以真实 parent 链 walk 为准 |
| SRC-01-R2 | 补齐的 path 成员计入 SOURCE_RANGE_MAX_MESSAGES 数量预算 |
| SRC-03-R1 | verify 解析归档 manifest 并核对身份（payload 文件名 + sha256 一致）；损坏/断链/retarget 均判不可恢复 |
| SRC-05(复发) | verify 引用扫描排除空字符串 raw_path |
| SRC-06(P3) | 迁移 apply 侧三处条目补 "path"（相对路径），verify 消费端一致 |

验证：909 收集 908 passed + 1 skipped 0 failed（分批前台/隔离根）；
新增回归 4 例（重放保留接续引用/导航重放/删除恢复真实结果/列表 gap）。
