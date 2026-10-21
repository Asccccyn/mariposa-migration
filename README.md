# mariposa

- 2026-10-21（代码 `6267df3` 已验证，待部署）：JEV v4语义输入去重，注入仅date/text，后台引用支持同桶全量；OR通道准入限制保留。[实现、验证与状态纠正](docs/reports/JEV_SEMANTIC_PROJECTION_20261021.md)。

- 2026-10-21（已开启并核验）：JEV自动浮现，代码 `af2dbbe`；窗口/轨迹与JEV缓存哈希保持不变，零OR验证请求。[生产入口与回滚](docs/reports/JEV_SURFACING_ACTIVATION_20261021.md)。

- 2026-10-21（工作树，未部署）：JEV 缺 chunk 结束块的完整响应严格恢复，零补发、缓存规则不变。[原因与验证](docs/reports/JEV_INCOMPLETE_READ_20261021.md)。

- 2026-10-21（工作树，未部署）：JEV 同轮浮现校准、15秒总预算及编辑重发新修订任务；缓存前缀不变。[验证说明](docs/reports/JEV_SURFACING_CALIBRATION_20261021.md)。

宿主用户与char的记忆与原文应用。

## 2026-10-19 · EST_MARIPOSA_AUTO_SURFACING_V1（自动浮现四能力，默认关）

- 新增 `backend/mariposa/surfacing/`（prepare/validate/check 编排+严格选择+两阶段 TypeSafe judge 适配+运行库 store）与 `retrieval/candidate_service.py`（候选生成从 recall/service 等价提取，主动 recall 反向 import——单一候选生成接缝）；`memory/detail.py` 按桶全量详情（八段/五域/码点分页/无 view_receipt）。
- 运行库迁移 16（surface 五表）；registry 四新面 `memory.surfacing.prepare/validate/check`+`memory.detail.read`（运行时幂等隔离，契约导出已重生成 122 能力）；config `SURFACE_*` 段（`MARIPOSA_SURFACE_ENABLED` 默认关；cost cap null=有付费即 blocked）。
- 测试：新增 unit 43+integration 4 全绿；全量 unit 1283 绿/2 失败=她在途批遗留（words 惰性重建/读流上限，与本批无关已复验）。报告：estómago 仓 `docs/reports/EST_MARIPOSA_AUTO_SURFACING_V1/`。开发和测试使用独立数据根、合成数据，
不读写旧 Ombre 生产数据；当前正式数据状态不能由历史开发说明推断。

## 快速开始（开发）

```bash
# 初始化依赖（首次）
python -m venv .venv
.venv/bin/pip install -r requirements.lock
# 种子 + 启动（127.0.0.1:18780；Windows 时代 .ps1 脚本已随 Mac 迁移退役删除）
PYTHONPATH=backend MARIPOSA_ROOT=<数据根> MARIPOSA_ALLOW_CREATE=1 .venv/bin/python -m mariposa.devseed
PYTHONPATH=backend MARIPOSA_ROOT=<数据根> .venv/bin/python -m uvicorn mariposa.app:app --port 18780
```

正式前端位于 `frontend/`：`cd frontend && pnpm install && pnpm build`。
网页登录使用已配置的身份密码；开发 token 位于开发数据根的
`runtime/dev_tokens.json`（不入 git），供隔离 API 测试使用。

## 测试

- 遵守 AGENTS.md 硬约束（2026-09-22 内存事故后）：pytest 分批、
  指定文件/用例、`-q -x`、前台显式超时（60/120s），禁缓存/字节码
  写入仓库；预期内存 >2GB 或 >20 分钟先确认再跑
- 2026-10-18 夜批基线：unit 1240 绿 / 2 失败（words 惰性重建移除与
  读流上限预检属原文分页在途批的既有失败，与本批无关）——本批=
  我们的话只剩原文（删 expression_kind 全链+迁移 39）+ memory.update
  就地改（版本计数器仍递进，票据防串改依赖）+ 话语新增/修改并入
  （说话人随出处、逐字核验连续片段）。该批 **已部署**（2026-10-19：
  estómago 先重启→迁移 39+mariposa 重启，451 记忆/86 话语完好；commit
  8c8f131 已 push，含她在途心情修订/原文分页同树批）

