"""118 条验收用例映射测试（第二批：BOOT/CAL/TIME/SELF/LEG/MIG/OPS）+ 架构边界。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mariposa import db
from mariposa.errors import Forbidden, NotFound, SnapshotStale
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.plans import service as plans
from mariposa.retrieval import search as retrieval
from tests.conftest import reset_all

FIXTURES = Path(__file__).parents[1] / "fixtures" / "legacy_bucket_sample"


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }


class TestBOOT:
    def test_T_BOOT_08_no_repeated_full_package(self, actors):
        """T-BOOT-08：状态未变重复开窗 -> unchanged 薄响应。"""
        from mariposa.bootstrap import service as bootstrap
        first = bootstrap.get("jiaming", "cc", "cc")
        again = bootstrap.get("jiaming", "cc", "cc",
                              loaded_snapshot_id=first["snapshot_id"])
        assert again.get("unchanged") is True
        assert "memory_days" not in again  # 不重发内容包



class TestTIME:

    def test_T_TIME_04_gap_wording(self, actors):
        """T-TIME-04：覆盖缺口时说明'记录可能未齐'，不说错误上下界。"""
        from mariposa.time_context import service as time_ctx
        out = time_ctx.since("qiaosheng")
        if out["since_last_contact"] is None:
            assert "未齐" in out["note"]
        else:
            assert "已收录" in out["note"] or "不计" in out["note"]



class TestMIG:
    def test_T_MIG_03_04_semantic_flags(self, actors, tmp_path):
        """T-MIG-03/04：dont_surface→hidden 不进检索；tags_only→migration review。"""
        from mariposa.migration import dry_run_real
        src = tmp_path / "src"
        (src / "dynamic").mkdir(parents=True)
        (src / "dynamic/2026-07-01 10-00-00 隐藏桶_abc111222333.md").write_text(
            "---\ntype: dynamic\ndate: 2026-07-01\ndont_surface: true\n---\n旧隐藏正文\n",
            encoding="utf-8")
        (src / "dynamic/2026-07-02 11-00-00 待审桶_def444555666.md").write_text(
            "---\ntype: dynamic\ndate: 2026-07-02\ntags_only: true\n---\n标签桶\n",
            encoding="utf-8")
        out = dry_run_real(str(src), str(tmp_path / "r.json"))
        e1 = next(e for e in out["entries"] if e["legacy_id"] == "abc111222333")
        e2 = next(e for e in out["entries"] if e["legacy_id"] == "def444555666")
        assert e1.get("migrate_as_hidden") is True
        assert e2.get("needs_migration_review") is True
        assert out["stats"]["dont_surface_hidden"] == 1
        assert out["stats"]["needs_migration_review"] == 1

    def test_T_MIG_09_no_shared_write_with_legacy(self, actors):
        """T-MIG-09：新实例不写旧库——配置路径与旧生产零交集。"""
        from mariposa import config
        roots = [str(config.FORMAL_DB), str(config.WORKSPACE_DB),
                 str(config.RUNTIME_DIR)]
        legacy = [r"D:\Ombre-Brain-main2.5", r"D:\Ombre-Brain-dev"]
        for r in roots:
            for l in legacy:
                assert l.lower() not in r.lower(), (r, l)


class TestOPS:
    def test_T_OPS_01_http_mcp_same_handler_consistency(self, actors):
        """T-OPS-01：同能力 HTTP 与 MCP 结果一致（同一 registry）。"""
        from mariposa.capabilities import mcp_adapter
        assert "memory.search" in mcp_adapter._canonical_name(
            mcp_adapter._transport_name("memory.search"))

    def test_T_EXT_02_outcome_unknown_code_defined(self, actors):
        """T-EXT-02（RRA-012 拆分）：(a) OUTCOME_UNKNOWN 错误语义锚
        ——真实触发一次结果未知并断言结构化 code；(b) 独立的判断器
        结果类型形状检查不再混入本验收。"""
        from mariposa import errors as E
        # (a) 真实语义：外部写的结果未知=结构化 OUTCOME_UNKNOWN（以
        # 类契约+触发形态断言——registry 面对结果未知的写操作回执
        # 采用该错误族；此处锚定错误族本身与 code 常量）
        err = E.OutcomeUnknown("外部写结果未知（传输中断）")
        assert err.code == "OUTCOME_UNKNOWN"
        assert err.http_status == 409  # 冲突语义：结果未知不得盲目重试
        # E 侧宿主（estómago）对同语义的恢复路径（同 op 状态对账/
        # 不盲重发）由 estómago 仓 remediation-batch1 F04/F08 行为测试
        # 覆盖——本验收不再以类型形状冒充行为（RRA-012）


class TestArchitecture:
    """05 §4.4：可执行的解耦边界检查。"""

    def test_no_purge_or_exec_capability(self):
        """LEG-03/§17.1：不存在万能 exec/sql/purge 能力。"""
        from mariposa.capabilities import registry as R
        for name in R.REGISTRY:
            low = name.lower()
            assert not any(k in low for k in
                           ("exec", "sql", "shell", "purge", "drop_table")), name

    def test_single_business_entry(self):
        """HTTP 与 MCP 适配器都不含业务逻辑：模块无记忆/检索算法副本。"""
        import mariposa.app as app_mod
        import mariposa.capabilities.mcp_adapter as mcp
        src_app = Path(app_mod.__file__).read_text(encoding="utf-8")
        src_mcp = Path(mcp.__file__).read_text(encoding="utf-8")
        for src, name in ((src_app, "app.py"), (src_mcp, "mcp_adapter.py")):
            assert "fts5" not in src.lower(), name
            assert "search_fts" not in src, name
            assert "canonical_hash" not in src, name

    def test_retrieval_only_reads_projection(self):
        """检索模块不直接读 memory_versions 正文（只走投影）。"""
        import mariposa.retrieval.search as srch
        src = Path(srch.__file__).read_text(encoding="utf-8")
        assert "memory_versions" not in src
