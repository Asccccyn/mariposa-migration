# NEXT

> v2.0.1 第一批（P0–P6 后端核心）已交付；2026-09-23 独立审计+修复批
> 已交付（docs/AUDIT_REPORT_v2_20260923.md：14 项真实缺陷修复、
> BLOCKERS B8"表已建"更正、313 测试 + E2E 2 全绿）。
> 2026-09-26 召回运行时 v1.3/v1.4 全量落地（docs/memory_runtime/）；
> 435 测试全绿。从这里续接。

## 召回运行时后续（v1.4 框架内，待授权）

- **评测执行**：构造合成标注评测集跑 A/B（scripts/eval_recall.py，
  入口就绪）；真实私人样本不入 fixtures/Git（EVALUATION.md）。
- **dense 通道启用**：模型缓存已在仓库根（bge-small-zh 91MB，
  字面 D:\mariposa 目录待迁移 derived/）；启用=生产授权项。
- **Jev 授权链**：外发数据范围→key→合成样本→C 组对照；JEV-07 的
  provider 级迟到安装真测随此补。
- **estómago 换窗 live**：宿主工程实施后接 SESSION-03 后半。
- **Mac test-guard**：常驻资源保险丝（当前以 AGENTS.md 前台约束替代）。
- **主体级 session 配额**（v1.4 §9.3 后半，未做）。

## 立即可做（v2 剩余，按优先级）

1. **P5 原文 v2**（RAW-01..13）：**从零开始**（2026-09-23 审计更正：
   raw_binding_ranges/raw_context_grants 两表不存在于任何迁移，B8 原声明
   不实）。需：新迁移建表（code point 偏移、source_version/hash）、
   raw.context.preview/expand 确认展开（范围/估算/票据）、
   raw.matching 评测集骨架（threshold=null、awaiting_evaluation、
   不自动生效）；同时收口两处自报置信直绑面（binding.py 默认 'high'、
   memory.hold raw_refs 硬编码 'exact' 且按范围合并旧桶——与 RAW-05
   一源多桶方向相反）。
2. **P5 语义 v2 投影**：BGE 通道改读 v2 白名单投影（event_text/
   summary_body+hash），旧向量不回灌（SEARCH-15）；影子索引+原子切换。
3. **P7 迁移映射**：v1 库→v2 分层字段的逐条映射表（legacy 字段保留、
   日期缺口标 date_gap、author/时间不补造）；合成库 dry-run 全流程。
   注意：migration.py 目前不写 date_gap retention 行（行为安全但
   文档承诺未兑现），映射时一并落。
4. **P6 前端 v2 页面**：分栏桶详情（当时字段 vs 回忆）、召回筛选页
   （分类/心情/日期/关键词）、回忆输入（凭确认）、审查台（原/新摘要+
   tags diff+线索）、原文选区/展开确认、I 编辑、计划固定到期展示；
   手机 390px + 刷新深链 E2E。
5. **验收矩阵机器化**：109 条（docs/spec_v2/mariposa_v2.0.1_需求与验收
   矩阵.json）逐条映射 pytest nodeid + 结果导出脚本（不手写 PASS 常量）。
6. **v2 契约导出**：contracts/capabilities.v2.json / core_input_schemas.v2
   （V2_INPUT_SCHEMAS 同源导出）+ /api/v2 路由版本协商。
7. **MCP 显式 manifest**：正向映射目前是机械下划线替换（202 名实测无
   碰撞、反解查表）；按规格冻结 manifest 文件。
8. **OPS-02 渐进补齐**：173 项无 schema 能力（~110 项真实读写）按风险
   排序补 schema；已修校验器深度（嵌套 required/minItems/minProperties/
   数值边界，2026-09-23）。
9. **审查通知/待办入口**：队列非空时的已授权通知通道（未配置时诚实
   显示待审数，不假报送达）。
