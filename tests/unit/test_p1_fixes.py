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
from mariposa.workspace import service as workspace
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


class TestB08ScanStarvation:
    """V2-RET-09：前页全是跳过项时，后续到期项仍能被发现。"""

    def _hold(self, actors, text, date):
        return memory.hold(actors["jiaming"], text=text, why_remember=None,
                           memory_date=date, date_confidence="exact")

    def test_no_starvation_when_earliest_rows_skipped(self, actors):
        # 25 条 date_unknown（NULL 日期在 ASC 排序中排最前、全部被跳过）
        # + 1 条真正到期：旧实现 LIMIT 20 全被跳过 → 到期项永远扫不到；
        # 新实现游标推进，跳过项不阻塞后续发现。
        for i in range(25):
            memory.hold(actors["jiaming"], text=f"无日期琐事{i}",
                        memory_date=None, date_confidence="unknown")
        due = self._hold(actors, "真正到期的旧记忆", "2020-01-01")["memory_id"]
        out = workspace.scan_candidates(actors["worker"], min_idle_days=0,
                                        limit=5)
        assert any(c["target_memory_id"] == due for c in out["created"]), out
        assert out["skipped"], "无日期项应被如实跳过"

    def test_cursor_is_stable_and_reported(self, actors):
        self._hold(actors, "旧事1", "2020-01-01")
        self._hold(actors, "旧事2", "2019-01-01")
        out = workspace.scan_candidates(actors["worker"], min_idle_days=0, limit=1)
        assert out["exhausted"] is False
        assert out["next_cursor"] and len(out["next_cursor"]) == 2
        out2 = workspace.scan_candidates(actors["worker"], min_idle_days=0,
                                         limit=5, cursor=tuple(out["next_cursor"]))
        assert out2["exhausted"] is True
        assert out2["next_cursor"] is None

    def test_defer_updates_work_item_state(self, actors):
        h = self._hold(actors, "待挂起的事", "2020-01-01")
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"]
                    if p["target_memory_id"] == h["memory_id"])
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     "摘要。", "r")
        sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
        out = workspace.decide(actors["qiaosheng"], sub["proposal_id"],
                               sub["revision"], sub["proposal_hash"],
                               sub["base_memory_version"], "defer")
        assert out["decision"] == "defer"
        items = {i["proposal_id"]: i for i in
                 workspace.list_items(states=["deferred"])}
        assert sub["proposal_id"] in items, "defer 必须实际落到 deferred 状态"
        # defer 后桶挂起：扫描跳过（open_item），不再产生新草稿
        scan2 = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        assert all(c["target_memory_id"] != h["memory_id"] for c in scan2["created"])

    def test_withdraw_capability_reachable_by_worker(self, actors):
        h = self._hold(actors, "将被撤回的事", "2020-01-01")
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"]
                    if p["target_memory_id"] == h["memory_id"])
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     "摘要。", "r")
        sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
        out = registry.invoke(actors["worker"], "workspace.proposals.withdraw",
                              {"proposal_id": sub["proposal_id"]}, None)
        assert out["data"]["state"] == "withdrawn"
        with pytest.raises(Forbidden):
            workspace.withdraw(actors["worker"], sub["proposal_id"])  # 已终局

    def test_worker_cannot_withdraw_others_proposal(self, actors):
        h = self._hold(actors, "他人提案", "2020-01-01")
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"]
                    if p["target_memory_id"] == h["memory_id"])
        rev = workspace.revise_draft(actors["jiaming"], prop["proposal_id"],
                                     "摘要。", "r")
        sub = workspace.submit(actors["jiaming"], prop["proposal_id"], rev["revision"])
        with pytest.raises(Forbidden):
            workspace.withdraw(actors["worker"], sub["proposal_id"])


class TestB02CrashWindow:
    """V2-OPS-05：running 残留不盲重放，显式对账后才能重试。"""

    ARGS = {"text": "崩溃后重试", "memory_date": "2026-01-01",
            "date_confidence": "exact", "raw_pending": False}

    def _seed_running(self, key, age_seconds):
        old = (datetime.now(timezone.utc)
               - timedelta(seconds=age_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO idempotency_records(principal_id, capability,"
                " idempotency_key, payload_hash, status, result_ref, created_at)"
                " VALUES('jiaming','memory.hold',?,?, 'running', NULL, ?)",
                (key, registry._payload_hash(self.ARGS), old))

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
