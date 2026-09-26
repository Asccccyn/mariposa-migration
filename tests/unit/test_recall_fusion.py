"""融合/去重/隔离统计与交付选择（HYBRID-02/03/06 / PACK-01..06）。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service
from mariposa.retrieval import fusion, selection
from mariposa.retrieval import evidence as evidence_mod
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": identity.Principal("jiaming", "周家明", "agent",
                                          "claude_chat", "bj")}


def hold(actors, text, date, cats=("daily",)):
    return memory.hold(actors["jiaming"], text=text, memory_date=date,
                       date_confidence="exact", original_title="t",
                       categories=list(cats),
                       creation_mode="contemporaneous", raw_pending=False)


class TestFusion:
    def test_hybrid03_rrf_1based_no_raw_score_sum(self):
        """HYBRID-03：RRF 用 1-based rank；不直接相加原始分。"""
        lexical = [{"resource_ref": "a", "matched_by": ["keyword"]},
                   {"resource_ref": "b", "matched_by": ["keyword"]}]
        dense = [{"resource_ref": "b", "matched_by": ["semantic"]},
                 {"resource_ref": "c", "matched_by": ["semantic"]}]
        fused = fusion.rrf_fuse({
            "lexical": fusion.family_rank(lexical),
            "dense": fusion.family_rank(dense)}, k=60)
        refs = [f["resource_ref"] for f in fused]
        # b 两族都第 1 → 融合最高；分数是 rank 融合值（约 2/(60+1)），
        # 不是 BM25 原始分与余弦的直接相加
        assert refs[0] == "b"
        # b = lexical rank2 + dense rank1：1/(60+2)+1/(60+1)，不是原始分相加
        assert abs(fused[0]["rrf_score"] - (1 / 62 + 1 / 61)) < 1e-6
        ranks = fusion.family_rank(lexical)
        assert [r["family_rank"] for r in ranks] == [1, 2]

    def test_hybrid02_same_resource_dedup_no_double_vote(self):
        """HYBRID-02：同资源多次命中 family 内只计一个候选，不叠票。"""
        hits = [{"resource_ref": "a", "matched_by": ["keyword"]},
                {"resource_ref": "a", "matched_by": ["keyword"]},
                {"resource_ref": "b", "matched_by": ["keyword"]}]
        ranked = fusion.family_rank(hits)
        assert len(ranked) == 2
        assert ranked[0]["family_rank"] == 1
        assert ranked[0]["matched_by"] == ["keyword"]

    def test_hybrid06_scoped_stats_isolation(self, actors):
        """HYBRID-06：加入 scope 外语料不影响本 scope 可见排名/计数。"""
        hold(actors, "范围内目标事件搬家", "2026-08-10")
        with db.formal() as conn:
            from mariposa.retrieval import query_plan as qp
            where1, params1, _, _ = qp.AllowedScope.for_plan(
                {"explicit_constraints": {
                    "event_date": {"from": "2026-08-01",
                                   "to": "2026-08-31"}}})
            r1 = fusion.scoped_lexical_search(
                conn, where1, params1, [["搬家"]], [])
            # 加入大量 scope 外语料（九月，同词高频出现）
            for i in range(20):
                hold(actors, f"范围外语料搬家搬家{i}", "2026-09-01")
            r2 = fusion.scoped_lexical_search(
                conn, where1, params1, [["搬家"]], [])
        assert [h["row"]["memory_id"] for h in r1["rows"]] == \
            [h["row"]["memory_id"] for h in r2["rows"]]
        assert [h["scoped_score"] for h in r1["rows"]] == \
            pytest.approx([h["scoped_score"] for h in r2["rows"]])
        assert r1["pool_size"] == r2["pool_size"]


class TestSelection:
    def _card(self, ref, evidence_kind="authored_event", **kw):
        ev = [evidence_mod.make_evidence(evidence_kind, "f", "s", ref)]
        card = {"resource_ref": ref, "candidate_ref": ref, "channel": "event",
                "evidence": ev, "rrf_score": 0.5}
        card.update(kw)
        return card

    def test_pack01_deliver_at_most_3(self):
        """PACK-01：后台 40 候选，交付 ≤3。"""
        from mariposa import config
        cards = [self._card(f"memory:m{i}") for i in range(40)]
        out = selection.select(cards, {"delivery_limit": 3}, set())
        assert len(out["delivered"]) <= config.RECALL_DELIVERY_LIMIT
        assert len(cards) == 40  # 后台集合不受交付上限影响

    def test_pack02_single_candidate_needs_validation(self):
        """PACK-02：未校准禁用高置信单条；唯一候选标 needs_validation。"""
        out = selection.select([self._card("memory:m1")],
                               {"delivery_limit": 3}, set())
        assert out["delivery_action"] == "needs_validation"

    def test_pack03_no_duplicate_fills(self):
        """PACK-03：同一资源的重复片段不占满前三。"""
        cards = [self._card("memory:m1#part1"),
                 self._card("memory:m1#part2"),
                 self._card("memory:m1#part3"),
                 self._card("memory:m2")]
        out = selection.select(cards, {"delivery_limit": 3}, set())
        bases = [c["resource_ref"].split("#")[0]
                 for c in out["delivered"]]
        assert bases.count("memory:m1") <= 1

    def test_pack04_conflict_disclosed(self):
        """PACK-04：冲突候选显式披露，不输出表面确定答案。"""
        cards = [self._card("memory:m1"),
                 self._card("memory:m2", conflict_flag=True)]
        out = selection.select(cards, {"delivery_limit": 3}, set(),
                               has_conflict=True)
        assert "memory:m2" in out["conflicts"]
        assert any("冲突" in m for m in out["missing"])

    def test_pack06_truncation_flag(self):
        """PACK-06：截断显式标记。"""
        long_text = "很长的原话内容。" * 200
        snippet, truncated = evidence_mod.excerpt(long_text, limit=100)
        assert truncated is True
        assert len(snippet) <= 101

    def test_rejected_excluded_from_delivery(self):
        """QUERY-04（交付层）：rejected 资源不进交付。"""
        cards = [self._card("memory:m1"), self._card("memory:m2")]
        out = selection.select(cards, {"delivery_limit": 3},
                               {"memory:m1"})
        assert [c["resource_ref"] for c in out["delivered"]] == \
            ["memory:m2"]
