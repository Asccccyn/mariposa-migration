"""复审 Recall/words 域修复回归（RA-012~018）。

- RA-012：撤销来源后 sparse/dense/重放不再签 word_verbatim
  （word_revoke_real_routes——SELECT 缺 source_binding_version）。
- RA-013：memory.words.get 对有来源逐字词不再 closed database。
- RA-014：find_words 顶层数组直接使用；speaker/source_date 生效。
- RA-015：navigate 同 key 重试不再 stale。
- RA-016：多卡正文合计 ≤4000、单卡 ≤600（共享额度）。
- RA-017：memory.search 池截断外露。
"""
from __future__ import annotations

import json

import pytest

from mariposa import config as cfg
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
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="rw",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def _seed_src(text, tag):
    from mariposa.source import importer
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    f = tmp / f"{tag}.json"
    f.write_text(json.dumps([{
        "uuid": f"c-{tag}",
        "chat_messages": [{
            "uuid": f"{tag}-m1", "sender": "human",
            "created_at": "2026-09-28T10:00:00Z",
            "content": [{"type": "text", "text": text}]}]}],
        ensure_ascii=False), encoding="utf-8")
    importer.import_file("jiaming", str(f))
    with db.formal() as conn:
        return conn.execute(
            "SELECT id FROM source_messages WHERE provider_message_id=?",
            (f"{tag}-m1",)).fetchone()["id"]


class TestWordRevokedRoutes:

    def test_sparse_dense_replay_not_verbatim_after_revoke(self, actors):
        msg_id = _seed_src("撤销链路原文", "rvk")
        ref = f"source_msg:{msg_id}"
        w = _hold(actors, "撤销链路话语",
                  our_words=[{"speaker": "qiaosheng", "text": "撤销链路话语",
                              "expression_kind": "verbatim",
                              "source_ref": ref}])
        with db.formal() as conn:
            word_id = conn.execute(
                "SELECT word_id FROM memory_our_words WHERE memory_id=?",
                (w["memory_id"],)).fetchone()["word_id"]
        # 公开撤销（换代 v0→v1）
        registry.invoke(actors["jiaming"], "memory.our_words.source.correct",
                        {"word_id": word_id, "expected_source_ref": ref,
                         "expected_source_version": 0,
                         "correction_action": "remove_wrong_binding",
                         "operation_id": "rvk-rm"}, None)
        # sparse
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start", {
            "query_plan": {"original_request": "撤销链路",
                           "channels": ["words"],
                           "lexical_terms": ["撤销链路"]},
            "operation_id": "rvk-s"}, None)["data"]
        for c in r1.get("candidates", []):
            for ev in c.get("evidence") or []:
                assert ev.get("evidence_kind") != "word_verbatim", \
                    "撤销来源后 sparse 不得再签 verbatim（RA-012）"
        # words.get（RA-013：不崩溃且非 verbatim）
        got = registry.invoke(actors["jiaming"], "memory.words.get",
                              {"word_id": word_id}, None)["data"]
        assert got["evidence"][0]["evidence_kind"] == "word_unverified"
        # 重放旧包（撤销前保存的 start）
        old = registry.invoke(actors["jiaming"], "memory.recall.start", {
            "query_plan": {"original_request": "撤销链路",
                           "channels": ["words"],
                           "lexical_terms": ["撤销链路"]},
            "operation_id": "rvk-old"}, None)
        # 重新绑定来源后再重放旧包（provenance 已换代）
        registry.invoke(actors["jiaming"], "memory.our_words.source.correct",
                        {"word_id": word_id, "expected_source_ref": None,
                         "expected_source_version": 1,
                         "correction_action": "replace_wrong_binding",
                         "replacement": {"source_ref": ref},
                         "operation_id": "rvk-rp"}, None)
        replayed = recall_service.revalidate_replayed(
            "memory.recall.start", old["data"], None)
        for c in replayed.get("candidates", []):
            if c.get("resource_ref", "").startswith("our_word:"):
                for ev in c.get("evidence") or []:
                    assert ev.get("evidence_kind") != "word_verbatim", \
                        "重放按当前 provenance 校验（RA-012 replay 侧）"


class TestFindWordsTopLevel:

    def test_lexical_terms_array_and_speaker(self, actors):
        _hold(actors, "说话人甲话语",
              our_words=[{"speaker": "jiaming", "text": "甲的数组词句",
                          "expression_kind": "paraphrase"}])
        out = registry.invoke(actors["jiaming"], "memory.find_words", {
            "query": "数组词句",
            "lexical_terms": ["数组词句"],
            "explicit_constraints": {"speaker": "qiaosheng"},
            "operation_id": "ra014-a"}, None)["data"]
        assert out["candidates"] == [], \
            "顶层数组直接使用 + speaker 过滤生效（RA-014）"


class TestNavigateReplay:

    def test_same_key_retry_not_stale(self, actors):
        _hold(actors, "导航重试正文")
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start", {
            "query_plan": {"original_request": "导航",
                           "channels": ["event"],
                           "lexical_terms": ["导航"]},
            "operation_id": "ra015-s"}, None)["data"]
        sid = r1["recall_session_id"]
        nav1 = registry.invoke(actors["jiaming"], "memory.recall.navigate", {
            "session_id": sid, "direction": "earlier",
            "operation_id": "ra015-n"}, None)
        assert nav1["ok"] is True
        nav2 = registry.invoke(actors["jiaming"], "memory.recall.navigate", {
            "session_id": sid, "direction": "earlier",
            "operation_id": "ra015-n"}, None)
        assert nav2["ok"] is True, "同 key 无状态变化重试不得 stale（RA-015）"


class TestBudgetAggregate:

    def test_multi_card_total_bounded(self, actors, monkeypatch):
        long_word = "额" * 1500
        for i in range(3):
            _hold(actors, f"预算卡{i}正文",
                  our_words=[{"speaker": "qiaosheng",
                              "text": f"预算{i}{long_word}",
                              "expression_kind": "paraphrase"}])
        out = registry.invoke(actors["jiaming"], "memory.recall.start", {
            "query_plan": {"original_request": "预算",
                           "channels": ["words"],
                           "lexical_terms": ["预算"]},
            "operation_id": "ra016-s"}, None)["data"]

        def body_chars(c):
            n = 0
            for ev in c.get("evidence") or []:
                n += len(ev.get("snippet") or "")
            n += len(c.get("excerpt") or "")
            m = c.get("_matched") or {}
            if isinstance(m, dict):
                n += sum(len(v) for v in m.values()
                         if isinstance(v, str))
            return n
        total = sum(body_chars(c) for c in out.get("candidates", []))
        assert total <= 4000, \
            f"多卡正文合计必须 ≤4000（审计反例 5166）：{total}"
        for c in out.get("candidates", []):
            assert body_chars(c) <= 600 * 1.05, \
                "单卡全部载体共享 600 额度（RA-016）"
        blob = json.dumps(out, ensure_ascii=False).encode("utf-8")
        assert len(blob) <= 24576


class TestSearchPoolTruncation:

    def test_memory_search_reports_pool_truncated(self, actors,
                                                  monkeypatch):
        from mariposa.retrieval import search as search_mod
        _hold(actors, "池截断甲")
        _hold(actors, "池截断乙", categories=["sweet"])
        monkeypatch.setattr(cfg, "RECALL_POOL_MAX_BUCKETS", 1)
        with db.formal() as conn:
            out = search_mod.search(conn, "不存在的关键词xyz")
        assert out.get("pool_truncated") is True, \
            "兼容 search 同样外露池截断（RA-017）"
        assert out.get("coverage") == "partial_pool_cap"
