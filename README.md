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
  - `workspace/` 独立工作区（草稿/提案/审批协调，跨库提交信封协议）
  - `retrieval/` 可检索投影 + FTS5（中文预分词）+ 查询安全编译
- `runtime/formal/mariposa.sqlite3` 正式库；`runtime/workspace/workspace.sqlite3` 工作区库
- 遗忘闭环：候选扫描 -> 工作区草稿 -> 提交（hash 冻结）-> 乔生/周家明任一方审批
  -> 正式库单事务切换版本+投影+FTS -> 可恢复

## 边界

- 旧 Ombre（`D:\Ombre-Brain-*`）只读、不共写、不复制私人内容到本仓库
- 语义检索：provider 未配置时显式 `degraded: semantic_unavailable`，不伪造向量
- letters/物理删除：等待现场行为核验，标 `blocked: legacy_contract_unverified`
