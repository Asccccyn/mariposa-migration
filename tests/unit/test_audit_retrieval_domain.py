"""Recall/检索域审计修复回归（2026-10-02 基线审计 P2 批）。

- CB-039：words 证据等级取决于 provenance 而非命中通道（sparse/dense
  共用构造；source_msg 按当前事实校验；撤销换代不升 verified）。
- CB-040：category_match/mood_match=all 随维度进入统一 scope
  （category_match_all_ignored——交集条件被按 any 执行）。
- CB-041：navigate 复用当前 QueryPlan 范围；words reject 后不再
  SQL 崩溃（navigate_anchor_and_scope / navigate_word_reject_error）。
- CB-042：输出预算覆盖 _matched/excerpt 全部正文载体并做最终
  序列化终检（json_output_budget_exceeded / output_budget_matched_
  word_unbounded）。
- CB-043：兼容检索池安全阀截断状态贯穿（compatibility_pool_
  truncated_lost——零命中与"池未查尽"不可再混淆）。
- CB-047：find_words 顶层过滤/短语/语义参数进入统一 plan
  （find_words_received_arguments——schema 接受的参数不得静默丢弃）。
"""
from __future__ import annotations

import json

import pytest

from mariposa import config as cfg
from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service, store
from mariposa.retrieval import search as search_mod
from mariposa.retrieval.words import _word_evidence
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors, text, categories=("daily",), **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="rv",
                categories=list(categories),
                creation_mode="contemporaneous", raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


# ---------------------------------------------------------------- CB-040

class TestMatchModeAll:

    def test_category_match_all_excludes_partial(self, actors):
        _hold(actors, "仅日常分类的正文")
        out = registry.invoke(actors["jiaming"], "memory.recall.start", {
            "query_plan": {
                "original_request": "日常",
                "channels": ["event"],
                "lexical_terms": ["日常"],
                "explicit_constraints": {
                    "categories": ["daily", "sweet"],
                    "category_match": "all"}},
            "operation_id": "cb040-all"}, None)["data"]["data"]
        assert out["candidates"] == [], \
            "category_match=all 的交集条件必须实际执行（反例：曾按 any 交付）"

    def test_category_match_any_still_or(self, actors):
        _hold(actors, "任一即中的正文")
        out = registry.invoke(actors["jiaming"], "memory.recall.start", {
            "query_plan": {
                "original_request": "任一",
                "channels": ["event"],
                "lexical_terms": ["任一"],
                "explicit_constraints": {
                    "categories": ["sweet"],
                    "category_match": "any"}},
            "operation_id": "cb040-any"}, None)["data"]["data"]
        assert out["candidates"] == [], "any 不误伤（sweet 不含 daily）"


# ---------------------------------------------------------------- CB-043

class TestPoolTruncationSurface:

    def test_pool_truncated_visible_in_keyword_mode(self, actors,
                                                     monkeypatch):
        _hold(actors, "甲桶关键词目标内容")
        out2 = memory.hold(actors["jiaming"], text="乙桶不同日期正文",
                           memory_date="2026-09-20",
                           date_confidence="exact", original_title="乙",
                           categories=["sweet"],
                           creation_mode="contemporaneous",
                           raw_pending=False)
        assert out2
        monkeypatch.setattr(cfg, "RECALL_POOL_MAX_BUCKETS", 1)
        with db.formal() as conn:
            res = search_mod._recall_keyword(
                conn, "关键词", ["m.visibility='active'"], [], {}, 20,
                None)
        assert res.get("pool_truncated") is True, \
            "安全阀触顶必须显式外露（不再与零命中混淆）"
        assert res.get("coverage") == "partial_pool_cap"


# ---------------------------------------------------------------- CB-047

