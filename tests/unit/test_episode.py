"""episode 回归（她批 2026-10-07"做吧"）+ 复审返修 R05-R09（2026-10-07）。

蓝图 MARIPOSA_LIFECYCLE_v1.0 §2/§6 全链 + 复审验收条件：
- 三态转移 OPEN/QUIESCENT/CLOSED；CLOSED 拒绝普通 continue；确为误关走 correct
- R07：来源证据必须是**真实已发布的 Source**（真实 ingest 夹具，不再用
  不存在的 sc_1/sm_1 证明证据门）——不存在/未发布/hash 不符/终点非成员全拒
- R08：correct 真正应用成员/终点/判断依据（note+判断依据必填）；纠正到
  CLOSED 满足闭合不变量；旧判断依据完整留痕；get 出站 decision refs
- R06：operation_id 领域操作身份——同 op 回放原结果（当前 revision 分列）、
  同 op 异 payload 结构化冲突
- R09：范围→身份→动作映射——她可改（continue/pause/close/correct）不可
  start；读全量通配；未授权范围拒
- expected_revision CAS；continue/close 去重追加；list 分页/过滤
"""
from __future__ import annotations

import hashlib
import json

import pytest

from mariposa import db
from mariposa.episodes import service as ep
from mariposa.errors import Forbidden, IdempotencyConflict, NotFound
from mariposa.identity import service as identity
from mariposa.identity import Principal
from mariposa.source import ingest as live
from tests.conftest import reset_all

