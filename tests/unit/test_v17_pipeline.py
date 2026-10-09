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
    # 2026-10-05 标题必填：未显式给标题时用正文前 8 字作提要
    out = memory.hold(
        actors["jiaming"], text=text, original_title=title or text[:8],
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
            creation_mode="contemporaneous",  # 1005B-R4：带心情须显式声明
            memory_date="2026-08-01", original_title="测试标题")
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
    """WP-05/D06：死适配层 round1_candidates 已删——阶段过滤语义改经现行
    路径 round1_lexical_hits（scope-stage-bm25，S06）验证。"""

    def test_core_excludes_title_and_words(self, actors):
        # 61 天前（H=20 → 2H=40，D=61 ≥ 40 → CORE）：标题/words 词不命中
        hold(actors, "无关正文", title="独有标题词犽", days_ago=61,
             words=[{"speaker": "qiaosheng", "text": "独有话语词鸢",
                     "expression_kind": "verbatim"}])
        with db.formal() as conn:
            out = pl.round1_lexical_hits(conn, {"lexical_terms": ["犽"]},
                                         set(), {})
            assert out == []  # CORE 无 title
            out = pl.round1_lexical_hits(conn, {"lexical_terms": ["鸢"]},
                                         set(), {})
            assert out == []  # CORE 无 words

    def test_wide_includes_title_and_words(self, actors):
        hold(actors, "普通正文", title="新近标题词犽", days_ago=0,
             words=[{"speaker": "qiaosheng", "text": "新近话语词鸢",
                     "expression_kind": "verbatim"}])
        with db.formal() as conn:
            assert pl.round1_lexical_hits(conn, {"lexical_terms": ["犽"]},
                                          set(), {})
            assert pl.round1_lexical_hits(conn, {"lexical_terms": ["鸢"]},
                                          set(), {})

    def test_event_text_searchable_all_stages(self, actors):
        hold(actors, "久远事件词曦", days_ago=100)
        with db.formal() as conn:
            out = pl.round1_lexical_hits(conn, {"lexical_terms": ["曦"]},
                                         set(), {})
            assert out  # CORE 仍可事件检索

    # stage_field_stats 内部统计随死适配层删除（D06）；阶段分布语义由
    # phase_policy 单测覆盖


class TestRound2Gate:
    """WP-05/D06：死适配层 pl.round2_gate 已删——gate 语义改经现行
    round2._round2_gate（服务端全条件核验）验证。"""

    def _gate(self, actors, reason, evidence_requirement=None):
        """真实 start + 现行 _round2_gate。默认 fixture 带
        verbatim_required（首轮事实 req_met=False →
        VERBATIM_REQUIRED_NOT_MET/EVIDENCE_INSUFFICIENT 有服务端事实支持，
        RRA-011：正例必须真实满足全部 gate 条件）。"""
        from mariposa.recall import round2 as r2
        from mariposa.recall import service as rs, store as st
        from mariposa import db as _db
        hold(actors, "gate 正文", days_ago=0)
        plan = {"original_request": "gate", "channels": ["event"],
                "lexical_terms": ["gate"]}
        if evidence_requirement:
            plan["evidence_requirement"] = evidence_requirement
        packet = rs.start(actors["jiaming"], {"query_plan": plan})
        sess = st.require_session(packet["recall_session_id"])
        with _db.recall_runtime() as conn:
            allowed, gate = r2._round2_gate(conn, sess, reason)
            return {"allowed": allowed, **gate}

    def test_round1_receipt_auto_issued_by_start(self, actors):
        """A05：首轮真实执行完成自动签发 ROUND1_COMPLETE。"""
        gate = self._gate(actors, "VERBATIM_REQUIRED_NOT_MET",
                          evidence_requirement="verbatim_required")
        assert gate["round1_receipt"] is True

    def test_gate_allows_with_full_conditions(self, actors):
        """RRA-011 修复：完整条件正例恢复 assert allowed is True——
        fixture 真实满足服务端事实门（verbatim_required 首轮 req_met=
        False → 理由有事实支持），不再只测理由属于闭集。"""
        gate = self._gate(actors, "VERBATIM_REQUIRED_NOT_MET",
                          evidence_requirement="verbatim_required")
        assert gate["reason_in_closed_set"] is True
        assert gate["round1_receipt"] is True
        assert gate["reason_fact_supported"] is True, \
            f"fixture 必须真实满足事实门（gate={gate}）"
        assert gate["allowed"] is True, \
            f"完整条件正例必须实际放行（gate={gate}）"

    def test_bad_reason_blocks(self, actors):
        gate = self._gate(actors, "JUDGE_UNAVAILABLE")
        assert gate["reason_in_closed_set"] is False
        assert gate["allowed"] is False

    def test_registry_round2_denies_without_gate(self, actors):
        from mariposa.capabilities import registry
        from mariposa.recall import service as rs
        hold(actors, "reg 正文", days_ago=0)
        packet = rs.start(actors["jiaming"], {"query_plan": {
            "original_request": "reg", "channels": ["event"],
            "lexical_terms": ["reg"]}})
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.recall.round2", { "operation_id": "op-auto-test_v-0",
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
        """WP-05/D06：死层 find_words_candidates 已删——跨阶段全量语义
        由现行 retrieval.words.words_search 验证（find_words 主线同源）。"""
        memory.hold(
            actors["jiaming"], text="久远事件正文", categories=["daily"],
            memory_date="2026-08-01",
            our_words=[{"speaker": "qiaosheng", "text": "跨阶段话语词翎",
                        "expression_kind": "verbatim"}], original_title="测试标题")
        with db.formal() as c:
            c.execute("UPDATE memories SET held_at=?",
                      ((NOW - timedelta(days=100)).isoformat(),))
            from mariposa.retrieval import words as wm
            wm.rebuild_words_index(c)
        with db.formal() as conn:
            from mariposa.retrieval import words as wm
            res = wm.words_search(conn, {"lexical_terms": ["翎"]}, 30)
            assert res.get("hits"), "CORE 阶段 our_words 仍可专项检索（跨阶段）"
