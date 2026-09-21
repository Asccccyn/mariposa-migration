"""安全（§18）与并发/恢复（§23.1 Integration）。"""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from mariposa import db
from mariposa.app import app
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.maintenance import service as maintenance
from mariposa.media import service as media
from mariposa.memory import service as memory
from mariposa.migration import apply_from_report, dry_run
from mariposa.workspace import service as workspace
from tests.conftest import TOKENS, reset_all


@pytest.fixture()
def c(actors):
    with TestClient(app) as client:
        yield client


def auth(pid):
    return {"Authorization": f"Bearer {TOKENS[pid]}"}


class TestSecurity:
    def test_xss_payload_is_data_not_html(self, c):
        """存储型内容按 JSON 返回，Content-Type 不触发 HTML 解析。"""
        payload = {"arguments": {"text": "<img src=x onerror=alert(1)>药水",
                                 "memory_date": "2026-06-01"}}
        r = c.post("/api/capability/memory.hold", json=payload,
                   headers=auth("jiaming"))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        mid = r.json()["data"]["memory_id"]
        r2 = c.post("/api/capability/memory.get",
                    json={"arguments": {"memory_id": mid}}, headers=auth("qiaosheng"))
        assert "<img" in r2.json()["data"]["text"]  # 原样存储；渲染层转义
        assert r2.headers["content-type"].startswith("application/json")

    def test_media_path_traversal_inert(self, c):
        r = c.get("/api/media/object/..%2F..%2Fwindows%2Fwin.ini",
                  headers=auth("qiaosheng"))
        assert r.status_code == 404  # hash 查库不存在即 404，无文件拼接

    def test_media_endpoints_require_auth(self, c):
        r = c.put("/api/media/stage/anytoken", content=b"x")
        assert r.status_code == 401
        r2 = c.get("/api/media/object/abc")
        assert r2.status_code == 401

    def test_stage_size_mismatch(self, c):
        prep = c.post("/api/capability/media.upload.prepare",
                      json={"arguments": {"mime": "image/png", "size": 10}},
                      headers=auth("qiaosheng")).json()["data"]
        r = c.put(f"/api/media/stage/{prep['upload_token']}", content=b"12345",
                  headers=auth("qiaosheng"))
        assert r.status_code == 403  # 字节数不符被拒（FORBIDDEN）

    def test_cross_principal_idempotency_isolated(self, actors):
        """幂等键按 (principal, capability, key) 隔离：不同主体同 key 各自生效。"""
        args = {"text": "幂等隔离测试", "memory_date": "2026-06-01"}
        a = registry.invoke(actors["jiaming"], "memory.hold", args, "same-key")
        b = registry.invoke(actors["qiaosheng"], "memory.hold", args, "same-key")
        ida, idb = a["data"]["memory_id"], b["data"]["memory_id"]
        assert ida != idb  # 两个独立桶，不互相重放


class TestConcurrency:
    def _submitted(self, actors, text):
        hold = memory.hold(actors["jiaming"], text=text, memory_date="2026-06-01")
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"]
                    if p["target_memory_id"] == hold["memory_id"])
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     f"{text}摘要", "压缩")
        sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
        return hold, sub

    def test_concurrent_decide_exactly_one_wins(self, actors):
        hold, sub = self._submitted(actors, "并发审批测试")
        args = (sub["proposal_id"], sub["revision"], sub["proposal_hash"],
                sub["base_memory_version"], "approve")

        def decide_as(p):
            try:
                workspace.decide(p, proposal_id=args[0], proposal_revision=args[1],
                                 proposal_hash=args[2],
                                 expected_memory_version=args[3],
                                 decision=args[4])
                return "ok"
            except Exception as e:
                return getattr(e, "code", "ERROR")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(decide_as, [actors["qiaosheng"]] * 2 +
                                    [actors["jiaming"]] * 2))
        assert results.count("ok") == 1, results
        with db.formal() as conn:
            versions = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_versions WHERE memory_id=?",
                (hold["memory_id"],)).fetchone()["c"]
        assert versions == 2  # 只产生一个压缩版本

    def test_workspace_reconcile_after_interrupt(self, actors):
        """模拟审批事务提交后、工作区回填前崩溃：对账修复。"""
        hold, sub = self._submitted(actors, "对账测试")
        workspace.decide(actors["qiaosheng"], proposal_id=sub["proposal_id"],
                         proposal_revision=sub["revision"],
                         proposal_hash=sub["proposal_hash"],
                         expected_memory_version=sub["base_memory_version"],
                         decision="approve")
        # 人为把工作区状态打回 submitted（模拟回填丢失）
        with db.workspace() as wconn:
            wconn.execute("UPDATE work_items SET state='submitted' WHERE item_id=?",
                          (sub["proposal_id"],))
        out = maintenance.reconcile_workspace()
        assert out["fixed"] >= 1
        items = {i["proposal_id"]: i for i in workspace.list_items()}
        assert items[sub["proposal_id"]]["state"] == "approved_and_applied"
        # 幂等：再跑不重复修
        assert maintenance.reconcile_workspace()["fixed"] == 0


class TestMigrationApplyDrill:
    def test_apply_and_verify(self, actors, tmp_path):
        from pathlib import Path
        fixtures = Path(__file__).parents[1] / "fixtures" / "legacy_bucket_sample"
        report_path = tmp_path / "dry.json"
        dry_run(str(fixtures), str(report_path))
        out = apply_from_report(str(report_path))
        assert out["ok"] is True
        assert out["applied"] == 3 and out["verified"] == 3
        # 核对：置顶保留、锁信锁参数保留（合成正文未进任何索引/日志）
        with db.formal() as conn:
            pinned = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE pinned=1").fetchone()["c"]
            locked = conn.execute(
                "SELECT COUNT(*) AS c FROM letters WHERE lock_type='timed'"
            ).fetchone()["c"]
        assert pinned >= 1 and locked >= 1

    def test_apply_rejects_big_report(self, actors, tmp_path):
        report = {"fixture_dir": str(tmp_path),
                  "entries": [{"legacy_id": f"x{i}.md"} for i in range(60)]}
        p = tmp_path / "big.json"
        p.write_text(json.dumps(report), encoding="utf-8")
        out = apply_from_report(str(p))
        assert out["ok"] is False
