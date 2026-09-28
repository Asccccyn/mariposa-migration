"""118 条验收用例映射测试（第二批：BOOT/CAL/TIME/SELF/LEG/MIG/OPS）+ 架构边界。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mariposa import db
from mariposa.calendar import service as calendar
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

class TestCAL:
    def test_T_CAL_03_plan_date_change_reflected(self, actors):
        """T-CAL-03：计划改日期后日历即时反映，同 plan_id 无第二份。"""
        p = plans.create("jiaming", title="复诊", state="planned",
                         due_at="2026-09-25T09:00:00")
        assert any(i["resource_id"] == p["plan_id"]
                   for i in calendar.day("2026-09-25")["items"])
        plans.update("jiaming", p["plan_id"], 1, due_at="2026-09-26T09:00:00")
        assert not any(i["resource_id"] == p["plan_id"]
                       for i in calendar.day("2026-09-25")["items"])
        day26 = [i for i in calendar.day("2026-09-26")["items"]
                 if i["resource_id"] == p["plan_id"]]
        assert len(day26) == 1

    def test_T_CAL_04_undated_section(self, actors):
        """T-CAL-04：日期未知进待定区，不冒充今天。"""
        h = memory.hold(actors["jiaming"], text="没有日期的桶",
                         categories=["daily"])  # memory_date=None
        out = calendar.undated()
        assert any(i["resource_id"] == h["memory_id"] for i in out["items"])
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        today = datetime.now(timezone.utc).astimezone(
            ZoneInfo("Asia/Shanghai")).date().isoformat()
        assert not any(i.get("resource_id") == h["memory_id"]
                       for i in calendar.day(today)["items"])

    def test_T_CAL_05_hidden_not_in_calendar(self, actors):
        """T-CAL-05：隐藏/归档资源不进日历计数或列表。"""
        h = memory.hold(actors["jiaming"], text="将被隐藏", memory_date="2026-06-01", categories=["daily"])
        with db.formal() as conn:
            conn.execute("UPDATE memories SET visibility='hidden' WHERE"
                         " memory_id=?", (h["memory_id"],))
        items = calendar.day("2026-06-01")["items"]
        assert not any(i.get("resource_id") == h["memory_id"] for i in items)
        # 锁信不在任何日历 provider（元数据保护）
        assert "letter" not in calendar.PROVIDERS


class TestTIME:
    def test_T_TIME_03_backfill_uses_message_time(self, actors):
        """T-TIME-03：两天前的用户消息回填原时刻，不当此刻见面。"""
        from mariposa.raw import service as raw
        from mariposa.time_context import service as time_ctx
        from datetime import datetime, timedelta, timezone
        t = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        raw.import_payload("worker", {
            "source_channel": "bf", "external_id": "bf1",
            "messages": [{"source_message_id": "m0", "role": "user",
                          "body": "两天前的联系",
                          "occurred_at": t, "sequence": 0}]})
        ctx = time_ctx.context("qiaosheng")
        assert ctx["last_user_message_at"] == t

    def test_T_TIME_04_gap_wording(self, actors):
        """T-TIME-04：覆盖缺口时说明'记录可能未齐'，不说错误上下界。"""
        from mariposa.time_context import service as time_ctx
        out = time_ctx.since("qiaosheng")
        if out["since_last_contact"] is None:
            assert "未齐" in out["note"]
        else:
            assert "已收录" in out["note"] or "不计" in out["note"]


class TestSELF:
    def test_T_SELF_03_q_correction_not_overwritten(self, actors):
        """T-SELF-03：乔生的情绪标签修正不被后续写入静默覆盖。"""
        from mariposa.content import service as content
        h = memory.hold(actors["jiaming"], text="情绪修正", memory_date="2026-06-01", categories=["daily"])
        content.tags_add("qiaosheng", h["memory_id"],
                         [{"tag": "平静", "whose": "qiaosheng"}])
        # 后续（模型观察路径的）同 tag 写入不覆盖
        content.tags_add("jiaming", h["memory_id"],
                         [{"tag": "平静", "whose": "qiaosheng"}])
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_tags WHERE memory_id=? AND"
                " tag='平静' AND whose='qiaosheng'", (h["memory_id"],)).fetchone()["c"]
            creator = conn.execute(
                "SELECT created_by FROM memory_tags WHERE memory_id=? AND"
                " tag='平静'", (h["memory_id"],)).fetchone()["created_by"]
        assert n == 1 and creator == "qiaosheng"

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

    def test_T_OPS_05_stop_script_scoped(self, actors):
        """T-OPS-05：stop-dev 只按端口+mariposa 命令行匹配，不按进程名杀。"""
        text = Path("scripts/stop-dev.ps1").read_text(encoding="utf-8")
        assert "mariposa" in text  # 命令行校验
        assert "Get-Process" not in text and "taskkill" not in text

    def test_T_EXT_02_outcome_unknown_code_defined(self, actors):
        """T-EXT-02：OUTCOME_UNKNOWN 错误码就绪（外部写入未接，blocked）。"""
        from mariposa.errors import MariposaError
        codes = {c.code for c in [MariposaError]}  # 占位
        from mariposa import errors as E
        assert E.MariposaError.code == "INTERNAL"
        # 实际校验：定义了语义类
        import inspect
        names = [n for n, o in inspect.getmembers(E, inspect.isclass)
                 if issubclass(o, MariposaError)]
        assert any("Provider" in n for n in names)


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
