"""v2 召回白名单与禁检探针（P5）：V2-SEARCH-01..17 子集。

独有合成探针分别只放进：标题 / 心情文字 / 我们的话 / 回忆 / 原文 /
事件正文，验证普通召回（recall/search/列表投影）不会因禁检字段命中。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import recollections as rec_mod
from mariposa.memory import service as memory
from mariposa.memory import views as views_mod
from mariposa.retrieval import search as rsearch
from mariposa.raw import service as raw
from mariposa.workspace import service as workspace
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent",
                                     "gpt_chat", "bw"),
    }


def hold_v2(actors, **kw):
    base = dict(text="普通事件正文", memory_date="2026-09-10",
                date_confidence="exact", original_title="普通标题",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def recall(actors, query="", **kw):
    with db.formal() as conn:
        return rsearch.recall(conn, query, kw.get("filters"),
                              kw.get("limit", 20), kw.get("cursor"))


def hit_ids(out):
    return {h["memory_id"] for h in out["hits"]}


class TestBrowseFilters:
    def test_category_only_browse_search01(self, actors):
        a = hold_v2(actors, text="约会事件", categories=["date"])
        b = hold_v2(actors, text="日常事件", categories=["daily"])
        out = recall(actors, filters={"categories": ["date"]})
        assert a["memory_id"] in hit_ids(out)
        assert b["memory_id"] not in hit_ids(out)
        assert out["mode"] == "browse"

    def test_mood_tag_only_browse_search02(self, actors):
        a = hold_v2(actors, text="开心事", mood={"text": "开心文字", "tags": ["开心"]})
        b = hold_v2(actors, text="无心情事")
        out = recall(actors, filters={"mood_tags": ["开心"]})
        assert a["memory_id"] in hit_ids(out)
        assert b["memory_id"] not in hit_ids(out)
        # 心情文字本身不被文本搜索命中（SEARCH-07）
        assert not hit_ids(recall(actors, "开心文字"))

    def test_event_date_filter_search03(self, actors):
        a = hold_v2(actors, text="八月事", memory_date="2026-08-01")
        b = hold_v2(actors, text="九月事", memory_date="2026-09-01")
        out = recall(actors, filters={"event_date": {"from": "2026-08-01",
                                                      "to": "2026-08-31"}})
        assert a["memory_id"] in hit_ids(out)
        assert b["memory_id"] not in hit_ids(out)

    def test_multi_category_any_all(self, actors):
        a = hold_v2(actors, text="约会甜蜜", categories=["date", "sweet"])
        b = hold_v2(actors, text="只有甜蜜", categories=["sweet"])
        any_out = recall(actors, filters={"categories": ["date", "sweet"],
                                           "category_match": "any"})
        all_out = recall(actors, filters={"categories": ["date", "sweet"],
                                           "category_match": "all"})
        assert {a["memory_id"], b["memory_id"]} <= hit_ids(any_out)
        assert a["memory_id"] in hit_ids(all_out)
        assert b["memory_id"] not in hit_ids(all_out)

    def test_same_bucket_deduped_search05(self, actors):
        m = hold_v2(actors, text="多池事件", categories=["date", "sweet", "sex"])
        out = recall(actors, filters={"categories": ["date", "sweet", "sex"]})
        ids = [h["memory_id"] for h in out["hits"]]
        assert ids.count(m["memory_id"]) == 1

    def test_pool_before_rank_search04(self, actors):
        hold_v2(actors, text="别的池的散步", categories=["daily"])
        target = hold_v2(actors, text="约会池的散步", categories=["date"])
        out = recall(actors, "散步", filters={"categories": ["date"]})
        ids = hit_ids(out)
        assert target["memory_id"] in ids
        assert all(h["matched_by"] == "keyword" for h in out["hits"])


class TestForbiddenProbes:
    """独有探针：各禁检字段独有词不得经任何普通召回途径命中。"""

    def test_title_only_probe_search06(self, actors):
        m = hold_v2(actors, original_title="翾骓标题词", text="完全无关的正文")
        assert m["memory_id"] not in hit_ids(recall(actors, "翾骓"))
        with db.formal() as conn:
            assert not rsearch.search(conn, "翾骓")["hits"]

    def test_mood_text_only_probe_search07(self, actors):
        m = hold_v2(actors, text="普通正文", mood={"text": "惘湎心情词",
                                                    "tags": ["平静"]})
        assert m["memory_id"] not in hit_ids(recall(actors, "惘湎"))

    def test_our_words_only_probe_search08(self, actors):
        m = hold_v2(actors, text="普通正文", our_words=[
            {"speaker": "jiaming", "text": "瓯缶话语词"}])
        assert m["memory_id"] not in hit_ids(recall(actors, "瓯缶"))

    def test_recollection_only_probe_search09(self, actors):
        m = hold_v2(actors, text="普通正文")
        o = views_mod.open_memory(actors["jiaming"], m["memory_id"])
        views_mod.confirm_view(actors["jiaming"], m["memory_id"], o["view_receipt"])
        rec_mod.append(actors["jiaming"], m["memory_id"], o["view_receipt"],
                       "瀺灂回忆词浮现在这里")
        assert m["memory_id"] not in hit_ids(recall(actors, "瀺灂"))

    def test_raw_only_probe_search10(self, actors):
        raw.import_payload("jiaming", {
            "source_channel": "probe", "external_id": "p1",
            "messages": [{"source_message_id": "m1", "role": "assistant",
                          "body": "棽椮原文词的独家记录",
                          "occurred_at": "2026-09-10T10:00:00+00:00",
                          "sequence": 1}]})
        assert not hit_ids(recall(actors, "棽椮"))
        # raw.search 是独立原文查询（source=raw），命中不冒充记忆
        with db.formal() as conn:
            mem_hits = rsearch.search(conn, "棽椮")["hits"]
        assert not mem_hits

    def test_forgotten_summary_and_old_text_search11(self, actors):
        m = hold_v2(actors, text="苉蕡旧事件独有词",
                    memory_date="2020-01-01")
        assert m["memory_id"] in hit_ids(recall(actors, "苉蕡"))
        # superseded（审计修复 2026-09-23）：v2 分层桶改走 v2 审查闭环
        # 遗忘（v1 扫描/审批对 v2 桶关闭，V2_MANAGED_TARGET），断言不变。
        from mariposa.workspace import review as review_mod
        from mariposa.memory import retention as ret_mod
        lin = identity.Principal("linshijian", "林石见", "human", "mcp", "bl")
        with db.formal() as conn:
            ret_mod.create_for_memory(conn, m["memory_id"], ["daily"],
                                      "2020-01-01T00:00:00+00:00")
        review_mod.ensure_default_delegation()
        g = review_mod.generate(actors["worker"],
                                memory_id=m["memory_id"],
                                candidate_summary="压缩后的摘要新词缱绻。")
        item_id = g["created"][0]["item_id"]
        with db.workspace() as wconn:
            wconn.execute(
                "UPDATE v2_review_items SET claimed_by='linshijian'"
                " WHERE item_id=?", (item_id,))
        review_mod.submit(lin, item_id, "release", 1)
        assert not hit_ids(recall(actors, "苉蕡"))  # 旧正文词失效
        got = recall(actors, "缱绻")
        assert m["memory_id"] in hit_ids(got)
        assert got["hits"][0]["matched_fields"] == ["summary_body"]
        # 结构化字段仍可命中（分类/日期）
        out = recall(actors, filters={"event_date": {"from": "2020-01-01",
                                                      "to": "2020-12-31"}})
        assert m["memory_id"] in hit_ids(out)


class TestRegistryRecall:
    def test_recall_capability_invokes(self, actors):
        m = hold_v2(actors, text="散步事件", categories=["sweet"])
        out = registry.invoke(actors["qiaosheng"], "memory.recall",
                              {"query": "散步",
                               "filters": {"categories": ["sweet"]}}, None)
        assert m["memory_id"] in {h["memory_id"] for h in out["data"]["hits"]}

    def test_worker_forbidden(self, actors):
        with pytest.raises(Exception):
            registry.invoke(actors["worker"], "memory.recall", {}, None)

    def test_chinese_short_terms_search17(self, actors):
        m1 = hold_v2(actors, text="晚上一起吃烧烤")
        m2 = hold_v2(actors, text="她说叽")
        assert m1["memory_id"] in hit_ids(recall(actors, "烧烤"))
        assert m2["memory_id"] in hit_ids(recall(actors, "叽"))
