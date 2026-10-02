"""Registry/Memory/Plan 域审计修复回归（2026-10-02 基线审计 P1 批）。

- CB-016：memory.open 幂等缓存重放前按当前资源重验（审计反例
  open_replay_after_revision / open_replay_after_delete——更新或物理
  删除后同 key 仍重发旧正文与已删票据）。
- CB-017：兼容 memory.search 的 dense 命中不得附送未判断正文
  （dense_search_judge_disabled——judge 关闭时 whitelist_body 全文
  仍出站，绕过 S10 硬门）。
- CB-018：plan 并发版本校验在写锁内裁决（plan_race——双线程同
  expected_version 都过预检，输家 IntegrityError 500 + 幂等 running
  残留）。
"""
from __future__ import annotations

import threading

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, StaleOperation
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="cb",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


# ---------------------------------------------------------------- CB-016

class TestOpenReplayRevalidation:

    def test_replay_after_update_rejected(self, actors):
        """审计反例：open(v1) → update(v2) → 同 key 重试——不得重发
        旧正文/失效票据，返回结构化 stale。"""
        mid = _hold(actors, "旧版本正文")["memory_id"]
        first = registry.invoke(actors["jiaming"], "memory.open",
                                {"memory_id": mid}, "open-k1")
        assert first["data"]["text"] == "旧版本正文"
        registry.invoke(actors["jiaming"], "memory.update",
                        {"memory_id": mid, "text": "新版本正文",
                         "expected_version": 1,
                         "operation_id": "upd-k1"}, None)
        with pytest.raises(StaleOperation):
            registry.invoke(actors["jiaming"], "memory.open",
                            {"memory_id": mid}, "open-k1")

    def test_replay_after_physical_delete_rejected(self, actors):
        """审计反例：物理删除后同 key 仍 ok=true 重发旧正文与已删
        receipt——现在结构化拒绝。"""
        mid = _hold(actors, "待删除正文")["memory_id"]
        registry.invoke(actors["jiaming"], "memory.open",
                        {"memory_id": mid}, "open-k2")
        registry.invoke(actors["jiaming"], "memory.delete",
                        {"memory_id": mid, "operation_id": "del-k2"}, None)
        with pytest.raises(StaleOperation):
            registry.invoke(actors["jiaming"], "memory.open",
                            {"memory_id": mid}, "open-k2")

    def test_fresh_open_after_stale_replay_still_works(self, actors):
        """拒绝重放不影响新 key 的正常 open（签发新票据）。"""
        mid = _hold(actors, "正常正文")["memory_id"]
        registry.invoke(actors["jiaming"], "memory.open",
                        {"memory_id": mid}, "open-k3")
        registry.invoke(actors["jiaming"], "memory.update",
                        {"memory_id": mid, "text": "更新后正文",
                         "expected_version": 1,
                         "operation_id": "upd-k3"}, None)
        again = registry.invoke(actors["jiaming"], "memory.open",
                                {"memory_id": mid}, "open-k3b")
        assert again["data"]["text"] == "更新后正文"


# ---------------------------------------------------------------- CB-017

class TestSearchDenseNoUnjudgedBody:

    def test_dense_hits_carry_metadata_only(self, actors, monkeypatch):
        """兼容检索的语义命中只带未交付候选元数据——正文载体
        whitelist_body 不得出站（取正文走 memory.open / Recall 的
        judge 硬门链路）。"""
        from mariposa.retrieval import search as search_mod
        from mariposa.retrieval import semantic

        mid = _hold(actors, "语义命中正文")["memory_id"]

        def fake_semantic_search(conn, query, limit, **kw):
            return [{
                "memory_id": mid, "matched_by": "semantic",
                "projection_kind": "active", "score": 0.99,
                "whitelist_body": "未判断的完整正文不应出站",
                "content_version": "1",
            }]

        monkeypatch.setattr(semantic, "semantic_search",
                            fake_semantic_search)
        from mariposa import config as cfg
        old = cfg.SEMANTIC_PROVIDER
        cfg.SEMANTIC_PROVIDER = "local_bge_zh"  # 本地 provider 形态
        try:
            with db.formal() as conn:
                out = search_mod.search(conn, "完全不匹配的关键词xyz")
        finally:
            cfg.SEMANTIC_PROVIDER = old
        dense = [h for h in out["hits"] if h.get("matched_by") == "semantic"]
        assert dense, "前置：假语义命中进入结果"
        assert all("whitelist_body" not in h for h in dense), \
            "兼容 dense 命中不得附送未判断正文"
        assert all("text" not in h for h in dense)


# ---------------------------------------------------------------- CB-018

class TestPlanConcurrentVersion:

    def test_concurrent_same_version_single_structured_winner(self, actors):
        """审计反例 plan_race：两线程同 expected_version=1——写锁内
        裁决，恰一成功，输家结构化 VERSION_CONFLICT（不再 500），
        双方幂等记录都非 running 残留。"""
        from mariposa.plans import service as plans
        plan = plans.create("jiaming", "并发计划", state="active")
        pid = plan["plan_id"]

        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def run(tag: str, title: str):
            # RA-001 后 schema 与 handler 均为顶层平铺字段
            barrier.wait()
            try:
                out = registry.invoke(
                    actors["jiaming"], "plan.update",
                    {"plan_id": pid, "expected_version": 1,
                     "title": title, "operation_id": f"pu-{tag}"},
                    f"transport-{tag}")
                results[tag] = ("ok", out["data"]["version"])
            except Forbidden as e:
                results[tag] = ("conflict", e.code)
            except Exception as e:  # noqa: BLE001
                results[tag] = ("unstructured", repr(e))

        t1 = threading.Thread(target=run, args=("a", "甲的标题"))
        t2 = threading.Thread(target=run, args=("b", "乙的标题"))
        t1.start(); t2.start(); t1.join(30); t2.join(30)

        outcomes = sorted(results.values())
        assert outcomes[0][0] == "conflict", \
            f"输家必须是结构化 VERSION_CONFLICT：{results}"
        assert outcomes[0][1] == "VERSION_CONFLICT"
        assert outcomes[1][0] == "ok", f"恰一赢家：{results}"

        with db.formal() as conn:
            v = conn.execute(
                "SELECT current_version_no FROM plans WHERE id=?",
                (pid,)).fetchone()["current_version_no"]
            running = conn.execute(
                "SELECT COUNT(*) c FROM idempotency_records WHERE"
                " capability='plan.update' AND status='running'"
            ).fetchone()["c"]
        assert v == 2, "版本只前进一次"
        assert running == 0, "败者不得遗留 running 幂等记录"
