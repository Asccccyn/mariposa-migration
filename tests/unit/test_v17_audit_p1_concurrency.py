"""v1.7 审计整改第一阶段回归：F39 memory revision 并发冲突。

覆盖审计要求：
- 同 revision 并发编辑：一方成功（revision+1），另一方结构化
  VERSION_CONFLICT（含 expected/current），不出现裸 IntegrityError
- 生成逻辑上无法解释的 revision（版本历史保持 1、2 连续）
- 同一来源（相同 raw_refs）并发 hold 只产生一个逻辑 memory，
  另一方 deduplicated 返回同一 memory_id
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mariposa import db
from mariposa.errors import VersionConflict
from mariposa.identity import service as identity
from mariposa.memory import extras as memory_extras
from mariposa.memory import service as memory
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold_v2(principal, text, **kw):
    base = dict(text=text, memory_date="2026-09-10",
                date_confidence="exact", original_title="并发审计",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(principal, **base)


class TestF39ConcurrentEdit:
    def test_same_revision_concurrent_update(self, actors):
        out = hold_v2(actors["jiaming"], "并发编辑场景初始正文")
        mid = out["memory_id"]
        gate = threading.Barrier(2, timeout=10)
        results, errors = {}, {}

        def run(name, new_text):
            gate.wait()
            try:
                results[name] = memory_extras.update_text(
                    actors["jiaming"].principal_id, mid,
                    expected_version=1, text=new_text)
            except Exception as e:  # noqa: BLE001
                errors[name] = e

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(run, "a", "甲方修改后的正文")
            f2 = ex.submit(run, "b", "乙方修改后的正文")
            f1.result(timeout=20), f2.result(timeout=20)

        assert len(results) == 1 and len(errors) == 1, \
            f"应一胜一负：{results} {errors}"
        loser = errors.popitem()[1]
        assert isinstance(loser, VersionConflict), \
            f"并发冲突必须是结构化 VERSION_CONFLICT，得到 {type(loser)}"
        assert loser.detail["expected"] == 1
        assert loser.detail["current"] == 2
        winner = results.popitem()[1]
        assert winner["version"] == 2
        # 版本历史连续可解释：1 -> 2，没有静默 last-write-wins 的第三版
        with db.formal() as conn:
            versions = [r["version_no"] for r in conn.execute(
                "SELECT version_no FROM memory_versions WHERE memory_id=?"
                " ORDER BY version_no", (mid,))]
        assert versions == [1, 2]

    def test_version_conflict_is_structured_not_500(self, actors):
        out = hold_v2(actors["jiaming"], "顺序冲突场景正文")
        mid = out["memory_id"]
        memory_extras.update_text(
            actors["jiaming"].principal_id, mid, expected_version=1,
            text="第一轮修改")
        with pytest.raises(VersionConflict) as ei:
            memory_extras.update_text(
                actors["jiaming"].principal_id, mid, expected_version=1,
                text="基于旧版本的迟到修改")
        assert ei.value.http_status == 409
        assert ei.value.code == "VERSION_CONFLICT"


class TestF39ConcurrentSameSourceHold:
    def test_concurrent_hold_same_raw_ref_single_memory(self, actors):
        refs = [{"conversation_id": "conv-c1", "message_from": "m-1",
                 "message_to": "m-2"}]
        gate = threading.Barrier(2, timeout=10)
        results, errors = {}, {}

        def run(name):
            gate.wait()
            try:
                results[name] = hold_v2(
                    actors["jiaming"], f"同源并发正文{name}",
                    raw_refs=refs)
            except Exception as e:  # noqa: BLE001
                errors[name] = e

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(run, "a")
            f2 = ex.submit(run, "b")
            f1.result(timeout=20), f2.result(timeout=20)

        assert not errors, f"并发同源 hold 不应失败：{errors}"
        mids = {r["memory_id"] for r in results.values()}
        assert len(mids) == 1, \
            f"同源并发 hold 产生了多个逻辑 memory：{results}"
        dedup_flags = [bool(r.get("deduplicated")) for r in results.values()]
        assert dedup_flags.count(True) == 1
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_raw_refs WHERE"
                " source_hash=?", (memory._raw_ref_hash(refs[0]),)
            ).fetchone()["c"]
        assert n == 1

    def test_sequential_same_raw_ref_dedup_preserved(self, actors):
        refs = [{"conversation_id": "conv-c2", "message_from": "m-3",
                 "message_to": "m-4"}]
        r1 = hold_v2(actors["jiaming"], "顺序同源去重正文一",
                     raw_refs=refs)
        r2 = hold_v2(actors["jiaming"], "顺序同源去重正文二",
                     raw_refs=refs)
        assert r2.get("deduplicated") is True
        assert r1["memory_id"] == r2["memory_id"]
