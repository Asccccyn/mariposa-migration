# WP04 · Round2 运行库事务化（recall-closure-20260930 S13/S14）

## 变更

1. **runtime migration 6**：`recall_round1_receipts`（Round1 成功
   回执：session/revision/plan_hash/scope_hash/policy/round_kind/
   methods/coverage/candidate_set_hash/judged-unavailable-unjudged
   统计/delivery_action，PK(session_id, revision)）；
   `recall_rounds.kind`（memory/words/raw 逻辑类别，S17）。
   旧 attempts 的 completed 不再被当作 Jev 正常证据。
2. **回执写入**：start/refine 的最终事务在提交 round/candidates
   的同时写 Round1 回执（judge 统计来自本 judge_result——
   evaluated/unavailable/未入判数）。
3. **round2 重写**（`recall/service.round2`，registry 走 operation
   包装）：
   - 服务端已存 plan（不接受可更换的 Round2 query_plan，S13-4）；
   - **六条件门禁全服务端事实**：Round1 回执在场且
     unavailable=0、unjudged=0（或明确合法空输入——candidate_set
     为空且 judge 配置可用）、coverage.judge 无故障；reason ∈
     冻结闭集**且有回执事实支持**（NO_DELIVERABLE_CANDIDATE ←
     delivery=no_candidates；EVIDENCE_INSUFFICIENT/
     VERBATIM_REQUIRED_NOT_MET ← needs_validation；
     EXPLICIT_REJECT_AFTER_DELIVERY ← session 存在 rejected）；
     raw 搜索授权 + **当前 Jev 供应商外发授权**（TypeSafe data
     profile 含 source_excerpt——S15：无许可 raw 不开始，返回
     结构化 gate 拒绝）；预算真实计数；同 burst 无既有 raw 轮
     （换 operation_id 不无界重跑）；
   - raw 候选（source 层 published=1，human/assistant）过**同一层
     Jev + selection 硬门**（S10/S14：新候选必须过唯一出口），
     不再直出 hits；
   - commit-at-end：计算（深搜+judge）在事务外，最终单事务提交
     operation + kind='raw' round + candidates + attempt；
   - packet 走 `_enforce_output_budget`（≤3/预算裁剪）。
4. 旧 `round2_server_facts`/`round2_gate`（attempts status 口径）
   保留为诊断路径，正式入口不再使用。

## 测试

`tests/unit/test_closure_wp04_round2.py` 7 个（全部真实链路，无
手工传 complete）：主链交付（Round1→round2 raw 候选经 judge ≤3、
rounds 含 memory+raw、回执 judged>0）；judge 故障阻断；reason 闭集
+事实支持双向；无 source_excerpt 许可 raw 不开始；同 operation 重放
单 raw 轮；换 operation_id 防重跑；无回执拒绝。

全量：unit 513 + acceptance/integration 126 =
**639 passed / 1 skipped / 0 failed**。

## 遗留（WP05/WP07 或后续）

- raw 候选的 content_hash/offsets 精装（source_msg 引用 + excerpt
  hash 现值）→ WP05 与 words envelope 一并强化；
- 旧库无回执的存量 session：按 S13"不可证明则不可升级"自然拒绝
  （要求新合法搜索），未做迁移回填（不伪造已判事实）；
- round2 的 S16 全局 deadline（模型调用截止）→ WP07 一并。