10. **存量超长函数重构**（651d647 后遗留）：`workspace/service.py::
    decide`（96行/嵌套5）、`submit`（88行）、`memory/service.py::
    apply_forget_approval`（95行）、`restore`（75行）。行为敏感且测试
    密集，先补行为测试画像再拆分验证。

## blocked（解锁条件明确）

- **CC Host**：需先安装官方 Claude Code CLI 并核验订阅登录（`CC_AUTH_MODE=subscription_cli`）；本机当前无 claude 命令。不装不用 API 替代。
- **远程 MCP OAuth / Claude Chat / GPT Chat 连接**：需用户提供远程端点/客户端注册；协议层已备。
- **语义 embedding provider**：需配置 MARIPOSA_SEMANTIC_PROVIDER；未配置时保持显式 degraded。
- **真实迁移 apply / 生产切换**：需乔生单独授权真实快照（§20.4 四隔离区流程）。
- **Siren / Superposition / 扎西德勒**：先做只读契约核验（需确认各服务当前 API 清单）再接。
- **自动绑定阈值**：需先完成标注集评测（V2-RAW-11..13）；threshold=null、
  awaiting_evaluation 期间只建议+人工确认。

## 待用户确认的 v2 口径（不阻塞开发，已按保守方向实现）

- 纯 plan 分类/未分类桶的期限（当前：不自动压缩，D16）
- 共同话语"接住/回应"的判定精度（当前：字面同一句，D18）


> v1.1 对照后：授权内可做项再次清零。剩余全部在 docs/BLOCKERS.md（7 项，均待外部条件）。

> 从这里续接。按优先级排列；blocked 项写明解锁条件。

## 立即可做（依赖满足）

1. ~~Web React/Vite 版~~ ✅ 第 3 轮完成（apps/web，TS 严格模式 + Playwright E2E）；**2026-10-05 退役整体删除**——认证停留在旧"粘贴 token"模式未跟上 OAuth 密码登录，招牌功能（遗忘闭环/日历）亦属已退役语义；现行唯一前端=backend/mariposa/web/index.html（配套 OAuth 登录）
2. **Diary/Self/Home 实体**（§10.3-10.5）：schema v4 + diary.write/read/search、self.write/review（隔日规则）、home.get/update；日历 provider 注册 diary。
3. ~~memory.by_emotion / 标签 whose~~ ✅ 第 2 轮完成
4. ~~bootstrap SNAPSHOT_STALE~~ ✅ 第 2 轮完成（分页 cursor 仍待做）
5. **workspace.forgetting.scan 定时器占位**：FORGET_SCHEDULE_ENABLED=false 的调度骨架 + 手动触发已有。
6. **quotes 语义校对受控管线骨架**（§10.1）：双步判定+修正校验，provider 未配置时只挂起不写——reserved 转可测。
7. ~~E2E 测试~~ ✅ 第 3 轮完成（apps/web/e2e，2 项：完整遗忘闭环 + 日历）；**2026-10-05 随 apps/web 退役一并移除**（默认 HTML 暂无自动化 UI 测试，后续如需另行立项）

## blocked（解锁条件明确）

- **CC Host**：需先安装官方 Claude Code CLI 并核验订阅登录（`CC_AUTH_MODE=subscription_cli`）；本机当前无 claude 命令。不装不用 API 替代。
- **远程 MCP OAuth / Claude Chat / GPT Chat 连接**：需用户提供远程端点/客户端注册；协议层已备。
- **语义 embedding provider**：需配置 MARIPOSA_SEMANTIC_PROVIDER；未配置时保持显式 degraded。
- **真实迁移 apply / 生产切换**：需乔生单独授权真实快照（§20.4 四隔离区流程）。
- **Siren / Superposition / 扎西德勒**：先做只读契约核验（需确认各服务当前 API 清单）再接。

## 遗留核验（Phase 0 待核验清单）

- 旧锁状态机全集与 MCP/AI 删除语义（letters.py 注释指出的与 web/human 差异）
- `import_memory.py` 幂等细节（迁移 apply 时需要）
- 旧 self/dream 文件格式（迁移映射用）