SCOPE = "private"
ARCHIVE_BINDING = "binding_episode_test"
STREAM = "stream_episode_test"
ROOM = "room-ep"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def actors():
    reset_all()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO client_bindings(binding_id, token_hash,"
                " principal_id, entry_source, capabilities_allowlist,"
                " created_at) VALUES(?,?,?,?,?,datetime('now'))",
                (ARCHIVE_BINDING, "x" * 64, "worker", "estomago_archive",
                 json.dumps(["source.ingest"])))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    live.create_grant(ARCHIVE_BINDING, STREAM, "estomago", ROOM,
                      ["user", "assistant"], "private", "qiaosheng")
    worker = Principal("worker", "维护工具人", "agent", "estomago_archive",
                       ARCHIVE_BINDING,
                       capabilities_allowlist=frozenset(["source.ingest"]))
    out = {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "estomago", "bj"),
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "worker": worker,
    }
    # R09 数据驱动映射（方案B）：测试范围显式授权——jiaming 全动作、
    # qiaosheng 改类+读（读全量另有默认 "*" 通配种子）
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for pid, acts in (
                ("jiaming", ["read", "list", "start", "continue", "pause",
                             "close", "correct"]),
                ("qiaosheng", ["read", "list", "continue", "pause",
                               "close", "correct"]),
            ):
                conn.execute(
                    "INSERT INTO episode_scope_grants(scope_id, principal_id,"
                    " actions, created_at) VALUES(?,?,?,datetime('now'))",
                    (SCOPE, pid, json.dumps(acts)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return out


def _ingest(worker, omid: str, text: str, seq: int = 1, rev: int = 1,
            prev_rev=None):
    r = live.ingest(worker, {
        "operation_id": f"epsrc-{omid}-{rev}", "stream_id": STREAM,
        "origin_instance": "estomago", "origin_conversation_id": ROOM,
        "messages": [{
            "origin_message_id": omid, "revision": rev,
            "previous_revision": prev_rev, "conversation_sequence": seq,
            "predecessor": None, "sender": "user",
            "published_kind": "chat_message",
            "occurred_at": "2026-10-07T01:00:00Z",
            "received_at": "2026-10-07T01:00:00Z",
            "published_at": "2026-10-07T01:00:00Z",
            "text": text, "assets": [],
            "content_hash": _text_hash(text)}]})
    return r["messages"][0]


def _seg(ack) -> dict:
    return {"conversation_id": ack["source_conversation_id"],
            "members": [{"source_message_id": ack["source_message_id"],
                         "content_hash": ack["content_hash"]}]}


def _ref(ack, offset=None) -> dict:
    out = {"source_message_id": ack["source_message_id"],
           "content_hash": ack["content_hash"]}
    if offset is not None:
        out["char_offset"] = offset
    return out


def _start(actors, label="装修新房", acks=None, scope=SCOPE, op=None,
           principal="jiaming"):
    acks = acks or [_ingest(actors["worker"], "m1", "我们开始计划装修")]
    args = {"action": "start", "scope_id": scope,
            "reason_code": "tracking_requested", "label": label,
            "source_selections": [_seg(a) for a in acks]}
    if op:
        args["operation_id"] = op
    return ep.apply(actors[principal], args)


class TestStartAndTransitions:
    def test_start_creates_open_episode_with_real_source(self, actors):
        out = _start(actors)
        assert out["state"] == "OPEN" and out["revision"] == 1
        assert out["episode_id"].startswith("ep_")

    def test_label_max_80_code_points(self, actors):
        with pytest.raises(Forbidden) as ei:
            _start(actors, label="字" * 81)
        assert ei.value.detail.get("code") == "INVALID_ARGUMENT"

    def test_full_lifecycle_open_pause_continue_close(self, actors):
        e = _start(actors)
        eid, rev = e["episode_id"], e["revision"]
        p = ep.apply(actors["jiaming"], {
            "action": "pause", "scope_id": SCOPE, "episode_id": eid,
            "expected_revision": rev, "reason_code": "waiting"})
        assert p["state"] == "QUIESCENT"
        c = ep.apply(actors["jiaming"], {
            "action": "continue", "scope_id": SCOPE, "episode_id": eid,
            "expected_revision": p["revision"], "reason_code": "same_episode"})
        assert c["state"] == "OPEN"
        ack2 = _ingest(actors["worker"], "m2", "地板选好了", seq=2)
        closed = ep.apply(actors["jiaming"], {
            "action": "close", "scope_id": SCOPE, "episode_id": eid,
            "expected_revision": c["revision"],
            "reason_code": "goal_resolved",
            "source_selections": [_seg(ack2)],
            "terminal_source_ref": _ref(ack2),
            "decision_source_refs": [_ref(ack2)]})
        assert closed["state"] == "CLOSED"
        got = ep.get(actors["jiaming"], {"episode_id": eid})
        assert got["closed_recorded_at"] is not None
        assert got["decision_source_refs"][0]["source_message_id"] == \
            ack2["source_message_id"], "get 出站判断依据引用"

    def test_closed_rejects_continue(self, actors):
        e = _start(actors)
        ack2 = _ingest(actors["worker"], "m3", "收尾", seq=2)
        ep.apply(actors["jiaming"], {
            "action": "close", "scope_id": SCOPE, "episode_id": e["episode_id"],
            "expected_revision": 1, "reason_code": "explicit_completion",
            "source_selections": [_seg(ack2)],
            "terminal_source_ref": _ref(ack2),
            "decision_source_refs": [_ref(ack2)]})
        with pytest.raises(Forbidden) as ei:
            ep.apply(actors["jiaming"], {
                "action": "continue", "scope_id": SCOPE,
                "episode_id": e["episode_id"], "expected_revision": 2,
                "reason_code": "same_episode"})
        assert ei.value.detail.get("code") == "INVALID_TRANSITION"

    def test_close_requires_terminal_and_decision(self, actors):
        e = _start(actors)
        with pytest.raises(Forbidden):
            ep.apply(actors["jiaming"], {
                "action": "close", "scope_id": SCOPE,
                "episode_id": e["episode_id"], "expected_revision": 1,
                "reason_code": "explicit_completion"})


class TestR07SourceEvidence:
    """R07：来源证据核验——真实/已发布/hash 钉住/终点属成员。"""

    def test_nonexistent_source_rejected(self, actors):
        with pytest.raises(Forbidden) as ei:
            ep.apply(actors["jiaming"], {
                "action": "start", "scope_id": SCOPE,
                "reason_code": "tracking_requested", "label": "虚构",
                "source_selections": [{
                    "conversation_id": "sc_x",
                    "members": [{"source_message_id": "sm_nope",
                                 "content_hash": "a" * 64}]}]})
        assert ei.value.detail.get("code") == "EPISODE_SOURCE_NOT_FOUND"

    def test_wrong_hash_rejected(self, actors):
        ack = _ingest(actors["worker"], "mh", "正文")
        with pytest.raises(Forbidden) as ei:
            ep.apply(actors["jiaming"], {
                "action": "start", "scope_id": SCOPE,
                "reason_code": "tracking_requested", "label": "错哈希",
                "source_selections": [{
                    "conversation_id": ack["source_conversation_id"],
                    "members": [{"source_message_id":
                                 ack["source_message_id"],
                                 "content_hash": "b" * 64}]}]})
        assert ei.value.detail.get("code") == "EPISODE_SOURCE_HASH_MISMATCH"

    def test_unpublished_source_rejected(self, actors):
        ack = _ingest(actors["worker"], "mu", "将被撤下")
        with db.formal() as conn:
            conn.execute("UPDATE source_messages SET published=0 WHERE id=?",
                         (ack["source_message_id"],))
        with pytest.raises(Forbidden) as ei:
            ep.apply(actors["jiaming"], {
                "action": "start", "scope_id": SCOPE,
                "reason_code": "tracking_requested", "label": "未发布",
                "source_selections": [_seg(ack)]})
        assert ei.value.detail.get("code") == "EPISODE_SOURCE_UNPUBLISHED"

    def test_terminal_outside_members_rejected(self, actors):
        ack_in = _ingest(actors["worker"], "mt1", "事件内消息")
        ack_out = _ingest(actors["worker"], "mt2", "事件外消息", seq=2)
        e = _start(actors, acks=[ack_in])
        with pytest.raises(Forbidden) as ei:
            ep.apply(actors["jiaming"], {
                "action": "close", "scope_id": SCOPE,
                "episode_id": e["episode_id"], "expected_revision": 1,
                "reason_code": "explicit_completion",
                "terminal_source_ref": _ref(ack_out),
                "decision_source_refs": [_ref(ack_in)]})
        assert ei.value.detail.get("code") == "EPISODE_TERMINAL_NOT_MEMBER"

    def test_offset_out_of_bounds_rejected(self, actors):
        ack = _ingest(actors["worker"], "mo", "正文短")
        with pytest.raises(Forbidden) as ei:
            ep.apply(actors["jiaming"], {
                "action": "start", "scope_id": SCOPE,
                "reason_code": "tracking_requested", "label": "偏移越界",
                "source_selections": [{
                    "conversation_id": ack["source_conversation_id"],
                    "members": [{"source_message_id":
                                 ack["source_message_id"],
                                 "content_hash": ack["content_hash"]}],
                    "start_char_offset": 999}]})
        assert ei.value.detail.get("code") == "EPISODE_SOURCE_OFFSET"


class TestR08Correct:
    def test_correct_applies_members_and_decision_and_keeps_audit(self, actors):
        ack1 = _ingest(actors["worker"], "mc1", "第一段")
        ack2 = _ingest(actors["worker"], "mc2", "判断依据旧", seq=2)
        ack3 = _ingest(actors["worker"], "mc3", "判断依据新", seq=3)
        e = _start(actors, acks=[ack1])
        eid = e["episode_id"]
        ep.apply(actors["jiaming"], {
            "action": "close", "scope_id": SCOPE, "episode_id": eid,
            "expected_revision": 1, "reason_code": "goal_resolved",
            "terminal_source_ref": _ref(ack1),
            "decision_source_refs": [_ref(ack2)]})
        out = ep.apply(actors["jiaming"], {
            "action": "correct", "scope_id": SCOPE, "episode_id": eid,
            "expected_revision": 2, "reason_code": "boundary_error",
            "corrected_state": "OPEN", "note": "边界划错，重开",
            "source_selections": [_seg(ack1), _seg(ack3)],
            "decision_source_refs": [_ref(ack3)]})
        assert out["state"] == "OPEN" and out["revision"] == 3
        got = ep.get(actors["jiaming"], {"episode_id": eid})
        assert got["closed_recorded_at"] is None, "纠正恢复非 CLOSED 清闭合"
        member_ids = {m["source_message_id"] for s in
                      got["source_selections"] for m in s["members"]}
        assert {ack1["source_message_id"], ack3["source_message_id"]} == \
            member_ids, "纠错后的成员真正生效（反例：读回仍旧成员）"
        assert got["decision_source_refs"][0]["source_message_id"] == \
            ack3["source_message_id"], "判断依据真正生效"
        # 旧判断依据完整留痕（审计）
        with db.formal() as conn:
            rows = conn.execute(
                "SELECT payload FROM audit_events WHERE"
                " event_type='episode.corrected' AND resource_id=?",
                (eid,)).fetchall()
        assert rows, "纠错必须留审计"
        payload = json.loads(rows[-1]["payload"])
        assert ack2["source_message_id"] in payload["prev_decision_refs"], \
            "旧判断依据在审计中可追溯（反例：无迹）"
        assert payload["prev_closed_at"] is not None

    def test_correct_to_closed_requires_invariants(self, actors):
        ack = _ingest(actors["worker"], "mcc", "闭合依据")
        e = _start(actors, acks=[ack])
        with pytest.raises(Forbidden) as ei:
            ep.apply(actors["jiaming"], {
                "action": "correct", "scope_id": SCOPE,
                "episode_id": e["episode_id"], "expected_revision": 1,
                "reason_code": "boundary_error",
                "corrected_state": "CLOSED", "note": "想直接纠正成关闭",
                "decision_source_refs": [_ref(ack)]})
        assert ei.value.detail.get("code") == "INVALID_ARGUMENT", \
            "纠正到 CLOSED 必须带 terminal（不得造缺闭合证据的 CLOSED）"
        out = ep.apply(actors["jiaming"], {
            "action": "correct", "scope_id": SCOPE,
            "episode_id": e["episode_id"], "expected_revision": 1,
            "reason_code": "boundary_error", "corrected_state": "CLOSED",
            "note": "补齐证据的纠正关闭",
            "terminal_source_ref": _ref(ack),
            "decision_source_refs": [_ref(ack)]})
        assert out["state"] == "CLOSED"
        got = ep.get(actors["jiaming"], {"episode_id": e["episode_id"]})
        assert got["closed_recorded_at"] is not None, "闭合时刻补齐"

    def test_correct_requires_note_and_decision(self, actors):
        ack = _ingest(actors["worker"], "mcn", "正文")
        e = _start(actors, acks=[ack])
        with pytest.raises(Forbidden):
            ep.apply(actors["jiaming"], {
                "action": "correct", "scope_id": SCOPE,
                "episode_id": e["episode_id"], "expected_revision": 1,
                "reason_code": "boundary_error", "corrected_state": "OPEN",
                "decision_source_refs": [_ref(ack)]})
        with pytest.raises(Forbidden):
            ep.apply(actors["jiaming"], {
                "action": "correct", "scope_id": SCOPE,
                "episode_id": e["episode_id"], "expected_revision": 1,
                "reason_code": "boundary_error", "corrected_state": "OPEN",
                "note": "缺判断依据"})


class TestR06OperationIdentity:
    def test_same_op_replays_original_result(self, actors):
        ack = _ingest(actors["worker"], "mo1", "正文")
        args = {"action": "start", "scope_id": SCOPE,
                "reason_code": "tracking_requested", "label": "幂等",
                "source_selections": [_seg(ack)],
                "operation_id": "es-ep-op-1"}
        r1 = ep.apply(actors["jiaming"], args)
        # 提交后/传输完成前崩溃的同 op 重试：回放原结果，不重复建事件
        r2 = ep.apply(actors["jiaming"], dict(args))
        assert r2["episode_id"] == r1["episode_id"]
        assert r2["idempotent_replay"] is True
        assert r2["current_state"] == "OPEN"
        assert r2["current_revision"] == r1["revision"], "当前 revision 分列"
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM semantic_episodes").fetchone()["c"]
        assert n == 1, "同 op 两次 start 只建一个事件（反例：两个 ID）"
        # 事件前进后重放：历史结果与当前 revision 分列
        ep.apply(actors["jiaming"], {
            "action": "pause", "scope_id": SCOPE,
            "episode_id": r1["episode_id"], "expected_revision": 1,
            "reason_code": "waiting"})
        r3 = ep.apply(actors["jiaming"], dict(args))
        assert r3["current_state"] == "QUIESCENT"
        assert r3["current_revision"] == 2

    def test_same_op_different_payload_conflicts(self, actors):
        ack = _ingest(actors["worker"], "mo2", "正文")
        ep.apply(actors["jiaming"], {
            "action": "start", "scope_id": SCOPE,
            "reason_code": "tracking_requested", "label": "第一版",
            "source_selections": [_seg(ack)],
            "operation_id": "es-ep-op-2"})
        with pytest.raises(IdempotencyConflict):
            ep.apply(actors["jiaming"], {
                "action": "start", "scope_id": SCOPE,
                "reason_code": "tracking_requested", "label": "第二版",
                "source_selections": [_seg(ack)],
                "operation_id": "es-ep-op-2"})


class TestR09Authorization:
    def test_qiaosheng_can_modify_but_not_start(self, actors):
        ack = _ingest(actors["worker"], "ma1", "她的事件")
        e = _start(actors, acks=[ack])  # jiaming 建
        with pytest.raises(Forbidden) as ei:
            _start(actors, acks=[ack], principal="qiaosheng")
        assert ei.value.detail.get("code") == "EPISODE_SCOPE_FORBIDDEN", \
            "start=写，不给她（她的裁定）"
        # 改类动作：continue/pause/close/correct 都可以
        p = ep.apply(actors["qiaosheng"], {
            "action": "pause", "scope_id": SCOPE,
            "episode_id": e["episode_id"], "expected_revision": 1,
            "reason_code": "waiting"})
        assert p["state"] == "QUIESCENT"
        c = ep.apply(actors["qiaosheng"], {
            "action": "correct", "scope_id": SCOPE,
            "episode_id": e["episode_id"], "expected_revision": 2,
            "reason_code": "boundary_error", "corrected_state": "OPEN",
            "note": "她的纠错权", "decision_source_refs": [_ref(ack)]})
        assert c["state"] == "OPEN", "她的纠错权不再被总门拒绝"

    def test_qiaosheng_reads_all_scopes_worker_denied(self, actors):
        ack = _ingest(actors["worker"], "mr1", "另一范围的内容")
        e = _start(actors, acks=[ack], scope="primary")  # 默认种子的范围
        got = ep.get(actors["qiaosheng"], {"episode_id": e["episode_id"]})
        assert got["episode_id"] == e["episode_id"], "读全量通配（同步她一份）"
        listed = ep.list_episodes(actors["qiaosheng"], {"scope_id": "primary"})
        assert listed["count"] >= 1
        with pytest.raises(Forbidden) as ei:
            ep.get(actors["worker"], {"episode_id": e["episode_id"]})
        assert ei.value.detail.get("code") == "EPISODE_SCOPE_FORBIDDEN"

    def test_ungranted_scope_rejected(self, actors):
        ack = _ingest(actors["worker"], "mu1", "未授权范围")
        with pytest.raises(Forbidden) as ei:
            _start(actors, acks=[ack], scope="other-room")
        assert ei.value.detail.get("code") == "EPISODE_SCOPE_FORBIDDEN"


class TestCasAndScope:
    def test_version_conflict(self, actors):
        e = _start(actors)
        with pytest.raises(Forbidden) as ei:
            ep.apply(actors["jiaming"], {
                "action": "pause", "scope_id": SCOPE,
                "episode_id": e["episode_id"], "expected_revision": 99,
                "reason_code": "waiting"})
        assert ei.value.detail.get("code") == "VERSION_CONFLICT"

    def test_cross_scope_rejected(self, actors):
        e = _start(actors)
        with pytest.raises(Forbidden):
            ep.apply(actors["jiaming"], {
                "action": "pause", "scope_id": "elsewhere",
                "episode_id": e["episode_id"], "expected_revision": 1,
                "reason_code": "waiting"})

    def test_continue_appends_segments_dedup(self, actors):
        ack = _ingest(actors["worker"], "md1", "段一")
        e = _start(actors, acks=[ack])
        seg1 = _seg(ack)
        seg2 = dict(seg1, members=[dict(seg1["members"][0])])
        c = ep.apply(actors["jiaming"], {
            "action": "continue", "scope_id": SCOPE,
            "episode_id": e["episode_id"], "expected_revision": 1,
            "reason_code": "same_episode",
            "source_selections": [seg1, seg2]})
        got = ep.get(actors["jiaming"], {"episode_id": e["episode_id"]})
        assert len(got["source_selections"]) == 1
        c2 = ep.apply(actors["jiaming"], {
            "action": "continue", "scope_id": SCOPE,
            "episode_id": e["episode_id"], "expected_revision":
                c["revision"],
            "reason_code": "same_episode", "source_selections": [seg2]})
        got2 = ep.get(actors["jiaming"], {"episode_id": e["episode_id"]})
        assert len(got2["source_selections"]) == 1, "同成员去重不重复追加"
        assert c2["revision"] == c["revision"] + 1


class TestListAndGet:
    def test_list_scoped_and_filtered(self, actors):
        _start(actors, label="事件甲")
        _start(actors, label="事件乙")
        out = ep.list_episodes(actors["jiaming"], {"scope_id": SCOPE})
        assert out["count"] == 2
        opens = ep.list_episodes(
            actors["jiaming"], {"scope_id": SCOPE, "states": ["OPEN"]})
        assert opens["count"] == 2
        listed = ep.list_episodes(
            actors["jiaming"], {"scope_id": SCOPE, "limit": 1})
        assert listed["count"] == 1 and listed["has_more"] is True
        assert listed["next_cursor"]["updated_before"], "续页游标可消费"

    def test_get_not_found(self, actors):
        with pytest.raises(NotFound):
            ep.get(actors["jiaming"], {"episode_id": "ep_none"})
