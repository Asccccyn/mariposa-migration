"""mariposa 配置。

所有值可由环境变量覆盖；默认值来自《mariposa § 01 工程执行文档》§21，
均为【默认】初值，不是已冻结决策。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# 数据根（v1.4 §10.1）：Mac/Linux 必须显式 MARIPOSA_ROOT，不回退 Windows
# 默认路径——错误路径应启动失败，不静默在别处建一个空正式库。
# Windows 保留旧默认 D:\mariposa（既有生产根）。
_root_env = os.environ.get("MARIPOSA_ROOT")
if _root_env:
    PROJECT_ROOT = Path(_root_env)
elif sys.platform == "win32":
    PROJECT_ROOT = Path(r"D:\mariposa")
else:
    raise RuntimeError(
        "MARIPOSA_ROOT 未设置：非 Windows 环境必须显式指定数据根"
        "（v1.4 §10.1 fail-fast；测试由 conftest 自动设置临时隔离根）。")

RUNTIME_DIR = PROJECT_ROOT / "runtime"
FORMAL_DB = RUNTIME_DIR / "formal" / "mariposa.sqlite3"
WORKSPACE_DB = RUNTIME_DIR / "workspace" / "workspace.sqlite3"
# Recall Session 等短期但需跨重启的运行状态（v1.4 §10.2；TTL 清理有界，
# 与正式库/工作区库相互独立）。
RECALL_DB = RUNTIME_DIR / "recall" / "recall.sqlite3"
LOG_DIR = RUNTIME_DIR / "logs"
# Source Layer（原文层）：Raw Archive 母本 + 上传暂存，均在宿主数据根，
# 容器/镜像不可吞噬（实施规格 §2）；runtime/ 整体在 .gitignore。
SOURCE_RAW_DIR = RUNTIME_DIR / "source" / "raw"
SOURCE_INCOMING_DIR = RUNTIME_DIR / "source" / "incoming"
SOURCE_PROJECTION_VERSION = "source_projection_v1"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# Source Layer 资源边界（复核 v1.1 §6）：工程初值，可环境变量覆盖；
# 超限走结构化拒绝，不允许吃满内存再失败。
SOURCE_MAX_ELEMENT_BYTES = _env_int("MARIPOSA_SOURCE_MAX_ELEMENT_BYTES", 64 << 20)
SOURCE_MAX_MESSAGE_TEXT_BYTES = _env_int(
    "MARIPOSA_SOURCE_MAX_MESSAGE_TEXT_BYTES", 2 << 20)
SOURCE_MAX_MESSAGE_CONTENT_BYTES = _env_int(
    "MARIPOSA_SOURCE_MAX_MESSAGE_CONTENT_BYTES", 4 << 20)
SOURCE_MAX_ZIP_MEMBERS = _env_int("MARIPOSA_SOURCE_MAX_ZIP_MEMBERS", 20000)
SOURCE_MAX_ZIP_MEMBER_BYTES = _env_int(
    "MARIPOSA_SOURCE_MAX_ZIP_MEMBER_BYTES", 1 << 30)
SOURCE_MAX_ZIP_TOTAL_BYTES = _env_int(
    "MARIPOSA_SOURCE_MAX_ZIP_TOTAL_BYTES", 2 << 31)
SOURCE_UPLOAD_MAX_BYTES = _env_int("MARIPOSA_SOURCE_UPLOAD_MAX_BYTES", 2 << 31)
#: md 转写文件级字节上限（SRC-06）：json 走元素流式限额，md 在
#: 读入前按 stat 快路径拒绝——拒绝路径先于整文件内存放大
SOURCE_MD_MAX_BYTES = _env_int("MARIPOSA_SOURCE_MD_MAX_BYTES",
                               512 << 20)
SOURCE_RANGE_MAX_MESSAGES = _env_int("MARIPOSA_SOURCE_RANGE_MAX_MESSAGES", 500)
SOURCE_RANGE_MAX_TEXT_BYTES = _env_int(
    "MARIPOSA_SOURCE_RANGE_MAX_TEXT_BYTES", 1 << 20)
# running 批次认领租约：超过该时长未完成的批次可被同文件重导接管
SOURCE_IMPORT_LEASE_MINUTES = _env_int("MARIPOSA_SOURCE_IMPORT_LEASE_MINUTES", 120)
# 在线 live ingest 工程初值（契约 §source.ingest：50 条/1MiB 规范化
# JSON；是工程护栏不是业务截断许可，超限结构化报错由宿主分批）
SOURCE_LIVE_MAX_MESSAGES = _env_int("MARIPOSA_SOURCE_LIVE_MAX_MESSAGES", 50)
SOURCE_LIVE_MAX_JSON_BYTES = _env_int(
    "MARIPOSA_SOURCE_LIVE_MAX_JSON_BYTES", 1 << 20)

ENV_FILE = PROJECT_ROOT / ".env"

# 新建数据库必须显式允许（OPS-RECALL-01）：测试根由 conftest 打开；
# 生产首次建库/换根走显式 MARIPOSA_ALLOW_CREATE=1，防止错误路径静默
# 建空库。
ALLOW_DB_CREATE = _flag("MARIPOSA_ALLOW_CREATE")

HTTP_BIND = os.environ.get("MARIPOSA_BIND", "127.0.0.1")
HTTP_PORT = int(os.environ.get("MARIPOSA_PORT", "18780"))

RELATIONSHIP_TIMEZONE = os.environ.get("MARIPOSA_TZ", "Asia/Shanghai")

PROJECTION_REVISION = "retrieval_projection_v1"
# 投影/迁移标识（v1.7：不再表示遗忘策略；遗忘链已退役）
POLICY_VERSION = "mariposa_v1_7"


SEMANTIC_PROVIDER = os.environ.get("MARIPOSA_SEMANTIC_PROVIDER", "")  # 空 = 未配置

CONTRACT_VERSION = "1.0"

# ---------- 召回运行时（v1.4；初值为隔离评测保护参数，非业务终值） ----------
# 各开关默认关闭，完成隔离验收后分项启用（§15.1）。
RECALL_RUNTIME_ENABLED = _flag("MARIPOSA_RECALL_ENABLED")
RECALL_WORDS_ENABLED = _flag("MARIPOSA_WORDS_RECALL_ENABLED")
RECALL_RAW_FALLBACK_ENABLED = _flag("MARIPOSA_RAW_FALLBACK_ENABLED")
# 未决业务项（v1.3 §23）：遗忘后的 words 显式检索保持 disabled，
# decision_state=PENDING_OWNER_DECISION；确认前不得借任何旁路恢复。
WORDS_FORGOTTEN_RECALL = "disabled"
WORDS_FORGOTTEN_DECISION_STATE = "PENDING_OWNER_DECISION"

RECALL_POLICY_VERSION = "recall-v1.8"
WORDS_PROJECTION_VERSION = "words-projection-v1"
RECALL_SESSION_TTL_HOURS = int(
    os.environ.get("MARIPOSA_RECALL_SESSION_TTL_HOURS", "24"))
RECALL_BURST_ROUNDS = 3                # 一个补查 burst：初次 + 最多 2 轮补查
RECALL_SESSION_BURSTS_MAX = 3
RECALL_ALTERNATE_PER_BURST = 2
RECALL_RELATION_HOPS = 1
RECALL_RELATION_NEIGHBOR_CAP = 10
RECALL_LEXICAL_K = 20
RECALL_DENSE_K = 20
RECALL_CANDIDATE_UNION_CAP = 60
# S19：scope 候选池安全阀——词法路 keyset 分页本可取尽全库，
# 此上限只防失控规模（内存/时延）；触顶时 coverage 诚实标
# partial_pool_truncated，不签 complete_within_scope
RECALL_POOL_MAX_BUCKETS = int(
    os.environ.get("MARIPOSA_RECALL_POOL_MAX_BUCKETS", "20000"))
RECALL_JUDGE_CANDIDATE_CAP = 40
RECALL_JUDGE_HTTP_ATTEMPTS = 40
RECALL_DELIVERY_LIMIT = 3
RECALL_AUTO_TOP1 = False                # 未校准前禁用"自动高置信单条"
RECALL_EXCERPT_CHARS = 600
RECALL_PACKET_TEXT_CHARS = 4000
RECALL_PACKET_BYTES_MAX = 24576
RECALL_RRF_K = 60
RECALL_TOKENIZER = "unavailable"        # 主模型 tokenizer 接入后替换

# MANUAL_HANDOFF_JUDGE_SWITCH_V1（2026-10-08）：判断层运行时正本=
# 正式库 recall_judge_policy（recall/judge_policy.py）。本 env 只在政策
# 表为空时作首次导入（旧部署兼容），不与数据库形成两套优先级；
# credentials（key/model）仍经 env 引用。
RECALL_JUDGE_PROVIDER = os.environ.get(
    "MARIPOSA_RECALL_JUDGE_PROVIDER", "disabled")  # disabled | typesafe_jev
RECALL_JUDGE_MODEL_ID = (
    os.environ.get("MARIPOSA_RECALL_JUDGE_MODEL_ID")
    or os.environ.get("TYPESAFE_DEFAULT_MODEL")
    or "jev-latest"
)
RECALL_JUDGE_PROMPT_VERSION = "mariposa-relevance-v1"
RECALL_JUDGE_SCHEMA_VERSION = "typesafe-systemone-v1"
RECALL_JUDGE_CONCURRENCY = 2
RECALL_JUDGE_TIMEOUT_MS = int(
    os.environ.get("MARIPOSA_RECALL_JUDGE_TIMEOUT_MS", "5000"))
RECALL_JUDGE_ROUND_DEADLINE_MS = 20000
RECALL_JUDGE_BATCH_SIZE = max(
    1, _env_int("MARIPOSA_RECALL_JUDGE_BATCH_SIZE", 12))
# query×candidate 动态精排缓存是可重建 derived metadata，不是正式记忆。
# 绑定 query/candidate/model/prompt/policy/schema 指纹；TTL 主要防止
# jev-latest 这类别名在服务端换代后无限复用旧判断。
RECALL_JUDGE_CACHE_TTL_HOURS = max(
    1, _env_int("MARIPOSA_RECALL_JUDGE_CACHE_TTL_HOURS", 168))


def ensure_dirs() -> None:
    for p in (RUNTIME_DIR, FORMAL_DB.parent, WORKSPACE_DB.parent,
              RECALL_DB.parent, LOG_DIR, SOURCE_RAW_DIR, SOURCE_INCOMING_DIR):
        p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 访问门禁
# 门禁三件套之一（2026-10-04 收口计划）：应用层限速 + 认证失败锁定。
# 内存态单进程实现（uvicorn 单 worker 部署形态）；进程重启计数清零
# 是可接受的 fail-soft（重启后攻击者重新累计即重新触发）。
# 失败锁定按"认证来源 IP"计数，指数退避封顶；限速按 principal 分
# 读/写两档 + 匿名（认证失败）按 IP 一档。
# GATE-04（2026-10-04 复审 P2）：阈值加载即钳制为正整数、锁定封顶
# 不低于基准——零/负值会进空 deque 索引（IndexError 500）或让锁定
# 永不升级，配置错误不允许以静默危险值生效。
GATE_AUTH_FAIL_THRESHOLD = max(1, _env_int(
    "MARIPOSA_GATE_AUTH_FAIL_THRESHOLD", 5))
GATE_LOCKOUT_BASE_SECONDS = max(1, _env_int(
    "MARIPOSA_GATE_LOCKOUT_BASE_SECONDS", 900))          # 15 分钟
GATE_LOCKOUT_MAX_SECONDS = max(
    GATE_LOCKOUT_BASE_SECONDS,
    _env_int("MARIPOSA_GATE_LOCKOUT_MAX_SECONDS", 86400))  # 封顶 24 小时
GATE_RATE_READ_PER_MIN = max(1, _env_int(
    "MARIPOSA_GATE_RATE_READ_PER_MIN", 240))
GATE_RATE_WRITE_PER_MIN = max(1, _env_int(
    "MARIPOSA_GATE_RATE_WRITE_PER_MIN", 30))
GATE_RATE_ANON_PER_MIN = max(1, _env_int(
    "MARIPOSA_GATE_RATE_ANON_PER_MIN", 60))


# -------------------------------------------- 自动记忆浮现（surface-v1）
# EST_MARIPOSA_AUTO_SURFACING_V1（2026-10-19）：本段是 M 侧设计参数的
# 唯一配置正本（包内 contracts/policy-defaults.json 为设计出处；M 侧
# 不再复制第二份）。全部数值为工程初值，不是已校准指标或用户历史事实。
# 默认关闭；judge 全局开关（recall_judge_policy 行）独立于本 flag——
# 两者任一关闭，自动路径都 blocked，不偷偷调用付费判断。
SURFACE_ENABLED = _flag("MARIPOSA_SURFACE_ENABLED", "")
# v2（她 2026-10-20 裁定 P0）：判断→分组→选择→交付统一到 surfacing/contract.py
# 一份准入合同。revision 升 2 使 v1 下 prepare 的 run 在 validate 处按
# SURFACE_POLICY_CHANGED 失效；v1 判断缓存因 schema 版本换键自然不可达，
# 不删不截。
SURFACE_POLICY_REVISION = 2
SURFACE_POLICY_VERSION = "surface-v2"
SURFACE_PROMPT_PREPARE = "surface_prepare_v2"
SURFACE_PROMPT_DELIVERY = "surface_delivery_v2"
# 判断 schema 版本（judge-decision.schema.json 的 schema_version const）
SURFACE_JUDGE_SCHEMA_VERSION = "surface-judge-v2"
# 确定性硬校验规则版本（facts.py）；进合同指纹与 policy 快照
SURFACE_FACTS_RULES_VERSION = "facts-v2"
# P2（她 2026-10-20 二批复）：召回词项/强事件身份规则与确定性准入规则版本，
# 同样进合同指纹——任一变化即换缓存键、使旧 run 在 validate 处失效
SURFACE_ANCHORS_RULES_VERSION = "anchors-v1"
SURFACE_DETERMINISTIC_RULES_VERSION = "deterministic-v1"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


SURFACE_DELIVERY_VALIDATION_DEADLINE_MS = _env_int(
    "MARIPOSA_SURFACE_DELIVERY_VALIDATION_DEADLINE_MS", 2000)
SURFACE_MAX_RECEIPT_AGE_MS = _env_int(
    "MARIPOSA_SURFACE_MAX_RECEIPT_AGE_MS", 250)
SURFACE_PREPARE_DEADLINE_MS = _env_int(
    "MARIPOSA_SURFACE_PREPARE_DEADLINE_MS", 20000)
SURFACE_CANDIDATE_TTL_SECONDS = _env_int(
    "MARIPOSA_SURFACE_CANDIDATE_TTL_SECONDS", 86400)
SURFACE_CANDIDATE_UNION_CAP = _env_int(
    "MARIPOSA_SURFACE_CANDIDATE_UNION_CAP", 60)
SURFACE_CANDIDATES_PER_HYPOTHESIS_CAP = _env_int(
    "MARIPOSA_SURFACE_CANDIDATES_PER_HYPOTHESIS_CAP", 20)
SURFACE_MAX_EVENT_HYPOTHESES = _env_int(
    "MARIPOSA_SURFACE_MAX_EVENT_HYPOTHESES", 24)
SURFACE_MAX_RETRIEVAL_QUERIES_PER_JOB = _env_int(
    "MARIPOSA_SURFACE_MAX_RETRIEVAL_QUERIES_PER_JOB", 8)
SURFACE_JUDGE_BATCH_SIZE = _env_int(
    "MARIPOSA_SURFACE_JUDGE_BATCH_SIZE", 12)
SURFACE_MAX_AUTO_ADDED_TOKENS_PER_TURN = _env_int(
    "MARIPOSA_SURFACE_MAX_AUTO_ADDED_TOKENS_PER_TURN", 4096)
SURFACE_MAX_AUTO_RETAINED_TOKENS_PER_WINDOW = _env_int(
    "MARIPOSA_SURFACE_MAX_AUTO_RETAINED_TOKENS_PER_WINDOW", 8192)
SURFACE_MAX_AUTO_PACKET_BYTES = _env_int(
    "MARIPOSA_SURFACE_MAX_AUTO_PACKET_BYTES", 24576)
SURFACE_DETAIL_PAGE_BYTES = _env_int(
    "MARIPOSA_SURFACE_DETAIL_PAGE_BYTES", 24576)
SURFACE_MAX_SNAPSHOT_RETRIES = _env_int(
    "MARIPOSA_SURFACE_MAX_SNAPSHOT_RETRIES", 1)
# 费用上限 None 不是无限（policy-defaults rules）：有付费出站即
# blocked，显式批准后才配置。daily_judge_cost_cap_usd 默认 None；
# 设置后按台账累计对账。
SURFACE_DAILY_JUDGE_COST_CAP_USD = (
    None if not os.environ.get("MARIPOSA_SURFACE_DAILY_JUDGE_COST_CAP_USD")
    else _env_float("MARIPOSA_SURFACE_DAILY_JUDGE_COST_CAP_USD", 0.0))
SURFACE_DAILY_JUDGE_REQUEST_CAP = _env_int(
    "MARIPOSA_SURFACE_DAILY_JUDGE_REQUEST_CAP", 100)
SURFACE_POSITIVE_NOUL_THRESHOLD = _env_float(
    "MARIPOSA_SURFACE_POSITIVE_NOUL_THRESHOLD", 0.98)
SURFACE_RISK_NOUL_THRESHOLD = _env_float(
    "MARIPOSA_SURFACE_RISK_NOUL_THRESHOLD", 0.02)
# same_occurrence 确认门（唯一语义门）。默认 0.98 是保守值=实际不会
# 自动确认任何候选；校准需她另批并按审计包装规则跑真测。历史真例
# 分布 .51–.71、负例 .03–.72（S05 日期冲突），不能用单一分数定交付——
# 交付还要过 facts 与竞争合同。
SURFACE_SAME_OCCURRENCE_THRESHOLD = _env_float(
    "MARIPOSA_SURFACE_SAME_OCCURRENCE_THRESHOLD", 0.98)
# P2 确定性准入：唯一性只能在**未截断**的日期集上证明；日期集超过此规模
# 一律 UNKNOWN（不声明唯一）。Dense 每作业预算：确定性失败后才按预算补查，
# 且只在模型已常驻、不重嵌入时运行。
SURFACE_DETERMINISTIC_MAX_DATE_SET = _env_int(
    "MARIPOSA_SURFACE_DETERMINISTIC_MAX_DATE_SET", 200)
SURFACE_DENSE_QUERIES_PER_JOB = _env_int(
    "MARIPOSA_SURFACE_DENSE_QUERIES_PER_JOB", 1)
SURFACE_MAX_JUDGE_REQUEST_BYTES = _env_int(
    "MARIPOSA_SURFACE_MAX_JUDGE_REQUEST_BYTES", 65536)
SURFACE_MAX_JUDGE_JOB_INPUT_BYTES = _env_int(
    "MARIPOSA_SURFACE_MAX_JUDGE_JOB_INPUT_BYTES", 262144)
SURFACE_MAX_EVENT_BODY_BYTES = _env_int(
    "MARIPOSA_SURFACE_MAX_EVENT_BODY_BYTES", 24576)
SURFACE_MAX_SOURCE_CONTEXT_BYTES = _env_int(
    "MARIPOSA_SURFACE_MAX_SOURCE_CONTEXT_BYTES", 98304)
