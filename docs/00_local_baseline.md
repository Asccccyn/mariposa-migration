# Phase 0 · 本地基线（只读盘点）

> 生成：2026-09-21 · 方式：只读元数据 + 源代码阅读；**未读取任何私人记忆/信件正文**
> 结论级别：【现场核验】= 本机证据；【待核验】= 未完成核验

## 1. 真实生产定位

| 项 | 结果 |
|---|---|
| 生产容器 | `ombre-brain`（docker，镜像 `deploy-ombre-brain`），Up since 2026-09-14 |
| 端口映射 | `127.0.0.1:18001 -> 8000/tcp`；宿主另有 `127.0.0.1:8000` 监听（PID 15112） |
| 挂载 | `D:\Ombre-Brain-main2.5\buckets-data -> /app/buckets` |
| compose project | `deploy`，config_files 位于 `D:\Ombre-Brain-main2.5\deploy\` |

**结论：`D:\Ombre-Brain-main2.5`（VERSION 2.17.11）是真实运行源**；`D:\Ombre-Brain-dev`（VERSION 2.17.5，git 最新 `db758bd`，工作区干净）是开发副本。与 01 文档"不默认 main2.5 或 dev 必然是运行源"的要求相符，本次以容器挂载证据判定。

## 2. 目录状态

| 目录 | VERSION | git | 未提交修改 | 数据卷 |
|---|---|---|---|---|
| `D:\Ombre-Brain-main2.5` | 2.17.11 | 非 git 仓库 | — | `buckets-data/`（生产，含 `archive/`、`_ledger/`、`_media/`、`_sources/`、`embeddings.db`、`dehydration_cache.db`、`config.yaml`） |
| `D:\Ombre-Brain-dev` | 2.17.5 | `db758bd` | 0 | `buckets/`（结构同，未挂载进容器） |

私人数据仅做目录/文件名级统计，内容未读取、未复制。

## 3. 技术栈（旧）

- 后端：Python（src/：`server_app.py`、`bucket_manager.py`、`deletion_requests.py`、`embedding_engine.py`、`bm25_index.py`、`decay_engine.py`、`dehydrator.py` 等）
- 前端：`frontend/`；MCP：`register(mcp)` + `custom_route` 模式
- 存储：Markdown bucket + sidecar 派生（embeddings.db、bm25、dehydration_cache）

## 4. CC / 平台前置

| 项 | 状态 |
|---|---|
| `claude` CLI | **未安装**（`Get-Command claude` 无结果）→ CC 接入 `blocked: claude_cli_missing` |
| mariposa 端口 18780/5178 | 空闲【现场核验】 |
| 本机 Python 3.12.10 / Node 24.14.0 | 可用【现场核验】 |

## 5. 生产边界确认

本次盘点全程只读；未停/重启容器、未改 compose/Tunnel/DNS、未写 `buckets-data`。
mariposa 数据全部位于 `D:\mariposa\runtime\`，与上述目录零交集。

## 6. 待核验清单（进入 legacy_behavior_matrix 追踪）

- MCP/AI 侧删除语义与 web/human 侧差异（letters.py 注释明确二者不同）
- 锁信列表可见字段、`letter_lock_state` 全状态机、到期解锁的自动归一化时机
- `import_memory.py` 导入幂等与 `_sources` 结构
- `decay_engine`/`dehydrator` 与遗忘的关系（不迁移 dream 机制，仅确认无隐藏物理删除路径）
