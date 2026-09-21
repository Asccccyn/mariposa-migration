# 平台前置核验

| 前置 | 状态 | 证据 | 影响 |
|---|---|---|---|
| Python 3.12.10 + venv | ✅ 已核验 | `.venv` + requirements.lock | — |
| SQLite 3.49.1 + FTS5 | ✅ 已核验 | doctor 冒烟 | — |
| 端口 18780 / 5178 | ✅ 空闲 | netstat | — |
| 服务真实启动 | ✅ 已核验 | `/health` 200；token 冒烟 hold+search | — |
| `claude` CLI | ❌ 未安装 | `Get-Command claude` 空 | CC Host 全部 `blocked: claude_cli_missing`；不装 CLI 不做任何订阅/API 冒充 |
| Claude Chat 远程 MCP | ⛔ 未联调 | 无远程 URL/凭据 | Phase 6 blocked：待提供连接配置 |
| GPT Chat maintenance MCP | ⛔ 未联调 | 同上 | Phase 6 blocked |
| Siren / Superposition / 扎西德勒 | ⛔ 未接 | 未做只读契约核验 | Phase 7 blocked：provider 接口预留，不伪造状态 |
| 语义 embedding provider | ⛔ 未配置 | `MARIPOSA_SEMANTIC_PROVIDER` 空 | 检索显式 `degraded: semantic_unavailable` |

blocked 项不阻塞：本地可验证的领域闭环（Phase 1-5 合成数据）全部继续。
