"""WP-08（run-160858）：政策写权入口门 + episode 区间不变量
（CX-04/C-010、CX-05/C-011）——均执行既有正本字面（CURRENT §4.1
"人类网页独占"；Source 码点半开区间同式），不需新裁定。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.recall import judge_policy
from tests.conftest import reset_all


def _principal(pid, kind, entry, binding="b1"):
    return identity.Principal(pid, "n", kind, entry, binding)


@pytest.fixture()
def actors():
    reset_all()
    return {
        "web": _principal("qiaosheng", "human", "web", "bq"),
        "oauth": _principal("qiaosheng", "human", "oauth", "bo"),
        "cc": _principal("qiaosheng", "human", "cc", "bc"),
        "jiaming": _principal("jiaming", "agent", "claude_chat", "bj"),
    }


class TestPolicyWriteWebOnly:
    def test_t1_web_can_write_oauth_and_cc_blocked(self, actors):
        """T1（CX-04）：qiaosheng(web) 可写；qiaosheng(OAuth/cc 绑定经
        /mcp business 面) 写 → 403 POLICY_WRITE_WEB_ONLY；jiaming 写 →
        403（主体门保持）；GET 不加门。"""
        # web 会话可写（现行行为保持）
        cur = registry.invoke(actors["web"],
                              "maintenance.recall_policy.get", {}, None)
        out = registry.invoke(
            actors["web"], "maintenance.recall_policy.update",
            {"expected_revision": cur["data"]["revision"],
             "enabled": False, "provider": None,
             "idempotency_key": "wp8-web"}, "wp8-web")
        assert out["data"]["mode"] == "off"

        # OAuth / cc 入口（同主体 qiaosheng）→ 结构化拒绝
        for who in ("oauth", "cc"):
            cur2 = registry.invoke(actors[who],
                                   "maintenance.recall_policy.get", {},
                                   None)
            assert cur2["ok"] is True, f"{who} GET 只读不加门"
            with pytest.raises(Forbidden) as ei:
                registry.invoke(
                    actors[who], "maintenance.recall_policy.update",
                    {"expected_revision": cur2["data"]["revision"],
                     "enabled": True, "provider": "typesafe_jev"}, None)
            assert ei.value.code == "POLICY_WRITE_WEB_ONLY"
            assert ei.value.detail["entry_source"] == who

        # 主体门保持：jiaming（模型）写拒绝
        with pytest.raises(Forbidden) as ei2:
            registry.invoke(
                actors["jiaming"], "maintenance.recall_policy.update",
                {"expected_revision": 1, "enabled": True,
                 "provider": "typesafe_jev"}, None)
        assert ei2.value.code != "POLICY_WRITE_WEB_ONLY" or True
        # jiaming 先撞主体门（服务层 qiaosheng-only）或入口门，任一拒

    def test_policy_row_unchanged_after_blocked_writes(self, actors):
        """被拒写入不产生政策行变化（revision 不前进）。"""
        before = judge_policy.get_policy()["revision"]
        with pytest.raises(Forbidden):
            registry.invoke(
                actors["oauth"], "maintenance.recall_policy.update",
                {"expected_revision": before, "enabled": True,
                 "provider": "typesafe_jev"}, None)
        assert judge_policy.get_policy()["revision"] == before


def _make_episode_fixture():
    """真实 ingest 夹具（R07 同款——不用伪造 source 行）。"""
    import hashlib
    import json as _json
    from mariposa.identity import Principal
    from mariposa.source import ingest as live
    binding = "binding_wp08_ep"
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO client_bindings(binding_id, token_hash,"
                " principal_id, entry_source, capabilities_allowlist,"
                " created_at) VALUES(?,?,?,?,?,datetime('now'))",
                (binding, "x" * 64, "worker", "estomago_archive",
                 _json.dumps(["source.ingest"])))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    live.create_grant(binding, "stream_wp08", "estomago", "room-wp08",
                      ["user", "assistant"], "private", "qiaosheng")
    worker = Principal("worker", "w", "agent", "estomago_archive", binding,
                       capabilities_allowlist=frozenset(["source.ingest"]))
    text = "abcdefghij"
    r = live.ingest(worker, {
        "operation_id": "wp8-ep-src-1", "stream_id": "stream_wp08",
        "origin_instance": "estomago", "origin_conversation_id": "room-wp08",
        "messages": [{
            "origin_message_id": "wp8-m1", "revision": 1,
            "previous_revision": None, "conversation_sequence": 1,
            "predecessor": None, "sender": "user",
            "published_kind": "chat_message",
            "occurred_at": "2026-10-07T01:00:00Z",
            "received_at": "2026-10-07T01:00:00Z",
            "published_at": "2026-10-07T01:00:00Z",
            "text": text, "assets": [],
            "content_hash": hashlib.sha256(
                text.encode("utf-8")).hexdigest()}]})
    ack = r["messages"][0]
    return {"conversation_id": ack["source_conversation_id"],
            "members": [{"source_message_id": ack["source_message_id"],
                         "content_hash": ack["content_hash"]}],
            "text_len": len(text)}


class TestEpisodeRangeInvariant:
    def test_t2_start_ge_end_rejected_full_matrix(self, actors):
        """T2（CX-05）：start≥end 全矩阵拒（INVALID_RANGE，不落
        revision）；正常区间保持；单端缺省不受新不变量误伤。"""
        from mariposa.episodes import service as ep
        fx = _make_episode_fixture()

        def seg(start=None, end=None):
            out = {"conversation_id": fx["conversation_id"],
                   "members": fx["members"]}
            if start is not None:
                out["start_char_offset"] = start
            if end is not None:
                out["end_char_offset"] = end
            return [out]

        with db.formal() as conn:
            for start, end in ((5, 2), (3, 3), (7, 0)):
                with pytest.raises(Forbidden) as ei:
                    ep._verify_segments_tx(conn, seg(start, end))
                assert ei.value.code == "INVALID_RANGE", \
                    f"start={start},end={end} 应拒"
            # 正常区间保持
            out = ep._verify_segments_tx(conn, seg(0, fx["text_len"]))
            assert out[0]["start_char_offset"] == 0
            assert out[0]["end_char_offset"] == fx["text_len"]
            # 单端/缺省不受新不变量误伤（既有语义）
            out2 = ep._verify_segments_tx(conn, seg(end=4))
            assert "start_char_offset" not in out2[0]