class TestFindWordsTopLevelParams:

    def test_top_level_constraints_and_phrases_apply(self, actors):
        _hold(actors, "过滤目标话语",
              our_words=[{"speaker": "qiaosheng", "text": "稀有原话目标",
                          "expression_kind": "paraphrase"}])
        # 顶层 exact_phrases 不匹配任何话语 → 零候选（此前被丢弃会
        # 全量浏览交付）
        out = registry.invoke(actors["jiaming"], "memory.find_words", {
            "query": "原话",
            "exact_phrases": ["完全不存在的话"],
            "operation_id": "cb047-a"}, None)
        data = out["data"]["data"] if out["data"].get("data") \
            else out["data"]
        assert data["candidates"] == [], \
            "顶层 exact_phrases 必须进入统一 plan（不得静默丢弃）"

    def test_top_level_categories_rejected_not_dropped(self, actors):
        """words 通道白名单不含 categories（现行规格）——schema 接受
        的字段必须要么生效要么结构化拒绝，不得静默丢弃后全量检索。"""
        _hold(actors, "日常桶话语",
              our_words=[{"speaker": "qiaosheng", "text": "日常桶的话语",
                          "expression_kind": "paraphrase"}])
        with pytest.raises(Forbidden):
            registry.invoke(actors["jiaming"], "memory.find_words", {
                "query": "话语",
                "explicit_constraints": {"categories": ["sweet"]},
                "operation_id": "cb047-b"}, None)


# ---------------------------------------------------------------- CB-041

class TestNavigateScopeAndReject:

    def test_navigate_respects_plan_scope(self, actors):
        _hold(actors, "范围锚点桶", categories=("sweet",))
        other = memory.hold(
            actors["jiaming"], text="更早的日常桶",
            memory_date="2026-09-20", date_confidence="exact",
            original_title="d", categories=["daily"],
            creation_mode="contemporaneous", raw_pending=False)
        assert other
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start", {
            "query_plan": {"original_request": "范围",
                           "channels": ["event"],
                           "lexical_terms": ["范围"],
                           "explicit_constraints": {
                               "categories": ["sweet"]}},
            "operation_id": "cb041-s"}, None)["data"]["data"]
        sid = r1["recall_session_id"]
        anchor = r1["candidates"][0]["candidate_ref"] if r1[
            "candidates"] else None
        assert anchor, "前置：sweet 范围有锚点"
        nav = registry.invoke(actors["jiaming"], "memory.recall.navigate", {
            "session_id": sid, "direction": "earlier",
            "anchor_candidate_ref": anchor,
            "operation_id": "cb041-n"}, None)["data"]["data"]
        cats = []
        with db.formal() as conn:
            for c in nav.get("candidates", []):
                row = conn.execute(
                    "SELECT memory_id FROM memories WHERE memory_id=?",
                    (c["resource_ref"].split(":")[-1],)).fetchone()
                mid = row["memory_id"]
                cats += [r["category"] for r in conn.execute(
                    "SELECT category FROM memory_categories WHERE"
                    " memory_id=?", (mid,))]
        assert not cats or set(cats) == {"sweet"}, \
            f"导航必须沿当前查询范围（daily 不得混入）：{cats}"

    def test_navigate_after_word_reject_no_crash(self, actors):
        w = _hold(actors, "拒绝导航话语",
                  our_words=[{"speaker": "qiaosheng", "text": "要拒绝的话",
                              "expression_kind": "paraphrase"}])
        with db.formal() as conn:
            word_id = conn.execute(
                "SELECT word_id FROM memory_our_words WHERE memory_id=?",
                (w["memory_id"],)).fetchone()["word_id"]
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start", {
            "query_plan": {"original_request": "拒绝",
                           "channels": ["words"],
                           "lexical_terms": ["拒绝"]},
            "operation_id": "cb041w-s"}, None)["data"]["data"]
        sid = r1["recall_session_id"]
        registry.invoke(actors["jiaming"], "memory.recall.reject", {
            "session_id": sid, "candidate_ref": f"our_word:{word_id}",
            "reject_target": "word",
            "operation_id": "cb041w-r"}, None)
        # 审计反例：混合 rejected 集合曾造成占位符/绑定数不匹配崩溃
        out = registry.invoke(actors["jiaming"], "memory.recall.navigate", {
            "session_id": sid, "direction": "earlier",
            "operation_id": "cb041w-n"}, None)
        assert out["ok"] is True


