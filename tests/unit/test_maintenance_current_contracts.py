"""维护页面和能力发现从现行事实源读取；合同导出不访问业务库。"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import runpy

from mariposa import db, schema
from mariposa.capabilities import registry, v1_compat
from mariposa.maintenance import service as maintenance
from tests.conftest import reset_all


def test_jobs_report_current_source_batches_and_historical_outbox():
    reset_all()
    with db.formal() as conn:
        for index, (kind, status) in enumerate([
                ("export_snapshot", "completed"),
                ("export_snapshot", "failed"), ("live_delta", "running")]):
            conn.execute(
                "INSERT INTO source_import_batches(batch_id, provider, status,"
                " original_filename, original_bytes, sha256, raw_path,"
                " parser_version, import_started_at, imported_by, kind)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (str(index), "claude", status, "synthetic.json", 0,
                 str(index), "synthetic", "test", "2026-10-15", "jiaming", kind))
        conn.execute("INSERT INTO import_jobs(id,source_channel,external_id,"
                     "status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     ("legacy", "raw", "old", "failed", "2026-09-21",
                      "2026-09-21"))
        conn.execute("INSERT INTO events_outbox VALUES(?,?,?,?,?)",
                     ("old-event", "test", "2026-09-21", "{}", 0))
    out = maintenance.jobs_status()
    assert out["import_jobs"] == {"completed": 1, "failed": 1, "running": 1}
    assert out["import_jobs_by_kind"]["live_delta"] == {"running": 1}
    assert out["import_jobs_source"] == "source_import_batches"
    assert out["outbox_pending"] == 1
    assert out["outbox_status"] == "historical_readonly"
    assert maintenance.outbox_status() == {
        "pending": 1, "total": 1, "status": "historical_readonly",
        "consumers_registered": False}
    assert maintenance.outbox_drain()["drained"] == 0


def test_status_uses_explicit_state_not_description(monkeypatch):
    template = registry.REGISTRY["time.now"]
    monkeypatch.setitem(registry.REGISTRY, "synthetic.blocked", replace(
        template, name="synthetic.blocked", status="blocked", description="plain"))
    monkeypatch.setitem(registry.REGISTRY, "synthetic.available", replace(
        template, name="synthetic.available", status="available",
        description="[reserved] 历史描述不应决定当前状态"))
    status = registry.REGISTRY["capabilities.status"].handler(None, {})
    assert "synthetic.blocked" in status["blocked"]
    assert "synthetic.available" not in status["reserved"]
    assert set(status["reserved"]) == {
        name for name, cap in registry.REGISTRY.items() if cap.status == "reserved"}


def test_five_compat_entries_preserve_handlers_and_repeat_safely():
    before = dict(registry.REGISTRY)
    assert v1_compat.register_v1_compat() == {"alias": 0, "thin": 0}
    assert registry.REGISTRY == before
    for alias, target in v1_compat._ALIASES.items():
        assert registry.REGISTRY[alias].handler is registry.REGISTRY[target].handler
        assert registry.REGISTRY[alias].status == registry.REGISTRY[target].status
    for name in ("plan.get", "plan.complete", "plan.cancel"):
        assert registry.REGISTRY[name].handler.__module__ == registry.__name__


def test_contract_export_does_not_migrate_or_connect(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("合同导出不能连接/迁移数据库")

    monkeypatch.setattr(schema, "migrate", forbidden)
    monkeypatch.setattr(db, "formal", forbidden)
    written = {}

    def capture(path, text, **kwargs):
        written[path.name] = json.loads(text)
        return len(text)

    monkeypatch.setattr(Path, "write_text", capture)
    root = Path(__file__).parents[2]
    runpy.run_path(str(root / "scripts/export_contracts.py"), run_name="__main__")
    exported = written["capabilities.v1.json"]["capabilities"]
    assert {cap["canonical_name"] for cap in exported} == set(registry.REGISTRY)
    for cap in exported:
        assert cap["status"] == registry.REGISTRY[cap["canonical_name"]].status
