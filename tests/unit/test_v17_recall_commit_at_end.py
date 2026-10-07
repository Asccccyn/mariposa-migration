"""Recall 幂等与崩溃恢复整改回归（commit-at-end，2026-09-29）。

对应执行方案 §14 的 A-I：
A start 检索失败 → 数据库零痕迹，重试可成功
B Jev 失败 → 零痕迹，重试可重新执行
C 组装失败（Jev 成功之后）→ 零痕迹
D 最终事务中途失败 → 整体回滚（session/round/candidates/operation 全无）
E operation_id 重复调用 → 返回首次结果，正式状态零变化
F 同 operation_id 并发 → 1 operation / 1 round / 1 session
G 不同 operation 并发抢最后一轮预算 → 只有一个成功，无超额
H 模拟进程崩溃（子进程真实 os._exit，多个断点）→ 无脏中间态，
  重试只产生一次正式结果
I COMMIT 成功后响应丢失 → 客户端重试读取已有结果，无重复副作用
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.recall import budget, service as recall_service, store
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


def hold(actors, text="八月搬家事件正文", date="2026-08-10"):
    from mariposa.memory import service as memory
    return memory.hold(
        actors["jiaming"], text=text, memory_date=date,
        date_confidence="exact", original_title="搬家",
        categories=["daily"], creation_mode="contemporaneous",
        raw_pending=False)


def start_op(actors, terms=("搬家",), op="op-x"):
    return registry.invoke(actors["jiaming"], "memory.recall.start",
                           {"query_plan": {
                               "original_request": "查询",
                               "channels": ["event"],
                               "lexical_terms": list(terms)},
                            "operation_id": op}, None)


def runtime_counts(root=None):
    """runtime 库正式状态计数（可指定外部根，供 crash 子进程库检查）。"""
    if root is None:
        with db.recall_runtime() as conn:
            return {
                "sessions": conn.execute(
                    "SELECT COUNT(*) c FROM recall_sessions"
                ).fetchone()["c"],
                "rounds": conn.execute(
                    "SELECT COUNT(*) c FROM recall_rounds").fetchone()["c"],
                "operations": conn.execute(
                    "SELECT COUNT(*) c FROM recall_operation_keys"
                ).fetchone()["c"],
                "candidates": conn.execute(
                    "SELECT COUNT(*) c FROM recall_candidates"
                ).fetchone()["c"],
            }
    path = os.path.join(str(root), "runtime", "recall", "recall.sqlite3")
    if not os.path.exists(path):
        return {"sessions": 0, "rounds": 0, "operations": 0, "candidates": 0}
    conn = sqlite3.connect(path)
    try:
        def one(q):
            return conn.execute(q).fetchone()[0]
        return {
            "sessions": one("SELECT COUNT(*) FROM recall_sessions"),
            "rounds": one("SELECT COUNT(*) FROM recall_rounds"),
            "operations": one(
                "SELECT COUNT(*) FROM recall_operation_keys"),
            "candidates": one("SELECT COUNT(*) FROM recall_candidates"),
        }
    finally:
        conn.close()


class TestAComputeFailureZeroTrace:
    def test_retrieval_failure_no_trace_then_retry(self, actors,
                                                   monkeypatch):
        hold(actors)
        from mariposa.recall import service as svc

        def boom(*a, **kw):
            raise RuntimeError("retrieval down")

        monkeypatch.setattr(svc, "_event_candidates", boom)
        with pytest.raises(RuntimeError):
            start_op(actors, op="op-a1")
        c = runtime_counts()
        assert c["sessions"] == 0 and c["rounds"] == 0 \
            and c["operations"] == 0 and c["candidates"] == 0, \
            f"检索失败留下了正式痕迹：{c}"
        monkeypatch.undo()
        packet = start_op(actors, op="op-a1")
        assert packet["data"]["recall_session_id"]
        c = runtime_counts()
        assert c["sessions"] == 1 and c["rounds"] == 1 \
            and c["operations"] == 1

    def test_jev_exception_no_trace_then_retry(self, actors, monkeypatch):
        hold(actors)
        from mariposa.retrieval.judges import base as judge_base

        class ExplodingJudge:
            def judge(self, *a, **kw):
                raise RuntimeError("jev exploded")

        monkeypatch.setattr(judge_base, "get_provider",
                            lambda: ExplodingJudge())
        with pytest.raises(RuntimeError):
            start_op(actors, op="op-b1")
        c = runtime_counts()
        assert c["sessions"] == 0 and c["rounds"] == 0 \
            and c["operations"] == 0, f"Jev 失败留下了正式痕迹：{c}"
        monkeypatch.undo()
        assert start_op(actors, op="op-b1")["data"]["candidates"] \
            is not None

    def test_assembly_failure_after_jev_no_trace(self, actors,
                                                 monkeypatch):
        """C：Jev 成功之后 result builder 抛异常 → 零正式副作用。"""
        hold(actors)
        from mariposa.recall import service as svc

        real = svc._finalize_cards

        def boom(cards):
            raise RuntimeError("assembly failed after judge")

        monkeypatch.setattr(svc, "_finalize_cards", boom)
        # _finalize_cards 在计算路径被引用为模块属性
        with pytest.raises(RuntimeError):
            start_op(actors, op="op-c1")
        c = runtime_counts()
        assert c["sessions"] == 0 and c["rounds"] == 0 \
            and c["operations"] == 0, f"组装失败留下了正式痕迹：{c}"
        monkeypatch.undo()
        assert real  # 保持引用
        start_op(actors, op="op-c1")


class TestDFinalTransactionAtomicity:
    def test_mid_transaction_failure_full_rollback(self, actors,
                                                   monkeypatch):
        """D：session/round INSERT 成功后 receipts 写入抛异常 → 整体回滚。"""
        hold(actors)

        def boom(conn, session_id, receipts, revision=None):
            raise RuntimeError("receipts insert failed")

        monkeypatch.setattr(store, "add_receipts", boom)
        with pytest.raises(RuntimeError):
            start_op(actors, op="op-d1")
        c = runtime_counts()
        assert c == {"sessions": 0, "rounds": 0, "operations": 0,
                     "candidates": 0}, f"事务中途失败未整体回滚：{c}"
        monkeypatch.undo()
        start_op(actors, op="op-d1")
        assert runtime_counts()["sessions"] == 1


class TestEIdempotentResubmit:
    def test_duplicate_operation_returns_first_result(self, actors):
        hold(actors)
        r1 = start_op(actors, op="op-e1")
        r2 = start_op(actors, op="op-e1")
        assert r2["data"].get("idempotent_replay") is True
        p1, p2 = r1["data"], r2["data"]
        assert p1["recall_session_id"] == p2["recall_session_id"]
        c = runtime_counts()
        assert c["sessions"] == 1 and c["rounds"] == 1 \
            and c["operations"] == 1, f"重复提交产生了新副作用：{c}"

    def test_response_lost_client_resubmits(self, actors):
        """I：COMMIT 已成功、响应丢失 → 重试读取已有结果。"""
        hold(actors)
        r1 = start_op(actors, op="op-i1")
        sid = r1["data"]["recall_session_id"]
        # 模拟客户端从未收到响应，再次提交同 operation_id
        r2 = start_op(actors, op="op-i1")
        assert r2["data"].get("idempotent_replay") is True
        assert r2["data"]["recall_session_id"] == sid
        c = runtime_counts()
        assert c["sessions"] == 1 and c["rounds"] == 1


class TestFConcurrentSameOperation:
    def test_two_workers_same_operation_single_effect(self, actors):
        hold(actors)
        gate = threading.Barrier(2, timeout=10)

        def run(_i):
            gate.wait()
            return start_op(actors, op="op-f1")

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1, f2 = ex.submit(run, 0), ex.submit(run, 1)
            r1, r2 = f1.result(timeout=30), f2.result(timeout=30)
        sids = {r1["data"]["recall_session_id"],
                r2["data"]["recall_session_id"]}
        assert len(sids) == 1, "同 operation 并发产生了两个 session"
        replays = [bool(r1["data"].get("idempotent_replay")),
                   bool(r2["data"].get("idempotent_replay"))]
        assert replays.count(True) == 1
        c = runtime_counts()
        assert c["sessions"] == 1 and c["rounds"] == 1 \
            and c["operations"] == 1


class TestGBudgetFinalCheckConcurrency:
    def test_concurrent_refines_cannot_exceed_burst_rounds(self, actors):
        """G：burst 只剩最后一轮时两个不同 operation 并发 refine，
        最终只有一个成为该轮，round 总数不突破上限。"""
        hold(actors)
        p = start_op(actors, op="op-g0")          # round 1
        sid = p["data"]["recall_session_id"]
        r2 = registry.invoke(
            actors["jiaming"], "memory.recall.refine",
            {"session_id": sid,
             "query_plan": {"original_request": "二查", "channels": ["event"],
                            "lexical_terms": ["搬家"]},
             "operation_id": "op-g1"}, None)       # round 2
        assert r2["data"]["revision"] == 2
        # 两边都完成事务外计算后在最终事务前同步冲线，制造真实的
        # "计算都完成、写事务并发"场景（预算终检/CAS 串行化验证）
        from mariposa.recall import service as svc
        real_compute = svc._run_round_compute
        finish_gate = threading.Barrier(2, timeout=15)

        def gated_compute(session, plan, principal=None):
            out = real_compute(session, plan, principal)
            finish_gate.wait()
            return out
        svc._run_round_compute = gated_compute
        results, errors = {}, {}

        def run(name):
            try:
                results[name] = registry.invoke(
                    actors["jiaming"], "memory.recall.refine",
                    {"session_id": sid,
                     "query_plan": {"original_request": "三查",
                                    "channels": ["event"],
                                    "lexical_terms": ["搬家"]},
                     "operation_id": f"op-g2-{name}"}, None)
            except Exception as e:  # noqa: BLE001
                errors[name] = e

        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1, f2 = ex.submit(run, "a"), ex.submit(run, "b")
                f1.result(timeout=30), f2.result(timeout=30)
        finally:
            svc._run_round_compute = real_compute
        with db.recall_runtime() as conn:
            rounds = conn.execute(
                "SELECT COUNT(*) c FROM recall_rounds WHERE session_id=?",
                (sid,)).fetchone()["c"]
        # burst 上限 3 轮：并发后总量不得突破（恰好 3 = 其中一个赢）
        assert rounds <= 3, f"并发 refine 突破了 burst 轮数上限：{rounds}"
        assert rounds == 3, "应有且仅有一个并发 refine 成为第 3 轮"
        assert len(results) == 1 and len(errors) == 1


# ---------- H：真实子进程 crash injection ----------

def _child_crash(root: str, crash_at: str, op_id: str, q):
    """子进程：独立隔离根内跑 start，在指定断点 os._exit。

    MARIPOSA_ROOT 由父进程在创建子进程前设置（spawn 在进程创建时
    继承父 env；函数内再改对已加载的 config 无效）。
    """
    import sys
    sys.path.insert(0, os.environ["MARIPOSA_TEST_BACKEND"])
    os.environ["MARIPOSA_ALLOW_CREATE"] = "1"  # 隔离根首建（fail-closed）
    from mariposa import schema as schema_m
    schema_m.migrate()
    schema_m.migrate_runtime()
    from mariposa.identity import service as identity_m
    from mariposa.memory import service as memory_m
    from mariposa.recall import service as recall_svc, store as recall_st
    principal = identity_m.Principal("jiaming", "周家明", "agent",
                                     "claude_chat", "bj")
    memory_m.hold(principal, text="崩溃注入搬家正文", memory_date="2026-08-10",
                  date_confidence="exact", original_title="搬家",
                  categories=["daily"], creation_mode="contemporaneous",
                  raw_pending=False)
    ctx = {"principal_id": "jiaming", "operation_key": f"start:new:{op_id}",
           "payload_hash": "h-crash"}

    if crash_at == "after_draft":
        real = recall_st.new_session_draft

        def hook(*a, **kw):
            d = real(*a, **kw)
            os._exit(73)
        recall_st.new_session_draft = hook
    elif crash_at == "after_retrieval":
        real = recall_svc._event_candidates

        def hook(*a, **kw):
            out = real(*a, **kw)
            os._exit(73)
        recall_svc._event_candidates = hook
    elif crash_at == "after_jev":
        from mariposa.retrieval.judges import base as jb
        real = jb.get_provider

        class Wrap:
            def judge(self, *a, **kw):
                r = real().judge(*a, **kw)
                os._exit(73)
        jb.get_provider = lambda: Wrap()
    elif crash_at == "in_final_transaction":
        real = recall_st.insert_session

        def hook(conn, draft):
            real(conn, draft)      # session INSERT 成功
            os._exit(73)           # 提交前死亡
        recall_st.insert_session = hook
    try:
        recall_svc.start(principal, {
            "query_plan": {"original_request": "查", "channels": ["event"],
                           "lexical_terms": ["搬家"]}}, op_ctx=ctx)
    finally:
        os._exit(0)


def _child_resume(root: str, op_id: str, q):
    """子进程：crash 后同 op_id 重试，回传 sid。"""
    import sys
    sys.path.insert(0, os.environ["MARIPOSA_TEST_BACKEND"])
    os.environ["MARIPOSA_ALLOW_CREATE"] = "1"  # 隔离根首建（fail-closed）
    from mariposa import schema as schema_m
    schema_m.migrate()
    schema_m.migrate_runtime()
    from mariposa.identity import service as identity_m
    from mariposa.recall import service as recall_svc
    principal = identity_m.Principal("jiaming", "周家明", "agent",
                                     "claude_chat", "bj")
    ctx = {"principal_id": "jiaming", "operation_key": f"start:new:{op_id}",
           "payload_hash": "h-crash"}
    packet = recall_svc.start(principal, {
        "query_plan": {"original_request": "查", "channels": ["event"],
                       "lexical_terms": ["搬家"]}}, op_ctx=ctx)
    q.put({"sid": packet["recall_session_id"]})


class TestHCrashInjection:
    @pytest.mark.parametrize("crash_at", [
        "after_draft", "after_retrieval", "after_jev",
        "in_final_transaction"])
    def test_crash_leaves_no_trace_and_retry_succeeds(self, actors,
                                                      crash_at):
        # conftest 测试根保险丝只认系统临时目录等白名单根；
        # basetemp 不在其中，crash 隔离根必须建在系统 temp 下
        import tempfile
        import shutil
        from pathlib import Path
        root = Path(tempfile.mkdtemp(prefix=f"mariposa-crash-{crash_at}-"))
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        os.environ["MARIPOSA_TEST_BACKEND"] = os.path.abspath("backend")
        old_root = os.environ.get("MARIPOSA_ROOT")
        os.environ["MARIPOSA_ROOT"] = str(root)  # 子进程继承此根
        try:
            p = ctx.Process(target=_child_crash,
                            args=(str(root), crash_at, "op-h1", q))
            p.start()
            p.join(timeout=60)
        finally:
            if old_root is not None:
                os.environ["MARIPOSA_ROOT"] = old_root
            crash_db = runtime_counts(root)
        # 崩溃后：数据库无任何本 operation 的正式痕迹
        c = crash_db
        assert c["sessions"] == 0 and c["rounds"] == 0 \
            and c["operations"] == 0, \
            f"crash({crash_at}) 留下了脏中间态：{c}"
        # 重启后同 op_id 重试：只产生一次正式结果
        os.environ["MARIPOSA_ROOT"] = str(root)
        try:
            p2 = ctx.Process(target=_child_resume,
                             args=(str(root), "op-h1", q))
            p2.start()
            p2.join(timeout=60)
        finally:
            if old_root is not None:
                os.environ["MARIPOSA_ROOT"] = old_root
        assert not p2.exitcode, f"重试子进程失败 exit={p2.exitcode}"
        sid = q.get(timeout=5)
        c = runtime_counts(root)
        assert c["sessions"] == 1 and c["rounds"] == 1 \
            and c["operations"] == 1, \
            f"重试后正式状态不唯一：{c}"
        assert sid
        shutil.rmtree(root, ignore_errors=True)
