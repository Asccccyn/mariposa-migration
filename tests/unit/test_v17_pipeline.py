"""v1.7 P3 管线测试：分字段投影 + 阶段过滤 + Round2 gate + find_words。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.errors import Forbidden
from mariposa.recall import pipeline as pl
from mariposa.recall import phase_policy as pp
from mariposa.retrieval import field_projection as fp
from mariposa.memory import service as memory

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def hold(actors, text, title=None, cats=("daily",), days_ago=0, words=None):
    out = memory.hold(
        actors["jiaming"], text=text, original_title=title,
        categories=list(cats), creation_mode="contemporaneous",
        memory_date="2026-08-01", our_words=words)
    if days_ago:
        with db.formal() as c:
            c.execute("UPDATE memories SET held_at=? WHERE memory_id=?",
                      ((NOW - timedelta(days=days_ago)).isoformat(),
                       out["memory_id"]))
    return out["memory_id"]


class TestFieldProjection:
    def test_hold_builds_field_docs(self, actors):
        mid = hold(actors, "事件正文甲", title="标题甲",
                   words=[{"speaker": "qiaosheng", "text": "话语甲",
                           "expression_kind": "verbatim"}])
        with db.formal() as c:
            kinds = {r["field_kind"] for r in c.execute(
                "SELECT field_kind FROM field_search_docs WHERE memory_id=?",
                (mid,))}
        assert kinds == {"original_title", "event_text", "our_words"}

    def test_mood_text_never_indexed(self, actors):
        out = memory.hold(
            actors["jiaming"], text="正文乙", categories=["sweet"],
            mood={"text": "没说出口的心事秘密词", "tags": ["开心"]},
            memory_date="2026-08-01")
        with db.formal() as c:
            norm = " ".join(r["text_norm"] for r in c.execute(
                "SELECT text_norm FROM field_search_docs WHERE memory_id=?",
                (out["memory_id"],)))
        assert "秘密词" not in norm  # mood_text 不进任何投影

    def test_rebuild_all_idempotent(self, actors):
        hold(actors, "重建正文", title="重建标题")
        r1 = fp.rebuild_all()
        r2 = fp.rebuild_all()
        assert r1["rebuilt"] >= 2 and r2["rebuilt"] >= 2


class TestStageFilteredRound1:
    def test_core_excludes_title_and_words(self, actors):
        # 61 天前（H=20 → 2H=40，D=61 ≥ 40 → CORE）：标题/words 词不命中
        hold(actors, "无关正文", title="独有标题词犽", days_ago=61,
             words=[{"speaker": "qiaosheng", "text": "独有话语词鸢",
                     "expression_kind": "verbatim"}])
        with db.formal() as conn:
            out = pl.round1_candidates(conn, {"lexical_terms": ["犽"]})
            assert out["candidates"] == []  # CORE 无 title
            out = pl.round1_candidates(conn, {"lexical_terms": ["鸢"]})
            assert out["candidates"] == []  # CORE 无 words

    def test_wide_includes_title_and_words(self, actors):
        hold(actors, "普通正文", title="新近标题词犽", days_ago=0,
             words=[{"speaker": "qiaosheng", "text": "新近话语词鸢",
                     "expression_kind": "verbatim"}])
        with db.formal() as conn:
            assert pl.round1_candidates(conn, {"lexical_terms": ["犽"]})["candidates"]
            assert pl.round1_candidates(conn, {"lexical_terms": ["鸢"]})["candidates"]

    def test_event_text_searchable_all_stages(self, actors):
        hold(actors, "久远事件词曦", days_ago=100)
        with db.formal() as conn:
            out = pl.round1_candidates(conn, {"lexical_terms": ["曦"]})
            assert out["candidates"]  # CORE 仍可事件检索

    def test_stage_stats_reported(self, actors):
        hold(actors, "统计正文", days_ago=0)
        hold(actors, "久远正文", days_ago=100)
        with db.formal() as conn:
            out = pl.round1_candidates(conn, {"lexical_terms": ["不存在词"]})
        assert out["stage_field_stats"]["WIDE"] >= 1
        assert out["stage_field_stats"]["CORE"] >= 1


class TestRound2Gate:
    def _session(self, actors):
        from mariposa.recall import service as rs
        hold(actors, "gate 正文", days_ago=0)
        packet = rs.start(actors["jiaming"], {"query_plan": {
            "original_request": "gate", "channels": ["event"],
            "lexical_terms": ["gate"]}})
        return packet

    def test_round1_receipt_auto_issued_by_start(self, actors):
        """A05：首轮真实执行完成自动签发 ROUND1_COMPLETE（不再依赖手工）。"""
        from mariposa.recall import store as st
        packet = self._session(actors)
        sess = st.require_session(packet["recall_session_id"])
        gate = pl.round2_gate(sess, sess["current_revision"],
                              "EVIDENCE_INSUFFICIENT", "complete",
                              True, True)
        assert gate["gate"]["round1_complete_receipt"] is True

    def test_gate_allows_with_full_conditions(self, actors):
        from mariposa.recall import store as st
        packet = self._session(actors)
        sid = packet["recall_session_id"]
        sess = st.require_session(sid)
        gate = pl.round2_gate(sess, sess["current_revision"],
                              "EVIDENCE_INSUFFICIENT", "complete",
                              True, True)
        assert gate["allowed"] is True

    def test_bad_reason_blocks(self, actors):
        from mariposa.recall import store as st
        packet = self._session(actors)
        sid = packet["recall_session_id"]
        sess = st.require_session(sid)
        gate = pl.round2_gate(sess, sess["current_revision"],
                              "JUDGE_UNAVAILABLE", "complete", True, True)
        assert gate["allowed"] is False

    def test_registry_round2_denies_without_gate(self, actors):
        from mariposa.capabilities import registry
        from mariposa.recall import service as rs
        hold(actors, "reg 正文", days_ago=0)
        packet = rs.start(actors["jiaming"], {"query_plan": {
            "original_request": "reg", "channels": ["event"],
            "lexical_terms": ["reg"]}})
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.recall.round2", {
                "session_id": packet["recall_session_id"],
                "reason": "EVIDENCE_INSUFFICIENT"}, None)
        assert ei.value.code == "ROUND2_GATE_DENIED"

    def test_raw_deep_search_hits_published_source(self, actors, tmp_path):
        import json as _json
        from mariposa.source import importer
        conv = {"uuid": "c-r2", "chat_messages": [
            {"uuid": "r2-1", "sender": "human",
             "created_at": "2026-07-01T00:00:00.000Z",
             "content": [{"type": "text", "text": "深搜目标词旌旗"}]}]}
        p = tmp_path / "r2.json"
        p.write_text(_json.dumps([conv], ensure_ascii=False), encoding="utf-8")
        importer.import_file("jiaming", str(p))
        out = pl.raw_deep_search(actors["jiaming"],
                                 {"lexical_terms": ["旌旗"]})
        assert out["hits"] and out["hits"][0]["evidence_kind"] == "raw_verbatim"


class TestFindWords:
    def test_cross_stage_words(self, actors):
        memory.hold(
            actors["jiaming"], text="久远事件正文", categories=["daily"],
            memory_date="2026-08-01",
            our_words=[{"speaker": "qiaosheng", "text": "跨阶段话语词翎",
                        "expression_kind": "verbatim"}])
        with db.formal() as c:
            c.execute("UPDATE memories SET held_at=?",
                      ((NOW - timedelta(days=100)).isoformat(),))
            from mariposa.retrieval import words as wm
            wm.rebuild_words_index(c)
        with db.formal() as conn:
            out = pl.find_words_candidates(
                conn, {"lexical_terms": ["翎"]})
        assert out["words_hits"]
        assert out["stage_restricted"] is False  # 跨阶段
