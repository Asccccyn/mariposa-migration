# WP06 · 预算真实计数与输出预算（recall-closure-20260930）

规则依据：S16（配额/批次/输出预算）/ S17（成功轮预算）。

## 变更

1. **S17 burst 真实计数**：`rounds_left_in_burst` 与最终事务终检
   均改为 `COUNT(recall_rounds WHERE burst_no=?)` 真实记录计数，
   废除"总数-(burst-1)×固定值"推断——burst1 未填满时提前开启的
   burst2 不被虚增已用（测试钉住）。
2. **S16 输出预算实际计算**（`_enforce_output_budget`）：完整
   packet UTF-8 序列化 ≤24576 字节、候选正文合计 ≤4000 字符、
   卡数 ≤3、单候选文本 ≤600 code points——逐层实际计算与裁剪
   （命中窗截断→正文总量收缩→JSON 字节收敛），超限显式
   budget_flags/truncated，packet.budget.output_limits 如实声明。
3. **burst=2 验收**：`test_burst_two_acceptance` 按 00 指令以
   原 v1.7 每 burst 2 轮验收（monkeypatch，隔离会话）；**config
   默认值未动**——生产 3→2 切换与旧会话处理按指令单独呈报，
   待用户批准。

## 遗留到后续 WP

- S16 全局 monotonic deadline（单请求/退避占截止）→ 与 WP04 的
  Round2 模型调用一起落；
- S19 覆盖分页（2000 池截断 partial+游标）→ WP07 对照时统一。

## 测试

`tests/unit/test_closure_wp06.py` 4 个：提前 burst 不被挤占、
burst=2 验收、JSON 字节实际生效、单候选 600 窗。全量：unit 503 +
acceptance/integration 126 = **629 passed / 1 skipped / 0 failed**。
