"""episode 回归（她批 2026-10-07"做吧，我需要这个功能"）。

蓝图 MARIPOSA_LIFECYCLE_v1.0 §2/§6 全链：
- 三态转移 OPEN/QUIESCENT/CLOSED
- CLOSED 拒绝普通 continue；确为误关走 correct
- correct 恢复非 CLOSED → 清闭合记录，旧值留审计
- pause QUIESCENT→QUIESCENT 返回 no_change
- expected_revision CAS 版本冲突
- continue/close selections 去重追加不覆盖
- close 必带 terminal+decision 证据
- scope 隔离（跨 scope 操作拒）
- list 分页/状态过滤/不给全库
- label ≤80 码点
"""
from __future__ import annotations

import json
import pytest

from mariposa import db
from mariposa.episodes import service as ep
from mariposa.errors import Forbidden, NotFound
from mariposa.identity import service as identity
from tests.conftest import reset_all


@pytest.fixture()
def jiaming():
    reset_all()
    return identity.Principal("jiaming", "周家明", "agent",
                              "estomago", "bj")


def _start(jiaming, label="装修新房", scope="private"):
    return ep.apply(jiaming, {
        "action": "start", "scope_id": scope, "reason_code": "tracking_requested",
        "label": label,
        "source_selections": [{"conversation_id": "sc_1",
                                "members": [{"source_message_id": "sm_1",
                                              "content_hash": "a" * 64}]}],
    })


class TestStartAndTransitions:
    def test_start_creates_open_episode(self, jiaming):
        out = _start(jiaming)
        assert out["state"] == "OPEN" and out["revision"] == 1
        assert out["episode_id"].startswith("ep_")

    def test_label_max_80_code_points(self, jiaming):
        with pytest.raises(Forbidden) as ei:
            _start(jiaming, label="字" * 81)
        assert ei.value.detail.get("code") == "INVALID_ARGUMENT"

    def test_full_lifecycle_open_pause_continue_close(self, jiaming):
        e = _start(jiaming)
        eid, rev = e["episode_id"], e["revision"]
        # pause → QUIESCENT
        p = ep.apply(jiaming, {"action": "pause", "scope_id": "private",
                                "episode_id": eid, "expected_revision": rev,
                                "reason_code": "waiting"})
        assert p["state"] == "QUIESCENT" and p["revision"] == rev + 1
        # pause again → no_change（不制造无意义版本）
        p2 = ep.apply(jiaming, {"action": "pause", "scope_id": "private",
                                 "episode_id": eid,
                                 "expected_revision": rev + 1,
                                 "reason_code": "waiting"})
        assert p2.get("no_change") is True and p2["revision"] == rev + 1
        # continue → OPEN
        c = ep.apply(jiaming, {"action": "continue", "scope_id": "private",
                                "episode_id": eid,
                                "expected_revision": rev + 1,
                                "reason_code": "same_episode"})
        assert c["state"] == "OPEN"
        # close → CLOSED（带证据）
        cl = ep.apply(jiaming, {
            "action": "close", "scope_id": "private", "episode_id": eid,
            "expected_revision": c["revision"], "reason_code": "explicit_completion",
            "terminal_source_ref": {"source_message_id": "sm_1",
                                     "content_hash": "a" * 64},
            "decision_source_refs": [{"source_message_id": "sm_2"}]})
        assert cl["state"] == "CLOSED"

    def test_closed_rejects_continue(self, jiaming):
        e = _start(jiaming)
        cl = ep.apply(jiaming, {
            "action": "close", "scope_id": "private",
            "episode_id": e["episode_id"],
            "expected_revision": e["revision"],
            "reason_code": "goal_resolved",
            "terminal_source_ref": {"source_message_id": "sm_1"},
            "decision_source_refs": [{"source_message_id": "sm_1"}]})
        assert cl["state"] == "CLOSED"
        with pytest.raises(Forbidden) as ei:
            ep.apply(jiaming, {"action": "continue", "scope_id": "private",
                                "episode_id": e["episode_id"],
                                "expected_revision": cl["revision"],
                                "reason_code": "same_episode"})
        assert ei.value.detail.get("code") == "INVALID_TRANSITION"

    def test_close_requires_terminal_and_decision(self, jiaming):
        e = _start(jiaming)
        with pytest.raises(Forbidden):
            ep.apply(jiaming, {"action": "close", "scope_id": "private",
                                "episode_id": e["episode_id"],
                                "expected_revision": e["revision"],
                                "reason_code": "explicit_completion"})


