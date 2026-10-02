"""全量审计（2026-10-01）第三批 P2-01..06 验收测试。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.errors import Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service
from mariposa.retrieval.judges import base as jb
from mariposa.retrieval.judges import typesafe_jev
from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(principal, text, title, date, **kw):
    return memory.hold(
        principal, text=text, memory_date=date, date_confidence="exact",
        original_title=title, categories=["daily"],
        creation_mode="contemporaneous", raw_pending=False, **kw)


class TestP201StageBeforeTopK:
    def test_core_title_hits_do_not_starve_event_hit(self, actors):
        """P2-01 反例：大量 CORE 桶的 title 命中先占满取数窗，后面的
        合法 event_text 永远进不了候选。修后阶段过滤在打分之前。"""
        from mariposa.retrieval import search as rsearch
        # 15 个久远桶：标题含"灯河"（CORE 阶段不允许 title）
        for i in range(15):
            out = _hold(actors["jiaming"], f"无关正文第{i}号",
                        f"灯河旧梦{i}", "2025-01-10")
            with db.formal() as conn:
                conn.execute(
                    "UPDATE memories SET held_at='2025-01-10T00:00:00+00:00'"
                    " WHERE memory_id=?", (out["memory_id"],))
        # 1 个新桶：正文含"灯河"（WIDE 全字段允许）
        good = _hold(actors["jiaming"], "今晚在灯河边散步很久",
                     "平常一夜", "2026-09-28")
        with db.formal() as conn:
            res = rsearch.search(conn, "灯河", limit=3)
        mids = [h["memory_id"] for h in res["hits"]]
        assert good["memory_id"] in mids, \
            f"合法 event_text 命中必须进入候选（fetch 窗内）：{mids}"
        for h in res["hits"]:
            if h["memory_id"] != good["memory_id"]:
                assert "original_title" not in h.get(
                    "matched_fields", []), "CORE 桶 title 命中不得交付"


class TestP202NegativeDates:
    def test_to_only_negative_rejected(self, actors):
        """{to:...} 负向区间：validator 显式拒绝（不静默忽略）。"""
        _hold(actors["jiaming"], "正文", "t", "2026-09-25")
        with pytest.raises(Forbidden) as ei:
            recall_service.start(actors["jiaming"], {"query_plan": {
                "original_request": "查", "channels": ["event"],
                "lexical_terms": ["查"],
                "explicit_negative_constraints": {
                    "event_date_excluded": [{"to": "2026-09-01"}]}}})
        assert ei.value.code == "INVALID_ARGUMENT"

    def test_occurred_interval_negative_symmetric(self, actors):
        """memory_date 为空、occurred 区间落在排除窗内 → 必须被排除
        （此前只看 memory_date NOT BETWEEN，这类桶漏排）。"""
        out = memory.hold(
            actors["jiaming"], text="区间事件的栾树记录",
            memory_date=None, date_confidence="unknown",
            original_title="t2", categories=["daily"],
            creation_mode="contemporaneous", raw_pending=False,
            occurred_start="2026-09-15T10:00:00+08:00",
            occurred_end="2026-09-15T12:00:00+08:00")
        assert out["memory_id"]
        base = {"original_request": "栾树", "channels": ["event"],
                "lexical_terms": ["栾树"]}
        with_neg = recall_service.start(actors["jiaming"], {
            "query_plan": {**base,
                           "explicit_negative_constraints": {
                               "event_date_excluded": [
                                   {"from": "2026-09-10",
                                    "to": "2026-09-20"}]}}})
        assert with_neg["candidates"] == [], \
            "occurred 区间落入排除窗的桶必须被排除"
        no_neg = recall_service.start(actors["jiaming"], {
            "query_plan": dict(base)})
        assert no_neg["candidates"], "前置：无排除时命中"


class TestP203Round2BudgetPreview:
    def test_first_page_budget_previews_round(self, actors, monkeypatch):
        """首页 budget 按"本轮已成功"预览（不再少算一轮）。"""
        from mariposa.source import importer
        import json as _json
        import tempfile
        import pathlib

        class G(typesafe_jev.TypeSafeJevJudge):
            name = "g_p203"
            _api_key = "k"

            def __init__(self):
                super().__init__()
                self._data_profile = frozenset(
                    {"event_excerpt", "title_cue", "word_excerpt",
                     "source_excerpt"})
                self._disabled_reason = None

            def judge(self, plan, cs, ctx):
                items = [jb.JudgeItem(
                    c.get("candidate_ref") or c["resource_ref"],
                    c.get("content_version"), relevance_signal=0.8,
                    evaluation_status="evaluated", model_id="g",
                    prompt_version="t") for c in cs]
                return jb.JudgeBatchResult(items=items,
                                           provider_status="evaluated")

        jb.register_for_tests("g_p203", G())
        from mariposa import config as cfg
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "g_p203"
        try:
            memory.hold(
                actors["jiaming"], text="崧蓝事件", memory_date="2026-09-25",
                date_confidence="exact", original_title="b",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False,
                our_words=[{"speaker": "qiaosheng", "text": "复述：崧蓝",
                            "expression_kind": "paraphrase"}])
            p = recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "我当时的原话",
                               "channels": ["words"],
                               "lexical_terms": ["崧蓝"],
                               "evidence_requirement":
                                   "verbatim_required"}})
            sid = p["recall_session_id"]
            assert p["budget"]["rounds_used"] == 1  # Round1 预览口径
            tmp = pathlib.Path(tempfile.mkdtemp())
            convs = [{"uuid": "c-203", "chat_messages": [{
                "uuid": "m-203", "sender": "human",
                "created_at": "2026-09-28T10:00:00.000Z",
                "content": [{"type": "text", "text": "崧蓝备注一条"}]}]}]
            f = tmp / "s.json"
            f.write_text(_json.dumps(convs, ensure_ascii=False),
                         encoding="utf-8")
            importer.import_file("jiaming", str(f))
            r2 = recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT"})
            assert r2["budget"]["rounds_used"] == 2, \
                "Round2 首页必须预览本轮成功后的预算（P2-03）"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class TestP204ProviderDimension:
    def _seed_collision(self):
        with db.formal() as conn:
            for prov in ("claude", "gemini"):
                conn.execute(
                    "INSERT INTO source_conversations(id, provider,"
                    " provider_conversation_id, title, created_at,"
                    " updated_at, first_import_batch_id, last_import_batch_id)"
                    " VALUES(?,?,?, 't', '2026-09-01', '2026-09-01',"
                    " 'b1', 'b1')",
                    (f"cv-{prov}", prov, f"pcv-{prov}"))
                conn.execute(
                    "INSERT INTO source_messages(id, conversation_id,"
                    " provider, provider_conversation_id,"
                    " provider_message_id, raw_sender, normalized_sender,"
                    " speaker, created_at, updated_at, occurred_date, text,"
                    " attachments, has_thinking, has_tool_content,"
                    " sequence, import_batch_id, published, content_hash)"
                    " VALUES(?,?,?,?,?, 'human','human','qiaosheng',"
                    " '2026-09-01','2026-09-01','2026-09-01',?, '[]',"
                    " 0,0,1,'b1',1,'h')",
                    (f"m-{prov}", f"cv-{prov}", prov, f"pcv-{prov}",
                     "shared-uuid", f"{prov} 的共享消息"))

    def test_ambiguous_without_provider(self, actors):
        self._seed_collision()
        from mariposa.source import query as sq
        with pytest.raises(Forbidden) as ei:
            sq.get_message(provider_message_id="shared-uuid")
        assert ei.value.code == "SOURCE_MESSAGE_AMBIGUOUS"
        assert set(ei.value.detail["providers"]) == {"claude", "gemini"}

    def test_resolves_with_provider(self, actors):
        self._seed_collision()
        from mariposa.source import query as sq
        out = sq.get_message(provider_message_id="shared-uuid",
                             provider="gemini")
        assert out["message"]["provider"] == "gemini"


class TestP205LocatorFailed:
    def test_anchor_miss_yields_empty_excerpt_flagged(self, actors):
        """有锚词而全部定位失败 → excerpt 空 + excerpt_locator=failed，
        不拿无关头部文本冒充命中证据。"""
        from mariposa.source import importer
        import json as _json
        import tempfile
        import pathlib
        tmp = pathlib.Path(tempfile.mkdtemp())
        body = "起" * 400 + "正文里确实有雨燕掠过"
        convs = [{"uuid": "c-205", "chat_messages": [{
            "uuid": "m-205", "sender": "human",
            "created_at": "2026-09-28T10:00:00.000Z",
            "content": [{"type": "text", "text": body}]}]}]
        f = tmp / "s.json"
        f.write_text(_json.dumps(convs, ensure_ascii=False),
                     encoding="utf-8")
        importer.import_file("jiaming", str(f))
        from mariposa.recall import pipeline as pl
        res = pl.raw_deep_search(
            actors["jiaming"],
            {"lexical_terms": ["雨燕"],
             "explicit_constraints": {}}, limit=10)
        assert res["hits"]
        hit = res["hits"][0]
        assert "雨燕" in (hit["excerpt"] or "")  # 正常锚定对照

        from mariposa.source import query as sq
        from mariposa.retrieval import projection as _proj
        res2 = sq.search(None,
                         fts_expr=_proj.compile_query("雨燕"),
                         senders=["human", "assistant"],
                         anchor_terms=["完全不存在的锚词"], limit=10)
        assert res2["hits"], "FTS 命中仍在（检索与锚词已解耦）"
        h = res2["hits"][0]
        assert h["excerpt"] == "" and h["excerpt_locator"] == "failed", \
            "锚词定位失败必须显式 fail，不退头部窗"


class TestP206DescriptionsAndLabels:
    def test_labels_cover_all_categories(self):
        from mariposa.memory.categories import CATEGORIES, LABELS
        missing = [c for c in CATEGORIES if c not in LABELS]
        assert missing == [], f"LABELS 漏分类：{missing}"