# ---------------------------------------------------------------- CB-039

class TestWordEvidenceProvenance:

    def _source_msg(self, tag="ev", published=True):
        from mariposa.source import importer
        import tempfile, pathlib
        tmp = pathlib.Path(tempfile.mkdtemp())
        f = tmp / f"{tag}.json"
        f.write_text(json.dumps([{
            "uuid": f"c-{tag}",
            "chat_messages": [{
                "uuid": f"{tag}-m1", "sender": "human",
                "created_at": "2026-09-28T10:00:00Z",
                "content": [{"type": "text", "text": f"{tag} 原文"}]}]}],
            ensure_ascii=False), encoding="utf-8")
        importer.import_file("jiaming", str(f))
        with db.formal() as conn:
            return conn.execute(
                "SELECT id FROM source_messages WHERE"
                " provider_message_id=?", (f"{tag}-m1",)).fetchone()["id"]

    def test_published_source_validates_verbatim(self):
        msg_id = self._source_msg("oksrc")
        with db.formal() as conn:
            ev = _word_evidence({
                "word_id": "ow_x", "expression_kind": "verbatim",
                "source_ref": f"source_msg:{msg_id}",
                "source_binding_version": 0,
                "text": "有来源的原话", "current_version_no": 1}, conn)
        assert ev[0]["evidence_kind"] == "word_verbatim", \
            "真实已发布来源不得被降级（此前 _source_ref_valid 恒 False）"

    def test_invalid_source_downgrades(self):
        with db.formal() as conn:
            ev = _word_evidence({
                "word_id": "ow_y", "expression_kind": "verbatim",
                "source_ref": "source_msg:missing-id",
                "source_binding_version": 0,
                "text": "失效来源原话", "current_version_no": 1}, conn)
        assert ev[0]["evidence_kind"] == "word_unverified"

    def test_revoked_source_not_upgraded(self):
        """撤销换代（gen>0 且无来源）保留撤销状态，不按从未有来源
        的逐字声明路径签 verified。"""
        with db.formal() as conn:
            ev = _word_evidence({
                "word_id": "ow_z", "expression_kind": "verbatim",
                "source_ref": None,
                "source_binding_version": 2,
                "text": "被撤销来源的原话", "current_version_no": 1},
                conn)
        assert ev[0]["evidence_kind"] == "word_unverified"
        states = [e.get("structured_value") for e in ev
                  if e.get("evidence_kind") == "structured_fact"]
        assert states and states[0].get("source_ref_state") == "revoked"

    def test_native_no_source_verbatim_unchanged(self):
        with db.formal() as conn:
            ev = _word_evidence({
                "word_id": "ow_n", "expression_kind": "verbatim",
                "source_ref": None, "source_binding_version": 0,
                "text": "原生无来源原话", "current_version_no": 1}, conn)
        assert ev[0]["evidence_kind"] == "word_verbatim", \
            "从未有来源的正式逐字声明保持原语义"


# ---------------------------------------------------------------- CB-042

class TestOutputBudgetCarriers:

    def test_long_word_text_bounded_in_packet(self, actors):
        long_word = "长" * 9000
        _hold(actors, "预算载体正文",
              our_words=[{"speaker": "qiaosheng",
                          "text": f"预算{long_word}话语",
                          "expression_kind": "paraphrase"}])
        out = registry.invoke(actors["jiaming"], "memory.recall.start", {
            "query_plan": {"original_request": "预算",
                           "channels": ["words"],
                           "lexical_terms": ["预算"]},
            "operation_id": "cb042-a"}, None)["data"]["data"]
        blob = json.dumps(out, ensure_ascii=False).encode("utf-8")
        assert len(blob) <= 24576, \
            f"完整 packet 必须受字节预算终检：{len(blob)}"
        for c in out.get("candidates", []):
            m = c.get("_matched") or {}
            for v in (m.values() if isinstance(m, dict) else []):
                if isinstance(v, str):
                    assert len(v) <= 600, \
                        "_matched 载体受单候选窗约束"
            if isinstance(c.get("excerpt"), str):
                assert len(c["excerpt"]) <= 600