class TestCorrect:
    def test_correct_restores_open_and_clears_close(self, jiaming):
        e = _start(jiaming)
        cl = ep.apply(jiaming, {
            "action": "close", "scope_id": "private",
            "episode_id": e["episode_id"],
            "expected_revision": e["revision"],
            "reason_code": "episode_concluded",
            "terminal_source_ref": {"source_message_id": "sm_1"},
            "decision_source_refs": [{"source_message_id": "sm_1"}]})
        rev = cl["revision"]
        cr = ep.apply(jiaming, {
            "action": "correct", "scope_id": "private",
            "episode_id": e["episode_id"], "expected_revision": rev,
            "reason_code": "boundary_error", "corrected_state": "OPEN",
            "note": "模型误判已结束"})
        assert cr["state"] == "OPEN" and cr["corrected_from"] == "CLOSED"
        # 清空闭合记录
        got = ep.get(jiaming, {"episode_id": e["episode_id"]})
        assert got["closed_recorded_at"] is None
        assert got["terminal_source_ref"] is None
        # 审计保留旧闭合值
        with db.formal() as conn:
            logs = conn.execute(
                "SELECT payload FROM audit_events WHERE"
                " resource_id=? AND event_type='episode.corrected'",
                (e["episode_id"],)).fetchall()
        assert logs, "纠错审计在"
        assert "prev_terminal" in json.loads(logs[-1]["payload"])


class TestCasAndScope:
    def test_version_conflict(self, jiaming):
        e = _start(jiaming)
        with pytest.raises(Forbidden) as ei:
            ep.apply(jiaming, {"action": "pause", "scope_id": "private",
                                "episode_id": e["episode_id"],
                                "expected_revision": 99,  # 过时版本
                                "reason_code": "waiting"})
        assert ei.value.detail.get("code") == "VERSION_CONFLICT"

    def test_cross_scope_rejected(self, jiaming):
        e = _start(jiaming, scope="private")
        with pytest.raises(Forbidden):
            ep.apply(jiaming, {"action": "pause", "scope_id": "other-scope",
                                "episode_id": e["episode_id"],
                                "expected_revision": 1,
                                "reason_code": "waiting"})

    def test_continue_appends_segments_dedup(self, jiaming):
        e = _start(jiaming)
        seg1 = {"conversation_id": "sc_1",
                 "members": [{"source_message_id": "sm_1",
                              "content_hash": "a" * 64}]}
        seg2 = {"conversation_id": "sc_1",
                 "members": [{"source_message_id": "sm_2",
                              "content_hash": "b" * 64}]}
        c = ep.apply(jiaming, {"action": "continue", "scope_id": "private",
                                "episode_id": e["episode_id"],
                                "expected_revision": e["revision"],
                                "reason_code": "same_episode",
                                "source_selections": [seg1, seg2]})
        got = ep.get(jiaming, {"episode_id": e["episode_id"]})
        segs = got["source_selections"]
        assert len(segs) == 2, "去重追加（seg1 已在 start 写入，不重复）"
        # 再追加 seg2 → 仍 2 段（已存在）
        ep.apply(jiaming, {"action": "continue", "scope_id": "private",
                            "episode_id": e["episode_id"],
                            "expected_revision": c["revision"],
                            "reason_code": "same_episode",
                            "source_selections": [seg2]})
        got2 = ep.get(jiaming, {"episode_id": e["episode_id"]})
        assert len(got2["source_selections"]) == 2


class TestListAndGet:
    def test_list_scoped_and_filtered(self, jiaming):
        e1 = _start(jiaming, label="事件一")
        e2 = _start(jiaming, label="事件二")
        ep.apply(jiaming, {"action": "close", "scope_id": "private",
                            "episode_id": e2["episode_id"],
                            "expected_revision": 1,
                            "reason_code": "goal_resolved",
                            "terminal_source_ref": {"source_message_id": "s"},
                            "decision_source_refs": [{"source_message_id": "s"}]})
        # 全量（含本 fixture 内两个事件）
        all_list = ep.list_episodes(jiaming, {"scope_id": "private"})
        assert all_list["count"] >= 2
        labels = [e["label"] for e in all_list["episodes"]]
        assert "事件一" in labels and "事件二" in labels
        # 只 OPEN（事件二已 CLOSED）
        open_only = ep.list_episodes(jiaming, {"scope_id": "private",
                                                "states": ["OPEN"]})
        open_labels = [e["label"] for e in open_only["episodes"]]
        assert "事件一" in open_labels
        assert "事件二" not in open_labels
        # 其他 scope 空
        other = ep.list_episodes(jiaming, {"scope_id": "other"})
        assert other["count"] == 0

    def test_get_not_found(self, jiaming):
        with pytest.raises(NotFound):
            ep.get(jiaming, {"episode_id": "ep_missing"})


def open_list_label(lst):
    return lst["episodes"][0]["label"] if lst["episodes"] else None
