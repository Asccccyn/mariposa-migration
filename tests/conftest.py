"""测试环境：必须在 import mariposa 之前固定 MARIPOSA_ROOT。

保险丝（B06）：测试会清空数据库；外部传入的 MARIPOSA_ROOT 若不在允许的
测试根目录内则 fail closed，防止误把业务/生产库当测试库清掉。
路径比较基于规范化真实路径（realpath），不做字符串前缀判断。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _real(path) -> Path:
    return Path(os.path.realpath(str(path)))


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


#: 允许的测试根目录（fail closed 白名单）：
#: 1) 系统临时目录（默认）；2) 项目内 pytest basetemp；
#: 3) runtime/verification 取证隔离目录（§16.1 模板）；
#: 4) MARIPOSA_TEST_ROOTS 显式白名单（分号分隔的规范化路径）。
def _test_root_allowed(raw_root: str) -> tuple[bool, str]:
    root = _real(raw_root)
    candidates = [
        (_real(tempfile.gettempdir()), "system temp dir"),
        (_real(_REPO_ROOT / ".pytest_tmp"), "project pytest basetemp"),
        (_real(_REPO_ROOT / "runtime" / "verification"),
         "runtime/verification evidence dir"),
    ]
    for base, why in candidates:
        if _is_within(root, base):
            return True, why
    for entry in os.environ.get("MARIPOSA_TEST_ROOTS", "").split(";"):
        if entry.strip() and root == _real(entry.strip()):
            return True, "MARIPOSA_TEST_ROOTS allowlist"
    return False, ""


def _enforce_test_root_fuse() -> None:
    raw = os.environ.get("MARIPOSA_ROOT")
    if not raw:
        return
    ok, why = _test_root_allowed(raw)
    if ok:
        return
    raise RuntimeError(
        f"MARIPOSA_ROOT={raw} 拒绝用作测试根目录（fail closed）："
        "测试会清空该目录下的数据库。允许的根：系统临时目录、"
        ".pytest_tmp、runtime/verification、或 MARIPOSA_TEST_ROOTS 白名单"
        "（分号分隔）。业务/生产库绝不能作为测试根。")


_enforce_test_root_fuse()

_TEST_ROOT = os.environ.setdefault(
    "MARIPOSA_ROOT", os.path.join(tempfile.mkdtemp(prefix="mariposa-test-"))
)

# 测试根已过保险丝校验：显式允许在隔离根建库（OPS-RECALL-01 生产侧仍
# fail-closed），并打开召回运行时各通道开关（§15.1 生产默认关闭，隔离
# 验收在测试中显式启用；judge 保持 disabled——JEV-05 默认态不联网）。
os.environ.setdefault("MARIPOSA_ALLOW_CREATE", "1")
os.environ.setdefault("MARIPOSA_RECALL_ENABLED", "1")
os.environ.setdefault("MARIPOSA_WORDS_RECALL_ENABLED", "1")
os.environ.setdefault("MARIPOSA_RAW_FALLBACK_ENABLED", "1")
# recall-closure S10 出站硬门：未判断候选不得交付——测试链需要稳定
# 判断源。确定性 fake Jev（03 验收：结构级用 fake），不联网。
os.environ.setdefault("MARIPOSA_RECALL_JUDGE_PROVIDER", "test_deterministic")


def _install_test_judge() -> None:
    from mariposa.retrieval.judges import base as judge_base

    class DeterministicJudge(judge_base.JudgeProvider):
        name = "test_deterministic"

        def judge(self, query_plan, candidates, execution_context):
            items = []
            for c in candidates:
                ref = c.get("candidate_ref") or c["resource_ref"]
                items.append(judge_base.JudgeItem(
                    candidate_ref=ref,
                    candidate_version=str(c.get("content_version") or ""),
                    relevance_signal=0.55 + (hash(ref) % 40) / 100.0,
                    support_signal=None, contradiction_signal=None,
                    provider_confidence=None,
                    confidence_kind="not_applicable",
                    evaluation_status="evaluated",
                    model_id="test_deterministic",
                    prompt_version="test",
                    input_projection_version=str(
                        c.get("projection_version") or ""),
                    receipt_id=""))
            return judge_base.JudgeBatchResult(
                items=items, provider_status="evaluated",
                degraded_reason=None, cache_hits=0,
                cache_misses=len(candidates), request_count=0)

    judge_base.register_for_tests("test_deterministic", DeterministicJudge())


import pytest  # noqa: E402

from mariposa import db, schema  # noqa: E402


_install_test_judge()


@pytest.fixture(autouse=True)
def _ensure_test_judge():
    """任何测试 clear_injected 后自动重装（防顺序依赖连锁失败）。"""
    from mariposa.retrieval.judges import base as _jb
    from mariposa import config as _cfg
    if "test_deterministic" not in _jb._INJECTED:
        _install_test_judge()
    # 个别测试 cleanup 会把 provider 硬置 disabled 且不还原——
    # 每个测试的默认态恢复为确定性 judge（显式 disabled 的测试在
    # 自身作用域内自行设置）
    if _cfg.RECALL_JUDGE_PROVIDER != "test_deterministic":
        _cfg.RECALL_JUDGE_PROVIDER = "test_deterministic"
    yield
from mariposa.identity import service as identity  # noqa: E402

TOKENS = {"qiaosheng": "tok-q", "jiaming": "tok-j", "worker": "tok-w"}

FORMAL_TABLES = [
    # 子表在前，父表在后
    "i_revision_memory_relations", "i_item_revisions", "i_items",
    "i_suggestions", "i_versions", "i_documents",
    "memory_recollections", "memory_view_receipts", "memory_our_words",
    "memory_keeps",
    # 审计 2026-10-03：此前漏清——memories=0 而这三张派生/历史表
    # 残留，引入顺序依赖与孤儿统计
    "field_search_docs", "field_fts",
    "relation_corrections",
    "memory_mood_tags", "memory_moods", "memory_categories",
    "anniversary_occurrences", "anniversary_definitions",
    "audit_events", "events_outbox", "idempotency_records",  "words_fts", "words_search_docs", "search_fts",
    "retrieval_documents", "memory_versions",
    "memories", "client_bindings", "principals",
    "handoffs",
    "plan_memory_links", "plan_versions", "plans",
    "activity_events",
    "deletion_requests",
    "memory_tags", "bootstrap_snapshots",
    "memory_relations", "media_objects", "import_jobs",
    "memory_reengagements", "migration_id_map",
    # Source Layer（子表在前；FTS 虚表与普通表同样可 DELETE）
    "source_fts", "source_search_docs", "memory_source_bindings",
    "source_messages", "source_conversations", "source_import_batches",
    "source_snapshot_members", "source_conversation_snapshots",
    "source_message_versions",
    # estómago 生命周期（迁移 30/31）：live 谱系/stream 授权/绑定成员
    # manifest（子表在前）
    "source_live_revisions", "source_stream_grants",
    "memory_source_binding_members",
]
WORKSPACE_TABLES = []


def reset_all() -> None:
    schema.migrate()
    with db.formal() as c:
        c.execute("PRAGMA foreign_keys=OFF")
        for t in FORMAL_TABLES:
            c.execute(f"DELETE FROM {t}")
        c.execute("PRAGMA foreign_keys=ON")
    with db.workspace() as c:
        c.execute("PRAGMA foreign_keys=OFF")
        for t in WORKSPACE_TABLES:
            c.execute(f"DELETE FROM {t}")
        c.execute("PRAGMA foreign_keys=ON")
    schema.migrate_runtime()
    from mariposa.recall import store as recall_store
    recall_store.reset_for_tests()
    # 门禁三件套（2026-10-04）：门禁是进程内存态——不清零会让前序
    # 用例的认证失败/限速计数泄漏进后续用例（假锁定/假 429）
    from mariposa import gate as _gate
    _gate.reset_for_tests()
    identity.seed(TOKENS)


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "binding_qiaosheng"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "binding_jiaming"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "binding_worker"),
    }
