# NEXT

> v1.1 对照后：授权内可做项再次清零。剩余全部在 docs/BLOCKERS.md（7 项，均待外部条件）。

> 从这里续接。按优先级排列；blocked 项写明解锁条件。

## 立即可做（依赖满足）

1. ~~Web React/Vite 版~~ ✅ 第 3 轮完成（apps/web，TS 严格模式 + Playwright E2E）
2. **Diary/Self/Home 实体**（§10.3-10.5）：schema v4 + diary.write/read/search、self.write/review（隔日规则）、home.get/update；日历 provider 注册 diary。
3. ~~memory.by_emotion / 标签 whose~~ ✅ 第 2 轮完成
4. ~~bootstrap SNAPSHOT_STALE~~ ✅ 第 2 轮完成（分页 cursor 仍待做）
5. **workspace.forgetting.scan 定时器占位**：FORGET_SCHEDULE_ENABLED=false 的调度骨架 + 手动触发已有。
6. **quotes 语义校对受控管线骨架**（§10.1）：双步判定+修正校验，provider 未配置时只挂起不写——reserved 转可测。
7. ~~E2E 测试~~ ✅ 第 3 轮完成（apps/web/e2e，2 项：完整遗忘闭环 + 日历）

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
