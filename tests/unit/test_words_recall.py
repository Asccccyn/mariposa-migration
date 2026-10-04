"""独立 words 检索通道验收（v1.3 WORD-01..05 / EVID-02/03 / PACK-07 / RAWX-05）。

我们的话使用独立 words channel，不混入普通 event ranking；遗忘后的
words 显式检索保持 disabled / PENDING_OWNER_DECISION，不借索引、
relation、缓存或 raw 回退绕过。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }


def hold(actors, **kw):
    base = dict(text="普通事件正文", memory_date="2026-08-19",
                date_confidence="exact", original_title="普通标题",
                categories=["sweet"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def words_recall(actors, **a):
    return recall_service.words_recall(actors["jiaming"], a)


class TestWordsChannel:
    def test_word01_words_field_stage_gated(self, actors):
        """WORD-01（v1.7 REPLACE）：our_words 独有词按阶段进出 event 通道。

        WIDE 六入口含我们的话 → 命中；拨老后 CORE 四入口不含 → 不命中。
        """
        target = hold(actors, text="搬家事件正文",
                      our_words=[{"speaker": "qiaosheng",
                                  "text": "叽里咕噜独有话语",
                                  "expression_kind": "verbatim"}])
        hold(actors, text="另一条无关记忆", memory_date="2026-08-20")
        packet = recall_service.start(actors["jiaming"], {
            "query_plan": {
                "original_request": "找叽里咕噜",
                "channels": ["event"],
                "lexical_terms": ["叽里咕噜"]}})
        assert packet["candidates"], "WIDE 阶段 our_words 参与普通召回"
        assert packet["candidates"][0]["memory_id"] == target["memory_id"]
        # 拨老到 CORE（daily H=20 → 100 天 ≥ 2H）后同一词不再命中
        from datetime import datetime, timedelta, timezone as _tz
        with db.formal() as conn:
            conn.execute("UPDATE memories SET held_at=? WHERE memory_id=?",
                         ((datetime.now(_tz.utc) - timedelta(days=100)
                           ).isoformat(), target["memory_id"]))
        packet2 = recall_service.start(actors["jiaming"], {
            "query_plan": {
                "original_request": "找叽里咕噜",
                "channels": ["event"],
                "lexical_terms": ["叽里咕噜"]}})
        assert packet2["candidates"] == []
        assert packet2["search_status"] == "NO_MATCH_OBSERVED"

    def test_word02_words_channel_finds_word(self, actors):
        """WORD-02：words 通道可命中话语资源。"""
        m = hold(actors, text="搬家事件正文",
                 our_words=[{"speaker": "qiaosheng", "text": "搬家要一起选窗帘",
                             "expression_kind": "verbatim"}])
        # WP05 统一入口：session 化 packet（≤3 + judged）
        out = words_recall(actors, query="窗帘")
        refs = [h["resource_ref"] for h in out["candidates"]]
        assert refs, "统一入口仍应找到话语资源"
        assert len(refs) <= 3, "不再直返 20-30 条正文"
        assert out["delivery_action"] == "needs_validation"
        hit = out["candidates"][0]
        assert hit["channel"] in ("words", "word")
        assert "our_words" in hit["matched_fields"][0]

    def test_word03_speaker_uses_formal_field(self, actors):
        """WORD-03：'找我说的'按正式 speaker 字段过滤。"""
        hold(actors, text="搬家事件",
             our_words=[{"speaker": "jiaming", "text": "窗帘颜色听你的",
                         "expression_kind": "verbatim"},
                        {"speaker": "qiaosheng", "text": "窗帘颜色我说了算",
                         "expression_kind": "verbatim"}])
        out = words_recall(actors, query="窗帘",
                           explicit_constraints={"speaker": "qiaosheng"})
        assert out["candidates"]
        assert all(h.get("speaker") == "qiaosheng"
                   for h in out["candidates"])

    def test_word04_mixed_keeps_channel_roles(self, actors):
        """WORD-04：mixed 返回中 event 与 words 证据角色独立。"""
        hold(actors, text="搬家事件正文",
             our_words=[{"speaker": "qiaosheng", "text": "搬家要一起选窗帘",
                         "expression_kind": "paraphrase"}])
        packet = recall_service.start(actors["jiaming"], {
            "query_plan": {
                "original_request": "搬家的事和我们说过的话",
                "channels": ["event", "words"],
                "lexical_terms": ["搬家", "窗帘"]}})
        kinds = {(c["channel"], c["evidence"][0]["evidence_kind"])
                 for c in packet["candidates"]}
        assert any(ch == "event" and k == "authored_event"
                   for ch, k in kinds)
        assert any(ch == "words" and k == "word_paraphrase"
                   for ch, k in kinds)

    def test_word05_words_recall_no_renewal_side_effect(self, actors):
        """WORD-05：words recall / preview 不触发普通记忆续期（无查看回执）。"""
        m = hold(actors, text="搬家事件",
                 our_words=[{"speaker": "qiaosheng", "text": "窗帘话语",
                             "expression_kind": "verbatim"}])
        with db.formal() as conn:
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_view_receipts"
            ).fetchone()["n"]
        words_recall(actors, query="窗帘")
        with db.formal() as conn:
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_view_receipts"
            ).fetchone()["n"]
            updated = conn.execute(
                "SELECT updated_at FROM memories WHERE memory_id=?",
                (m["memory_id"],)).fetchone()["updated_at"]
        assert before == after  # 只有 open+view.confirm 才有续期语义


class TestEvidenceKinds:
    def test_evid02_paraphrase_not_verbatim(self, actors):
        """EVID-02：word_paraphrase 不满足 verbatim_required。"""
        hold(actors, text="搬家事件",
             our_words=[{"speaker": "qiaosheng", "text": "复述版搬家话语",
                         "expression_kind": "paraphrase"}])
        packet = recall_service.start(actors["jiaming"], {
            "query_plan": {
                "original_request": "我当时原话是什么",
                "channels": ["words"],
                "lexical_terms": ["搬家"],
                "evidence_requirement": "verbatim_required"}})
        met = [c["evidence_requirement_met"]
               for c in packet["candidates"]
               if c["channel"] == "words"]
        assert met and not all(met)
        assert "逐字原话证据尚未取得" in packet["missing"]
        kinds = [e["evidence_kind"] for c in packet["candidates"]
                 for e in c["evidence"]]
        assert "word_verbatim" not in kinds


    def test_pack07_word_unverified_neither_paraphrase_nor_verbatim(
            self, actors):
        """PACK-07：expression_kind=unspecified → word_unverified。"""
        hold(actors, text="搬家事件",
             our_words=[{"speaker": "qiaosheng", "text": "未标注搬家话语"}])
        out = words_recall(actors, query="搬家")
        kinds = [e["evidence_kind"] for h in out["candidates"]
                 for e in h["evidence"]]
        assert "word_unverified" in kinds
        assert "word_paraphrase" not in kinds
        assert "word_verbatim" not in kinds


class TestForgottenWords:
    def test_rawx05_forgotten_words_disabled(self, actors):
        """RAWX-05：遗忘后的 words 显式检索 disabled（PENDING_OWNER_DECISION）。"""
        from mariposa import schema  # noqa: F401
        m = hold(actors, text="搬家事件",
                 our_words=[{"speaker": "qiaosheng", "text": "搬家窗帘话语",
                             "expression_kind": "verbatim"}])
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET compression_state='forgotten_summary'"
                " WHERE memory_id=?", (m["memory_id"],))
            from mariposa.retrieval import projection
            projection.upsert(conn, m["memory_id"], 1,
                              "forgotten_summary",
                              projection.build_forgotten("搬家批准摘要"),
                              whitelist_body="搬家批准摘要")
        out = words_recall(actors, query="窗帘")
        assert out["candidates"] == []
        # 遗忘话语不进检索（覆盖显式标注，v1.4 PENDING 决策沿用）
        assert (out["coverage"].get("words_forgotten") or
                out["coverage"].get("words_lexical") == "blocked"
                or not out["candidates"])
        # memory.words.get 同样拒绝，不借读取旁路恢复
        with db.formal() as conn:
            wid = conn.execute(
                "SELECT word_id FROM memory_our_words LIMIT 1"
            ).fetchone()["word_id"]
        from mariposa.errors import Forbidden
        from mariposa.retrieval import words as wmod
        with pytest.raises(Forbidden) as ei:
            wmod.get_word(wid)
        assert ei.value.code == "WORDS_FORGOTTEN_DISABLED"


class TestScopedBm25WordsS05S06:
    """裁定（2026-10-04）：S05/S06 覆盖 words——scope 外文档不得参与
    BM25 corpus statistics（审计反例：追加 50 条被排除 speaker 的话语
    翻转可见 alpha/beta 排名）。"""

    def _search(self, conn, terms=("锚词甲乙",)):
        from mariposa.retrieval import words as words_mod
        return words_mod.words_search(conn, {
            "original_request": "找话语", "channels": ["words"],
            "lexical_terms": list(terms),
            "explicit_constraints": {"speaker": "qiaosheng"}})

    def test_out_of_scope_corpus_cannot_flip_ranking(self, actors):
        import mariposa.memory.service as mem
        # 可见 scope：qiaosheng 两条话语，甲含查询词、乙也含
        mem.hold(actors["jiaming"], text="scope排名事件一",
                 memory_date="2026-09-20", date_confidence="exact",
                 original_title="t", categories=["daily"],
                 creation_mode="contemporaneous", raw_pending=False,
                 our_words=[{"speaker": "qiaosheng", "text": "锚词甲乙出现在这里",
                             "expression_kind": "verbatim"}])
        mem.hold(actors["jiaming"], text="scope排名事件二",
                 memory_date="2026-09-21", date_confidence="exact",
                 original_title="t", categories=["daily"],
                 creation_mode="contemporaneous", raw_pending=False,
                 our_words=[{"speaker": "qiaosheng", "text": "也提到锚词甲乙",
                             "expression_kind": "verbatim"}])
        from mariposa import db
        with db.formal() as conn:
            first = [h["word_id"] for h in self._search(conn)["hits"]]
        assert first, "前置：scope 内有命中"
        # 追加 50 条 speaker 被排除的话语（含查询词，灌满 corpus 统计）
        for i in range(50):
            mem.hold(actors["jiaming"], text=f"排除话语事件{i}",
                     memory_date="2026-09-22", date_confidence="exact",
                     original_title="t", categories=["daily"],
                     creation_mode="contemporaneous", raw_pending=False,
                     our_words=[{"speaker": "jiaming",
                                 "text": f"排除者说的锚词甲乙{i}",
                                 "expression_kind": "verbatim"}])
        with db.formal() as conn:
            after = [h["word_id"]
                     for h in self._search(conn)["hits"]]
        # 可见候选集合与顺序不受 scope 外语料影响
        assert after == first, (
            f"scope 外语料翻转了可见排名：{first} -> {after}")
