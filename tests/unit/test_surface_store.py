"""surface 运行库（迁移 16）与 store 合同：幂等/快照/回执/台账/清理。"""
from __future__ import annotations

import json

import pytest

from mariposa import db, schema
from mariposa.errors import IdempotencyConflict
from mariposa.surfacing import store
from tests.conftest import reset_all


@pytest.fixture(autouse=True)
def _env():
    reset_all()


class TestMigrations:
    def test_runtime_migration_16_tables_exist(self):
        with db.recall_runtime() as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("surface_operations", "surface_runs",
                  "surface_validation_receipts",
                  "surface_detail_snapshots", "surface_judge_ledger"):
            assert t in names, t

    def test_migrate_runtime_idempotent(self):
        schema.migrate_runtime()
        schema.migrate_runtime()


class TestOperationIdempotency:
    def test_claim_then_complete_then_replay(self):
        assert store.claim_operation("p1", "op", "h1") is None
        store.complete_operation("p1", "op", {"status": "prepared"})
        replay = store.claim_operation("p1", "op", "h1")
        assert replay is not None
        assert replay["result"]["status"] == "prepared"

    def test_same_key_different_payload_conflicts(self):
        store.claim_operation("p1", "op", "h1")
        store.complete_operation("p1", "op", {"a": 1})
        with pytest.raises(IdempotencyConflict):
            store.claim_operation("p1", "op", "h2")

    def test_unknown_preserved_not_erased(self):
        store.claim_operation("p1", "op", "h1")
        store.mark_unknown("p1", "op")
        replay = store.claim_operation("p1", "op", "h1")
        assert replay["status"] == "unknown"   # 不假装未执行


class TestRunsAndReceipts:
    def test_run_roundtrip(self):
        run_id = store.new_surface_run(
            "op", "scope", "prepared", {"winners": [{"a": 1}]},
            {"queries": 2}, 1, "surface_prepare_v1", None)
        run = store.load_surface_run(run_id)
        assert run["snapshot"]["winners"] == [{"a": 1}]
        assert run["status"] == "prepared"

    def test_receipt_roundtrip_and_consume(self):
        rid = store.save_validation_receipt(
            "run1", "scope", "tm", 1, "w1", "hash",
            [{"detail_ref": "d1", "memory_id": "m"}], 1, "issued", 60)
        receipt = store.load_validation_receipt(rid)
        assert receipt["items"][0]["detail_ref"] == "d1"
        store.consume_validation_receipt(rid, "consumed")
        assert store.load_validation_receipt(rid)["status"] == "consumed"

    def test_detail_snapshot_roundtrip(self):
        sid = store.save_detail_snapshot(
            "dref", "m1", "3", {"memory": "3|t"}, "scope", "p", "2",
            True, [{"sections": {"memory": {}}}], {"memory": "complete"},
            False)
        snap = store.load_detail_snapshot_by_ref("dref")
        assert snap["snapshot_id"] == sid
        assert snap["changed_since_surface"] is True
        assert snap["complete"] is False

    def test_judge_ledger_usage_today(self):
        store.record_judge_requests("op", "prepare", "typesafe_jev",
                                    3, None, "completed")
        usage = store.judge_usage_today()
        assert usage["requests"] == 3
        assert usage["est_cost_usd"] is None


class TestPurge:
    def test_purge_removes_expired(self):
        import sqlite3
        run_id = store.new_surface_run(
            "op", "scope", "prepared", {}, {}, 1, "p", None)
        # 直接把过期时间改到过去
        with db.recall_runtime() as conn:
            conn.execute(
                "UPDATE surface_runs SET expires_at='2000-01-01T00:00:00'"
                " WHERE surface_run_id=?", (run_id,))
        assert store.purge_expired() >= 1
        assert store.load_surface_run(run_id) is None
