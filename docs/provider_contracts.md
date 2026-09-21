# 外部 Provider 只读契约核验（Phase 7 前置）

> 2026-09-21 · 全程只读（GET 健康检查 + 源码路由清单）；未调用任何写入接口、
> 未改任何服务配置/端口/数据。

## Superposition（许愿星）· ✅ 运行中，契约已核验

- 目录 `D:\superposition`（Python，`app/` + `mcp_server.py`）
- 默认端口 `127.0.0.1:8321`（`SUPERPOSITION_PORT`）；**当前监听中**
- 健康检查：`GET /api/health` → `{"status":"ok","db_ok":true,"version":"1.3.0","test_instance":false}`【实测】
- API 路由（源码 `app/api/routes.py` 只读清单）：`/health`、`/auth/login|me|logout`、
  `/stars`（写）、`/stars/hidden/mine[/{id}]`、`/stars/shared[/{id}]`、`/requests`（写/读）等
- 接入状态：**blocked: 未做鉴权对接**——需要为 mariposa 建独立服务凭据（§3.3 不透传用户 token）；
  契约面已可开始写适配 provider

## Siren（语音/实时通话）· ✅ 运行中，契约已核验

- 目录 `D:\siren`（Node monorepo：`apps/server` + `apps/playground`）
- 端口 `127.0.0.1:8790`；**当前监听中**
- 健康检查：`GET /health` → `{"status":"ok","service":"siren","version":"1.1.1",…}`【实测】
- **重要事实**：health 显示 `providers: asr=mock(dev-fallback), asyncTts=mock(dev-fallback),
  realtimeTts=mock(dev-fallback), coreBridge=mock` —— Siren 自身的语音供应商当前是开发回退，
  不是真实接通。mariposa 接入时不得把经 Siren 的语音能力报告为已上线。
- MCP 端点：`POST /mcp`（Streamable HTTP, stateless JSON）【源码/文档核验】
- 接入状态：**blocked: 同上待独立凭据**；媒体流不经 MCP 转发的约束记入适配设计

## 扎西德勒（群聊/好友）· ⛔ 服务未运行

- 目录 `D:\Zashidele` 存在（Node 24+ ESM，README：默认 `127.0.0.1:8787`，`npm start`）
- 端口 8787 **当前无监听**；未启动它（不属于本任务授权副作用）
- 接入状态：**blocked: service_not_running**——需要时由乔生启动后再核验契约

## 约束（对 mariposa 适配层）

1. 三个 provider 均为**独立服务**：不改其端口/DB/认证/Cloudflare 路由
2. 下游不支持幂等键时，社交写入超时标 `OUTCOME_UNKNOWN` 查实际状态，不盲重试（§16.4）
3. 两个入口（Chat/CC）共享同一调用记录
