# mariposa

江乔生与周家明的自建聊天与共同生活应用。当前仓库为**隔离开发版**：
独立目录、独立数据库，不读写旧 Ombre 生产数据；一切开发与测试使用合成数据。

## 快速开始（开发）

```powershell
# 环境检查
scripts/doctor.ps1
# 初始化依赖（首次）
python -m venv .venv
.venv\Scripts\pip install -r requirements.lock
# 种子 + 启动（127.0.0.1:18780）
scripts/start-dev.ps1
```

开发 token 生成于 `runtime/dev_tokens.json`（不入 git），在 Web 页粘贴对应主体的 token。

## 测试

```powershell
.venv\Scripts\python -m pytest tests -q
```

## 架构速览

- `backend/mariposa/` — FastAPI 后端
  - `identity/` 主体与绑定（token -> principal，服务端解析，参数不能自报）
  - `capabilities/registry.py` 统一能力注册表：HTTP / MCP / CC 共用同一 handler
  - `memory/` 正式桶、版本、遗忘审批应用、恢复
  - `relations/` 五域关系（memory/I/source/plan/word）+ 纠错历史与
    领域原子写；`deletion/` 删除申请两条路径（江乔生申请/周家明直删）
  - `recall/` 召回运行时（Session/预算/Round2/Raw 深搜）；
    `bootstrap/` 开窗装配；`source/` 原文层（导入/绑定/区间）
  - `retrieval/` 可检索投影 + FTS5（中文预分词）+ 查询安全编译
- `runtime/formal/mariposa.sqlite3` 正式库；
  `runtime/recall/recall.sqlite3` 召回运行库（workspace 已随
  Workspace Tasks/Lease 退役移除）
- 删除闭环（v2.0）：江乔生申请（配额：lifetime 5/日 10）-> 周家明
  approve 物理删除 / 周家明 memory.delete 直删；五域关系硬门拦截

## 边界

- 旧 Ombre（`D:\Ombre-Brain-*`）只读、不共写、不复制私人内容到本仓库
- 语义检索：provider 未配置时显式 `degraded: semantic_unavailable`，不伪造向量
- 退役业务（letters/Home/Self/Diary/Calendar/Reminder/Moments/
  Quotes/Review/Candidate/Proposal/Workspace Tasks/Legacy Raw/
  Memory archive）不再恢复；能力合同见 contracts/capabilities.v1.json
  （与 Registry 同源生成，`scripts/export_contracts.py` 刷新）
