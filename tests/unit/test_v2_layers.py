"""v2 分层模型与自然日留存（P2/P3）。

对应 spec_v2 验收：
- V2-REC-01/02/03/04/06/09（事件日期与hold分离、平行多分类、同窗口心情、
  跨窗口拒绝、心情缺失不降级、乔生不代写心情）
- V2-RET-01/02/03/04/05/06/13/14/15（hold起算、分类周期、最长、明确打开
  续期、被动不续期、同次打开去重、自然日边界、跨月闰日、同日不后移）
- V2-VIEW-01/02/03/04/05（回忆需明确查看、双人署名、回执不跨桶、刷新不
  自动写、版本可追溯）
- V2-I-01/02/03
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, NotFound, ViewReceiptInvalid
from mariposa.identity import service as identity
from mariposa.memory import (categories as cats_mod,

                             service as memory)
from mariposa.memory import recollections as rec_mod
from mariposa.memory import views as views_mod
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


def hold_v2(actors, text="一次晚饭后沿河散步", date="2026-08-15", cats=None,
            mood=None, our_words=None, mode="contemporaneous",
            title="散步的晚上", principal="jiaming", **kw):
    return memory.hold(
        actors[principal], text=text, memory_date=date,
        date_confidence="exact", original_title=title,
        categories=cats or ["daily"], mood=mood, our_words=our_words,
        creation_mode=mode, raw_pending=False, **kw)


class TestV2HoldLayered:
    def test_event_date_and_hold_separated_rec01(self, actors):
        out = hold_v2(actors, date="2026-08-15")  # 8月事件，9月 hold
        with db.formal() as conn:
            m = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                             (out["memory_id"],)).fetchone()
        assert m["memory_date"] == "2026-08-15"
        assert m["held_at"] is not None
        assert m["creation_mode"] == "contemporaneous"
        # v1.7：阶段由 held_at/last_explicit_open_at 现算（P2 阶段策略测试
        # 承载 RET-01 语义；这里仅断言事实分离）

    def test_parallel_categories_single_bucket_rec02(self, actors):
        out = hold_v2(actors, cats=["date", "sweet", "sex"])
        with db.formal() as conn:
            cats = cats_mod.list_of(conn, out["memory_id"])
        assert set(cats) == {"date", "sweet", "sex"}

    def test_contemporaneous_mood_stored_rec03(self, actors):
        out = hold_v2(actors, mood={"text": "河水很凉，风吹得舒服",
                                    "tags": ["开心"]})
        with db.formal() as conn:
            got = memory.get(conn, out["memory_id"])
        assert got["mood"]["text"].startswith("河水")
        assert set(got["mood"]["tags"]) == {"开心"}
        assert got["mood"]["author"] == "jiaming"
        assert got["mood"]["evidence_state"] == "contemporaneous"

    def test_retrospective_mood_rejected_rec04(self, actors):
        """V2-REC-04 原样：跨窗口补记不能补造当时心情（2026-10-05
        擅自放宽已回滚——裁定说的是解释槽归属，不是放开当时心情）。"""
        with pytest.raises(Forbidden) as e:
            hold_v2(actors, mode="retrospective",
                    mood={"text": "现在的感受", "tags": ["不安"]})
        assert e.value.detail.get("code") == "MOOD_WINDOW_REQUIRED"

    def test_no_mood_no_downgrade_rec06(self, actors):
        out = hold_v2(actors, mood=None)
        with db.formal() as conn:
            got = memory.get(conn, out["memory_id"])
        assert "mood" not in got
        # v1.7：心情空白不缩短期限——阶段现算，无 retention 行可断言

    def test_qiaosheng_cannot_write_mood_rec09(self, actors):
        with pytest.raises(Forbidden) as e:
            hold_v2(actors, principal="qiaosheng",
                    mood={"text": "她说很开心", "tags": ["开心"]})
        assert e.value.detail.get("code") == "MOOD_AUTHOR_REQUIRED"

    def test_our_words_order_and_speaker(self, actors):
        out = hold_v2(actors, our_words=[
            {"speaker": "jiaming", "text": "今晚风好大"},
            {"speaker": "qiaosheng", "text": "风大也陪你走完", "expression_kind": "verbatim"},
        ])
        words = registry.invoke(actors["jiaming"], "memory.our_words.list",
                                {"memory_id": out["memory_id"]}, None)["data"]["words"]
        assert [w["ordinal"] for w in words] == [1, 2]
        assert words[0]["speaker"] == "jiaming"
        assert words[1]["expression_kind"] == "verbatim"

    def test_title_not_in_projection(self, actors):
        out = hold_v2(actors, title="青鸾独语夜", text="普通的事件正文")
        with db.formal() as conn:
            doc = conn.execute(
                "SELECT search_text FROM retrieval_documents WHERE memory_id=?",
                (out["memory_id"],)).fetchone()
        # 标题字符不得进入索引（投影按单字分词，逐字检查）
        for ch in "青鸾独语":
            assert ch not in doc["search_text"], ch
        assert "普" in doc["search_text"] and "通" in doc["search_text"]


class TestRecollections:
    def _confirmed(self, actors, mid, who="jiaming"):
        opened = views_mod.open_memory(actors[who], mid)
        views_mod.confirm_view(actors[who], mid, opened["view_receipt"])
        return opened["view_receipt"]

    def test_append_requires_confirmed_view_view01(self, actors):
        out = hold_v2(actors)
        opened = views_mod.open_memory(actors["jiaming"], out["memory_id"])
        with pytest.raises(ViewReceiptInvalid):  # 未确认
            rec_mod.append(actors["jiaming"], out["memory_id"],
                           opened["view_receipt"], "后来的理解")
        with pytest.raises(ViewReceiptInvalid):  # 回执不存在
            rec_mod.append(actors["jiaming"], out["memory_id"],
                           "vr_nonexistent", "后来的理解")

    def test_both_owners_sign_own_names_view02(self, actors):
        out = hold_v2(actors)
        r1 = rec_mod.append(actors["jiaming"], out["memory_id"],
                            self._confirmed(actors, out["memory_id"], "jiaming"),
                            "我记得那晚的风")
        r2 = rec_mod.append(actors["qiaosheng"], out["memory_id"],
                            self._confirmed(actors, out["memory_id"], "qiaosheng"),
                            "我记得她的笑")
        assert r1["author"] == "jiaming" and r2["author"] == "qiaosheng"
        with pytest.raises(Forbidden):
            rec_mod.append(actors["worker"], out["memory_id"],
                           self._confirmed(actors, out["memory_id"], "jiaming"), "代写")

    def test_receipt_not_cross_memory_view03(self, actors):
        a = hold_v2(actors, text="桶A")
        b = hold_v2(actors, text="桶B")
        receipt = self._confirmed(actors, a["memory_id"])
        with pytest.raises(ViewReceiptInvalid):
            rec_mod.append(actors["jiaming"], b["memory_id"], receipt, "跨桶")

    def test_revision_keeps_history_view05(self, actors):
        out = hold_v2(actors)
        first = rec_mod.append(actors["jiaming"], out["memory_id"],
                               self._confirmed(actors, out["memory_id"]), "初稿")
        second = rec_mod.revise(actors["jiaming"], first["recollection_id"],
                                "修订稿")
        assert second["supersedes"] == first["recollection_id"]
        cur = rec_mod.list_for(out["memory_id"])
        assert [c["text"] for c in cur] == ["修订稿"]
        full = rec_mod.list_for(out["memory_id"], include_history=True)
        assert {c["text"] for c in full} == {"初稿", "修订稿"}


class TestIdentityI:
    def test_only_jiaming_writes_i01(self, actors):
        from mariposa.identity_i import service as i_svc
        with pytest.raises(Forbidden):
            i_svc.write("qiaosheng", "乔生写I")
        with pytest.raises(Forbidden):
            i_svc.write("worker", "后台写I")
        got = i_svc.write("jiaming", "我是周家明。")
        assert got["version"] == 1
        assert i_svc.get()["content"] == "我是周家明。"

    def test_no_mood_gate_i02(self, actors):
        from mariposa.identity_i import service as i_svc
        # 不存在情绪准入判断：写入永远不被"心情不平静"拒绝
        assert i_svc.write("jiaming", "争吵时也写下")["version"] >= 1

    def test_qiaosheng_suggestion_does_not_touch_canonical_i01(self, actors):
        from mariposa.identity_i import service as i_svc
        i_svc.write("jiaming", "正本内容")
        out = i_svc.suggest("qiaosheng", "建议加一条锚点")
        assert out["status"] == "open"
        assert i_svc.get()["content"] == "正本内容"  # 建议不改正本

    def test_versions_kept(self, actors):
        from mariposa.identity_i import service as i_svc
        v1 = i_svc.write("jiaming", "第一版")
        v2 = i_svc.write("jiaming", "第二版", expected_version=v1["version"])
        vs = i_svc.versions_read()
        assert [x["content"] for x in vs] == ["第一版", "第二版"]
        with pytest.raises(Exception):
            i_svc.write("jiaming", "过期版本写入", expected_version=1)
