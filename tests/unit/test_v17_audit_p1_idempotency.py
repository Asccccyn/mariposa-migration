"""v1.7 审计整改第一阶段回归：F07/F10/F26 幂等与 stale 重放。

覆盖审计要求：
- 同 idempotency key 两并发 mutation 只执行一次副作用（真实线程并发）
- failed operation 允许安全 retry
- in-progress operation 行为（结构化冲突，不双执行）
- WIDE 操作后 memory 转 CORE，再 replay 不泄露 title 命中正文
- memory revision 前进后 replay 剔除旧版本候选
- session 过期后 replay 拒绝（OPERATION_REPLAY_STALE）
- 同 key 异 payload 结构化冲突
- I current 读取带 Idempotency-Key 不缓存响应（F10：修订后同 key 读到新版）
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import (IdempotencyConflict, StaleOperation,
                             VersionConflict)
from mariposa.identity import service as identity
from mariposa.identity_i import service as i_service
from mariposa.memory import extras as memory_extras
from mariposa.memory import service as memory
from mariposa.recall import store
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold_v2(actors, text, title, date="2026-09-20", mid_label=None):
    return memory.hold(
        actors["jiaming"], text=text, memory_date=date,
        date_confidence="exact", original_title=title,
        categories=["sweet"], mood=None, our_words=None,
        creation_mode="contemporaneous", raw_pending=False)


def start_op(actors, terms, op, session_id="new"):
    res = registry.invoke(actors["jiaming"], "memory.recall.start",
                          {"query_plan": {
                              "original_request": "查询",
                              "channels": ["event"],
                              "lexical_terms": list(terms)},
                           "operation_id": op}, None)
    inner = res["data"]  # claim_operation 包装层
    return inner["data"], inner.get("idempotent_replay", False)



def make_builder(key, payload_hash, produce):
    """规范 operation builder：副作用与 operation 记录同事务。"""
    from mariposa import db as _db

    def builder():
        result = produce()
        with _db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                store.record_operation_row(conn, "jiaming", key,
                                           payload_hash, result)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return result
    return builder

class TestF26CommitAtEnd:
    """commit-at-end 模型（幂等与崩溃恢复整改后语义）。

    - 不存在中间态：计算失败/崩溃后数据库零痕迹，重试从头计算
    - 同 key 并发：进程内锁避免重复昂贵计算，数据库只留一份结果
    - 同 key 异 payload：结构化 IDEMPOTENCY_CONFLICT
    """

    def test_concurrent_same_key_single_execution(self, actors):
        """真实并发：同 key 两线程，builder 只执行一次，一方重放。"""
        gate = threading.Barrier(2, timeout=10)
        calls = []
        lock = threading.Lock()

        def produce():
            with lock:
                calls.append(threading.get_ident())
            return {"value": "done"}

        def run(_i):
            gate.wait()
            return store.run_operation(
                "jiaming", "op-concurrent-2", builder, payload_hash="h1")

        builder = make_builder("op-concurrent-2", "h1", produce)

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(run, 0)
            f2 = ex.submit(run, 1)
            r1, r2 = f1.result(timeout=20), f2.result(timeout=20)
        assert len(calls) == 1, "并发同 key 产生了两次计算/副作用"
        values = {r1["data"]["value"], r2["data"]["value"]}
        assert values == {"done"}
        replays = [bool(r1.get("idempotent_replay")),
                   bool(r2.get("idempotent_replay"))]
        assert replays.count(True) == 1, "应有且仅有一方走重放路径"

    def test_failed_computation_leaves_no_trace_and_retries(self, actors):
        """计算失败 → 无 operation 行（无 failed/中间态），重试重新执行。"""
        calls = []

        def failing():
            calls.append(1)
            raise RuntimeError("boom")  # 计算阶段失败：无事务无副作用

        with pytest.raises(RuntimeError):
            store.run_operation("jiaming", "op-retry-2", failing,
                                payload_hash="h1")
        assert len(calls) == 1
        # 失败零痕迹：没有任何 operation 记录残留
        assert store.read_operation("jiaming", "op-retry-2") is None
        # 同 key 重试：从头重新计算并成功
        out = store.run_operation(
            "jiaming", "op-retry-2",
            make_builder("op-retry-2", "h1", lambda: {"ok": True}),
            payload_hash="h1")
        assert out["data"] == {"ok": True}
        assert len(calls) == 1  # 原失败不重复计

    def test_same_key_different_payload_conflict(self, actors):
        out = store.run_operation(
            "jiaming", "op-hash-2",
            make_builder("op-hash-2", "h1", lambda: {"a": 1}),
            payload_hash="h1")
        assert out["data"] == {"a": 1}
        with pytest.raises(IdempotencyConflict):
            store.run_operation(
                "jiaming", "op-hash-2", lambda: {"a": 2},
                payload_hash="h2")

    def test_sequential_replay_returns_saved_result(self, actors):
        """顺序重试（RUNTIME-02 原语义保持）：同 key 同 payload 重放。"""
        out1 = store.run_operation(
            "jiaming", "op-seq-2",
            make_builder("op-seq-2", "h1", lambda: {"n": 1}),
            payload_hash="h1")
        out2 = store.run_operation(
            "jiaming", "op-seq-2",
            make_builder("op-seq-2", "h1", lambda: {"n": 2}),
            payload_hash="h1")
        assert out1["data"] == {"n": 1}
        assert out2["idempotent_replay"] is True
        assert out2["data"] == {"n": 1}


class TestF07StaleReplay:
    def test_wide_to_core_replay_drops_title_only_hits(self, actors):
        """v1.7 证据场景：title 命中后推进 CORE，同 operation_id 重放
        不得再返回 title 候选。"""
        out = hold_v2(actors, "正文里有独特湖泊甲", "湖泊甲的标题",
                      date="2026-09-25")
        mid = out["memory_id"]
        packet, _ = start_op(actors, ["湖泊甲"], "op-wide-1")
        title_hits = [c for c in packet["candidates"]
                      if "original_title" in (c.get("matched_fields") or [])]
        assert title_hits, "前置：WIDE 下应命中 title"
        # 时间推进到 CORE（2026-01-01 距 2026-09 远超 sweet H=30 的两倍）
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET held_at='2026-01-01T00:00:00+00:00'"
                " WHERE memory_id=?", (mid,))
        replay, replayed = start_op(actors, ["湖泊甲"], "op-wide-1")
        assert replayed is True
        for c in replay["candidates"]:
            assert "original_title" not in (c.get("matched_fields") or []), \
                "CORE 阶段重放仍暴露 title 命中"

    def test_version_advance_replay_drops_stale_body(self, actors):
        out = hold_v2(actors, "正文旧版有独特风铃草", "风铃草的晚上",
                      date="2026-09-25")
        mid = out["memory_id"]
        packet, _ = start_op(actors, ["风铃草"], "op-ver-1")
        assert any(c.get("memory_id") == mid for c in packet["candidates"])
        memory_extras.update_text(
            actors["jiaming"].principal_id, mid, expected_version=1,
            text="正文新版改成了铃兰")
        replay, replayed = start_op(actors, ["风铃草"], "op-ver-1")
        assert replayed is True
        assert all(c.get("memory_id") != mid
                   for c in replay["candidates"]), \
            "revision 前进后重放仍返回旧版本候选"

    def test_expired_session_replay_rejected(self, actors):
        hold_v2(actors, "过期场景正文有独特蒲公英", "蒲公英",
                date="2026-09-25")
        packet, _ = start_op(actors, ["蒲公英"], "op-exp-1")
        sid = packet["recall_session_id"]
        with db.recall_runtime() as conn:
            conn.execute(
                "UPDATE recall_sessions SET status='EXPIRED'"
                " WHERE session_id=?", (sid,))
        with pytest.raises(StaleOperation):
            start_op(actors, ["蒲公英"], "op-exp-1")

    def test_archived_memory_replay_dropped(self, actors):
        out = hold_v2(actors, "归档场景正文有独特石楠花", "石楠花",
                      date="2026-09-25")
        mid = out["memory_id"]
        packet, _ = start_op(actors, ["石楠花"], "op-arch-1")
        assert any(c.get("memory_id") == mid for c in packet["candidates"])
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET visibility='hidden'"
                " WHERE memory_id=?", (mid,))
        replay, _ = start_op(actors, ["石楠花"], "op-arch-1")
        assert all(c.get("memory_id") != mid
                   for c in replay["candidates"]), \
            "归档后的 memory 经旧 operation 重放仍可见"


class TestF10ReadNoResponseCache:
    def test_i_item_get_key_not_cached_across_revisions(self, actors):
        reg = registry.REGISTRY
        created = registry.invoke(
            actors["jiaming"], "i.item.create",
            {"content": "我是第一版内容"}, None)
        item_id = created["data"]["item_id"]
        first = registry.invoke(
            actors["jiaming"], "i.item.get", {"item_id": item_id},
            "idem-key-i-get-1")
        assert "第一版" in first["data"]["content"]
        # 修订（新 revision）
        registry.invoke(
            actors["jiaming"], "i.item.revise",
            {"item_id": item_id, "content": "我是第二版内容",
             "expected_revision": 1}, None)
        # 同 Idempotency-Key 再读：必须读到当前 revision，不重放旧缓存
        again = registry.invoke(
            actors["jiaming"], "i.item.get", {"item_id": item_id},
            "idem-key-i-get-1")
        assert "第二版" in again["data"]["content"], \
            "F10：读取幂等缓存把旧 revision 正文当作 current 返回"
        assert again.get("idempotent_replay") is not True

    def test_write_capability_still_idempotent(self, actors):
        """写能力幂等行为保持：同 key 同 payload 重放不二次执行。"""
        hold_v2(actors, "写幂等场景正文有独特紫罗兰", "紫罗兰",
                date="2026-09-25")
        # memory.update 是写能力：同 key 同 payload 重放不二次执行
        out = hold_v2(actors, "写幂等第二条有独特鸢尾", "鸢尾",
                      date="2026-09-25")
        args = {"memory_id": out["memory_id"], "expected_version": 1,
                "why_remember": "同 key 理由"}
        r1 = registry.invoke(actors["jiaming"], "memory.update", args,
                             "idem-key-write-1")
        assert r1["data"]["version"] == 2
        r2 = registry.invoke(actors["jiaming"], "memory.update", args,
                             "idem-key-write-1")
        assert r2.get("idempotent_replay") is True
        assert r2["data"]["version"] == 2
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_versions WHERE"
                " memory_id=?", (out["memory_id"],)).fetchone()["c"]
        assert n == 2, "写幂等重放产生了第二个版本"
