# r2 S09 · Jev 输入多角色证据模型（recall-closure-20260930-r2）

依据：r2 版 01_CURRENT_SEMANTICS S09（定位线索 ≠ 事件事实）、
02/WP02、03 定向反例、contracts/jev_payload_title_event.synthetic.json。

## 变更

1. **CandidateEnvelope v2 投影**（`typesafe_jev._candidate_projection`）：
   候选输入从单一片段改为 `segments[]` 多角色段——
   - `match_evidence`：真实命中片段（标题命中给标题、话语命中给
     话语、event/dense 命中给正文命中窗）；
   - `event_evidence`：普通 memory 候选的当前 event_text（事实
     主体，标题不能替代）；event 自身命中时**同段双标
     match+event 不复制**（测试钉住）；
   - `primary_evidence`：raw/words 的目标本体段；
   - 结构上下文（event_date/memory_id/speaker）入 metadata，
     不伪装正文段。
2. **title_cue 语义**：标题参与命中即给 `title_cue/match_evidence`
   段（无论 event 是否同时命中）；CORE 候选的 matched_fields 不含
   original_title（检索层阶段过滤）→ 天然不夹带；非命中字段不附段。
3. **prompt 角色契约**：state 携带 `candidate_role_contract`；
   question 的 task/criteria 按 r2 样例（"结合 match_evidence 和
   event_evidence；title_cue 只帮助定位，不单独证明事件"；
   true="定位线索与事件事实共同支持…"，false="仅标题/词面碰巧
   相同，事件事实不支持或与请求冲突"）。
4. **S15 逐段许可**：`_SEGMENT_GRANT` 按段字段映射外发许可
   （original_title→title_cue、event_text→event_excerpt、
   our_words→word_excerpt、raw→source_excerpt）；无许可段不外发。
5. **同段去重合并 roles**（不复制文本浪费预算）。
6. **数据流**：pipeline meta 补 original_title；候选 `_row` 保留到
   judge（`_attach_event_evidence` 不再 pop），出站前由
   `_finalize_cards` 白名单剔除——内部数据不进最终 packet。
7. **fingerprint**：candidate_projection 含完整 segments → 缓存
   身份自动覆盖实际投影（r2 要求 ✓）。

## 测试（03 点名的成对反例）

`tests/unit/test_closure_r2_roles.py` 3 个：
- **中秋+约会成对反例**：同标题（中秋）、不同正文（约会/加班）
  的两候选——Jev 输入同含 title cue 与 event 事实，正文差异可见
  （验证 payload 角色而非字符串比较）；
- event 命中同段双标不复制；
- CORE 不夹带 title/words 段。

同步更新旧语义断言 4 处（hybrid05 单测/验收、jev09、wp02 profile
组）到投影级/segments 级检查。

## 全量

unit 506 + acceptance/integration 126 = **632 passed / 1 skipped /
0 failed**。
