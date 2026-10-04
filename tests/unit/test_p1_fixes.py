"""P1 修复回归：B06 测试根保险丝 / B08 扫描防饥饿+defer+撤回 / B02 崩溃窗口。

对应 v2.0.1 规格验收：
- V2-OPS-07（测试根目录保险丝：继承业务根时拒绝测试）
- V2-RET-09（到期扫描分页无饥饿）
- V2-OPS-05（崩溃后幂等恢复：外部不明结果进入对账而非盲目重发）
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, OutcomeUnknown
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import TOKENS, _test_root_allowed, reset_all

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }


class TestB06TestRootFuse:
    """V2-OPS-07：MARIPOSA_ROOT 指向业务根时 fail closed。"""

    def test_business_root_rejected(self):
        ok, _ = _test_root_allowed(str(REPO_ROOT))
        assert not ok, "仓库根（业务根）必须被拒绝"

    def test_runtime_root_rejected(self):
        ok, _ = _test_root_allowed(str(REPO_ROOT / "runtime"))
        assert not ok

    def test_formal_db_dir_rejected(self):
        ok, _ = _test_root_allowed(str(REPO_ROOT / "runtime" / "formal"))
        assert not ok

    def test_verification_evidence_dir_allowed(self):
        ok, _ = _test_root_allowed(
            str(REPO_ROOT / "runtime" / "verification" / "v2-x" / "isolated-root"))
        assert ok

    def test_pytest_basetemp_allowed(self):
        ok, _ = _test_root_allowed(str(REPO_ROOT / ".pytest_tmp" / "case1"))
        assert ok

    def test_prefix_spoof_rejected(self):
        # 字符串前缀伪装：D:\mariposa-evil 不是允许根
        ok, _ = _test_root_allowed(str(REPO_ROOT) + "-evil")
        assert not ok

    def test_subprocess_pytest_refuses_business_root(self):
        env_root = str(REPO_ROOT)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "tests/unit/test_content.py"],
            capture_output=True, cwd=str(REPO_ROOT),
            env={"MARIPOSA_ROOT": env_root, "PYTHONIOENCODING": "utf-8",
                 "SYSTEMROOT": "C:\\Windows", "PATH": "",
                 "USERPROFILE": "C:\\Users\\Public"},
        )
        out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
        assert r.returncode != 0
        assert "fail closed" in out


class TestB02CrashWindow:
    """V2-OPS-05：running 残留不盲重放，显式对账后才能重试。"""

    ARGS = {"text": "崩溃后重试", "memory_date": "2026-01-01",
            "date_confidence": "exact", "raw_pending": False,
            "categories": ["daily"]}

    def _seed_running(self, key, age_seconds):
        # 审计 2026-10-03：RA-004 后 registry 读 _transport_key(key)
        # （t: 前缀）——fixture 必须种现行键空间，裸键根本不会被命中
        old = (datetime.now(timezone.utc)
               - timedelta(seconds=age_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO idempotency_records(principal_id, capability,"
                " idempotency_key, payload_hash, status, result_ref, created_at)"
                " VALUES('jiaming','memory.hold',?,?, 'running', NULL, ?)",
                (registry._transport_key(key),
                 registry._payload_hash(self.ARGS), old))

    def test_stale_running_raises_outcome_unknown_not_replay(self, actors):
        self._seed_running("crash-key", 120)
        with pytest.raises(OutcomeUnknown):
            registry.invoke(actors["jiaming"], "memory.hold", self.ARGS,
                            "crash-key")
        # 副作用未盲目执行
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE memory_date='2026-01-01'"
            ).fetchone()["c"]
        assert n == 0

    def test_fresh_running_still_busy(self, actors):
        self._seed_running("fresh-key", 1)
        from mariposa.capabilities.registry import Busy
        with pytest.raises(Busy):
            registry.invoke(actors["jiaming"], "memory.hold", self.ARGS,
                            "fresh-key")

    def test_same_key_different_payload_conflicts(self, actors):
        self._seed_running("mix-key", 1)
        with pytest.raises(Exception) as e:
            registry.invoke(actors["jiaming"], "memory.hold",
                            {**self.ARGS, "text": "不同内容"}, "mix-key")
        assert e.value.code == "IDEMPOTENCY_CONFLICT"

    def test_reconcile_clears_guard_then_retry_executes(self, actors):
        self._seed_running("crash-key", 120)
        from mariposa.maintenance import service as maintenance
        out = maintenance.idempotency_reconcile(
            "qiaosheng", "jiaming", "memory.hold", "crash-key")
        assert out["reconciled"] is True
        got = registry.invoke(actors["jiaming"], "memory.hold", self.ARGS,
                              "crash-key")
        assert got["data"]["memory_id"]
        # 再次对账：已 completed，无 running 可对账
        out2 = maintenance.idempotency_reconcile(
            "qiaosheng", "jiaming", "memory.hold", "crash-key")
        assert out2["reconciled"] is False