```bash
# 前台、单文件、120 秒硬超时；无字节码或 pytest 缓存写入仓库
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PYTEST'
import os, subprocess, tempfile
with tempfile.TemporaryDirectory(prefix="mariposa-test-") as root:
    env = dict(os.environ, MARIPOSA_ROOT=root, MARIPOSA_ALLOW_CREATE="1",
               MARIPOSA_SEMANTIC_PROVIDER="", MARIPOSA_CODEX_LIVE_ENABLED="0")
    subprocess.run([".venv/bin/python", "-m", "pytest", "-q", "-x",
                    "-p", "no:cacheprovider", "--basetemp", root + "/pytest",
                    "tests/unit/test_retired_capabilities_negative.py"],
                   env=env, check=True, timeout=120)
PYTEST
```

## 浏览器回归

现行 React 前端的四条隔离流程在 `tests/e2e/library.smoke.cjs`，覆盖登录、
无日期分页、编辑读回、合法心情补标签、回忆修订与撤销留、原文搜索续页、
角色按钮、删除申请/撤回/批准和末页删除。测试只使用自动创建的合成数据根。

已有前端依赖、Playwright 与 Chromium 后运行：

```bash
.venv/bin/python -B scripts/run_browser_checks.py
```

若 Playwright 不在 Node 模块搜索路径，用 `MARIPOSA_PLAYWRIGHT_MODULE` 指定
其模块目录；若浏览器未随 Playwright 安装，用 `MARIPOSA_CHROMIUM_EXECUTABLE`
指定独立 Chrome/Chromium 可执行文件。脚本使用新的无头浏览器上下文，不使用
个人浏览器配置。默认只监听 `127.0.0.1:18881`，端口已占用时失败，不接管旧进程；
测试限时，服务器与浏览器进程结束后清理；源码和构建使用临时副本，
不会刷新当前 checkout 的 `frontend/dist`，不会重启正在运行的服务。

## 独立纪念日退役与开窗验证

2026-10-15：用户已明确退役本仓独立纪念日子系统。旧读取/输出/续页
与 est 消费均删除；升级迁移仅封存可能存在的旧行，不销毁历史。
重要日期正本复用 Superposition。记忆的 `anniversary` 分类及永久 WIDE、
长心情分页均保留。

在 est 仓运行 `python3 scripts/verify-mariposa-bootstrap.py --mariposa-repo <本仓路径>`，
用临时合成库与真实 HTTP 验证完整心情、局部失败隔离、原快照复核、失效重建、
系统告警、取消和旧段退役。普通心情失败不连带丢弃已复核的 I 与计划；
合同细节见 `docs/memory_runtime/CURRENT.md`。
旧 66 发生项联调报告只属退役前历史，不参与现行验收。未部署生产。

## 架构速览

- `backend/mariposa/` — FastAPI 后端
  - `identity/` 主体与绑定（token -> principal，服务端解析，参数不能自报）
  - `capabilities/registry.py` 统一能力注册表：HTTP / MCP / CC 共用同一 handler
  - `memory/` 正式桶、版本（遗忘/恢复业务已随 v1.7 退役；删除走
    Relation/Deletion v2.0 两条路径）
  - `relations/` 五域关系（memory/I/source/plan/word）+ 纠错历史与
    领域原子写；`deletion/` 删除申请两条路径（宿主用户申请/char直删）
  - `recall/` 召回运行时（Session/预算/Round2/Raw 深搜）；
    `bootstrap/` 开窗装配；`source/` 原文层（导入/绑定/区间）
  - `retrieval/` 可检索投影 + FTS5（中文预分词）+ 查询安全编译
- `runtime/formal/mariposa.sqlite3` 正式库；
  `runtime/recall/recall.sqlite3` 召回运行库；
  `runtime/workspace/workspace.sqlite3` 辅助库（仅存 recall 运行
  相关辅助表与库身份元数据，Workspace Tasks/Lease 业务已退役）
- 删除闭环（v2.0）：宿主用户申请（配额：lifetime 5/日 10）-> char
  approve 物理删除 / char memory.delete 直删；五域关系硬门拦截

## 边界

- 旧 Ombre（`D:\Ombre-Brain-*`）只读、不共写、不复制私人内容到本仓库
- 语义检索：provider 未配置时显式 `degraded: semantic_unavailable`，不伪造向量
- 退役业务（letters/Home/Self/Diary/Calendar/Reminder/Moments/
  Quotes/Review/Candidate/Proposal/Workspace Tasks/Legacy Raw/
  Memory archive）不再恢复；能力合同见 contracts/capabilities.v1.json
  （与 Registry 同源生成，`scripts/export_contracts.py` 刷新）
