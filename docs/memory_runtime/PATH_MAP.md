# 路径映射（PATH_MAP）：v1.4 计划 → 实际落点

基线 HEAD `29b5e4f` 之上的本批变更。已实现/需修改/需新增三列对应
v1.4 §11；实际落点以本文件为准（Git 历史保留旧版）。

## 新增模块

| v1.4 拟名 | 实际落点 | 说明 |
|---|---|---|
| recall/models.py | `backend/mariposa/recall/models.py` | QueryPlan 校验/通道白名单/枚举 |
| recall/service.py | `backend/mariposa/recall/service.py` | 统一编排 + 七动作 + words 入口 + context.validate |
| recall/store.py | `backend/mariposa/recall/store.py` | runtime 持久化/CAS/操作幂等/过期清理 |
| recall/state_machine.py | `backend/mariposa/recall/state_machine.py` | 九状态与动作前置 |
| recall/budget.py | `backend/mariposa/recall/budget.py` | burst/轮次/显式继续 |
| retrieval/query_plan.py | `backend/mariposa/retrieval/query_plan.py` | terms/phrase 编译 + AllowedScope |
| retrieval/fusion.py | `backend/mariposa/retrieval/fusion.py` | RRF/族内去重/**作用域内词频统计**（scoped_lexical_search） |
| retrieval/selection.py | `backend/mariposa/retrieval/selection.py` | 0—3 交付/多样性/冲突披露 |
| retrieval/evidence.py | `backend/mariposa/retrieval/evidence.py` | 八类 evidence_kind + 无指令权限包装 + 截断标记 |
| retrieval/words.py | `backend/mariposa/retrieval/words.py` | words 派生索引（fingerprint 校验+重建）/words_search/get_word |
| retrieval/judges/base.py | `backend/mariposa/retrieval/judges/base.py` | JudgeProvider/DisabledJudge/信号消毒/测试注入点 |
| retrieval/judges/typesafe_jev.py | `backend/mariposa/retrieval/judges/typesafe_jev.py` | 真实适配器（默认不启用；无授权策略/无 key 时构造即拒绝） |
| raw/recall.py | `backend/mariposa/raw/recall.py` | scope resolver + 分批游标全历史扫描 + continuation |

## 修改的既有文件

| 文件 | 改动 |
|---|---|
| `config.py` | 数据根 fail-fast（非 Windows 必须显式 MARIPOSA_ROOT）；ALLOW_DB_CREATE；recall 全套参数/开关（默认关闭）；RECALL_DB |
| `db.py` | `recall_runtime()` 连接 |
| `schema.py` | 迁移 13（mariposa_db_meta + words 派生表）；`_require_identity`/`_stamp_identity` 启动身份校验；RUNTIME_MIGRATIONS + `migrate_runtime()` |
| `retrieval/semantic.py` | `semantic_search` 增加 extra_where/extra_params（scope 前置到评分与 Top-K 之前；不传=旧行为） |
| `capabilities/registry.py` | 新增 10 能力；召回能力集不走 formal 幂等（runtime operation_id 隔离） |
| `capabilities/input_schemas.py` | 10 个新能力的严格输入 schema |
| `app.py` | lifespan 增加 `migrate_runtime()` |
| `memory/service.py` | `get()` 顶层补 content_role/instruction_authority |
| `memory/our_words.py` | `list_for` 返回补标注 |
| `raw/service.py` | `search` 命中补标注（raw_verbatim/无指令权限） |
| `tests/conftest.py` | 测试根 ALLOW_CREATE + recall 开关；reset 清 runtime 库与 words 派生表 |
| `contracts/capabilities.v1.json` | 重新导出（212 项，含新能力） |
| `tests/acceptance/test_linshijian_review.py` | 既有未提交改动保留（Windows venv 路径→sys.executable） |

## 契约与脚本

- `contracts/recall_runtime.v1.schema.json`（新增）：能力/枚举/证据包/预算默认值/开关
- `scripts/verify_recall_runtime.py`（新增）：隔离根自检 9 项
- `scripts/eval_recall.py`（新增）：A/B 对照评测入口（C 组接口保留未执行）

## 有意不做（留待授权/后续批次）

- 真实 Jev 调用与 C 组评测（外部授权未给）
- estómago 宿主工程（不存在于本仓库）
- 生产数据根物理迁移到 §17 七目录布局（逻辑映射已建立：runtime/recall）
- raw.context.preview/expand 完整票据流程（属 spec_v2 P5 原文 v2 批次；本批以 raw recall 有限片段 + 既有 raw.read 覆盖 Mariposa 侧语义）
- quotes 体系的标注补齐（另一资源域，未动）
- 主体级 session 配额（v1.4 §9.3 后半；单 session 预算已实现）
- Mac 常驻 test-guard（以 AGENTS.md 前台约束替代，NEXT 已记）
