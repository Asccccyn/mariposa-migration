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
from mariposa.memory import (categories as cats_mod, retention as ret_mod,
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
            r = ret_mod.get(conn, out["memory_id"])
        assert m["memory_date"] == "2026-08-15"
        assert m["held_at"] is not None
        assert m["creation_mode"] == "contemporaneous"
        # 期限从 hold 日期起算（RET-01）：不因事件久远立即到期
        assert r["basis_date"] is not None and r["basis_date"] >= "2026-09"
        assert r["due_date"] == (datetime.fromisoformat(r["basis_date"]).date()
                                 + timedelta(days=20)).isoformat()

    def test_parallel_categories_single_bucket_rec02(self, actors):
        out = hold_v2(actors, cats=["date", "sweet", "sex"])
        with db.formal() as conn:
            cats = cats_mod.list_of(conn, out["memory_id"])
        assert set(cats) == {"date", "sweet", "sex"}

    def test_contemporaneous_mood_stored_rec03(self, actors):
        out = hold_v2(actors, mood={"text": "河水很凉，风吹得舒服",
                                    "tags": ["平静", "开心"]})
        with db.formal() as conn:
            got = memory.get(conn, out["memory_id"])
        assert got["mood"]["text"].startswith("河水")
        assert set(got["mood"]["tags"]) == {"平静", "开心"}
        assert got["mood"]["author"] == "jiaming"
        assert got["mood"]["evidence_state"] == "contemporaneous"

    def test_retrospective_mood_rejected_rec04(self, actors):
        with pytest.raises(Forbidden) as e:
            hold_v2(actors, mode="retrospective",
                    mood={"text": "现在的感受", "tags": ["平静"]})
        assert e.value.detail.get("code") == "MOOD_WINDOW_REQUIRED"

    def test_no_mood_no_downgrade_rec06(self, actors):
        out = hold_v2(actors, mood=None)
        with db.formal() as conn:
            got = memory.get(conn, out["memory_id"])
            r = ret_mod.get(conn, out["memory_id"])
        assert "mood" not in got
        assert r["status"] == "active"  # 心情空白不缩短期限、不判不重要
        assert r["due_date"]  # 正常周期

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


class TestNaturalDayRetention:
    def test_period_per_category_ret02(self, actors):
        cases = {"daily": 20, "sad": 30, "sweet": 30, "sex": 20, "date": 60}
        for cat, days in cases.items():
            out = hold_v2(actors, text=f"{cat}事", cats=[cat])
            with db.formal() as conn:
                r = ret_mod.get(conn, out["memory_id"])
            assert r["due_date"] == (
                datetime.fromisoformat(r["basis_date"]).date()
                + timedelta(days=days)).isoformat(), cat

    def test_permanent_categories_excluded_ret02(self, actors):
        for cat in ("milestone", "anniversary"):
            out = hold_v2(actors, text=f"{cat}事", cats=[cat])
            with db.formal() as conn:
                r = ret_mod.get(conn, out["memory_id"])
            assert r["status"] == "excluded" and r["due_date"] is None, cat

    def test_max_period_ret03(self, actors):
        out = hold_v2(actors, cats=["daily", "sad", "date"])  # 20+30+60 → 60
        with db.formal() as conn:
            r = ret_mod.get(conn, out["memory_id"])
        assert (datetime.fromisoformat(r["due_date"])
                - datetime.fromisoformat(r["basis_date"])).days == 60
        out2 = hold_v2(actors, cats=["milestone", "date"])  # 永久类压制
        with db.formal() as conn:
            r2 = ret_mod.get(conn, out2["memory_id"])
        assert r2["status"] == "excluded" and r2["due_date"] is None

    def test_natural_day_boundary_ret13(self, actors):
        # 2026-09-21 23:50（Asia/Shanghai）首次 hold 的 20 天桶 → 10-11 到期；
        # 到期从日界起算而非当天 23:50（纯函数级验证，冻结时间）
        held = "2026-09-21T15:50:00+00:00"  # = 上海 23:50
        row = ret_mod.compute_row(["daily"], held, "Asia/Shanghai")
        assert row["basis_date"] == "2026-09-21"
        assert row["due_date"] == "2026-10-11"
        # next_due_at = 10-11 上海日界的 UTC 时刻（=10-10 16:00Z）
        assert row["next_due_at"].startswith("2026-10-10T16:00:00")
        # 10-10 23:59 尚未到期；10-11 00:00 起具备候选资格
        from datetime import date as _d
        assert _d(2026, 10, 10) < _d.fromisoformat(row["due_date"])
        assert ret_mod.local_date("2026-10-10T15:59:00+00:00",
                                  "Asia/Shanghai") < _d.fromisoformat(row["due_date"])
        assert ret_mod.local_date("2026-10-10T16:00:00+00:00",
                                  "Asia/Shanghai") == _d.fromisoformat(row["due_date"])

    def test_date_arithmetic_cross_month_leap_ret14(self, actors):
        assert ret_mod.due_from_basis(
            __import__("datetime").date(2026, 12, 20), 20).isoformat() == "2027-01-09"
        assert ret_mod.due_from_basis(
            __import__("datetime").date(2028, 2, 20), 20).isoformat() == "2028-03-11"

    def test_explicit_open_renews_ret04(self, actors):
        out = hold_v2(actors, cats=["daily"])
        opened = views_mod.open_memory(actors["qiaosheng"], out["memory_id"])
        got = views_mod.confirm_view(actors["qiaosheng"], out["memory_id"],
                                     opened["view_receipt"])
        assert got["renewed"] is True
        with db.formal() as conn:
            r = ret_mod.get(conn, out["memory_id"])
        assert r["last_explicit_open_at"] is not None
        assert r["due_date"] == (
            datetime.fromisoformat(r["basis_date"]).date()
            + timedelta(days=20)).isoformat()  # basis 已移到打开日
        assert r["retention_revision"] >= 1

    def test_passive_reads_do_not_renew_ret05(self, actors):
        out = hold_v2(actors, cats=["daily"])
        with db.formal() as conn:
            before = ret_mod.get(conn, out["memory_id"])
            memory.get(conn, out["memory_id"])  # 读不续期
            memory.search(conn, "散步") if hasattr(memory, "search") else None
            from mariposa.retrieval import search as rsearch
            rsearch.search(conn, "散步")
            after = ret_mod.get(conn, out["memory_id"])
        assert before == after

    def test_same_receipt_idempotent_ret06(self, actors):
        out = hold_v2(actors, cats=["daily"])
        opened = views_mod.open_memory(actors["jiaming"], out["memory_id"])
        r1 = views_mod.confirm_view(actors["jiaming"], out["memory_id"],
                                    opened["view_receipt"])
        r2 = views_mod.confirm_view(actors["jiaming"], out["memory_id"],
                                    opened["view_receipt"])
        assert r2.get("idempotent_replay") is True
        with db.formal() as conn:
            rev = conn.execute(
                "SELECT retention_revision FROM memory_retention WHERE memory_id=?",
                (out["memory_id"],)).fetchone()["retention_revision"]
        assert rev == 1  # 只续期一次

    def test_worker_cannot_open_or_renew(self, actors):
        out = hold_v2(actors, cats=["daily"])
        with pytest.raises(Forbidden):
            views_mod.open_memory(actors["worker"], out["memory_id"])

    def test_same_day_reopen_no_hour_shift_ret15(self, actors):
        out = hold_v2(actors, cats=["daily"])
        with db.formal() as conn:
            before = ret_mod.get(conn, out["memory_id"])
        o1 = views_mod.open_memory(actors["jiaming"], out["memory_id"])
        views_mod.confirm_view(actors["jiaming"], out["memory_id"],
                               o1["view_receipt"])
        o2 = views_mod.open_memory(actors["jiaming"], out["memory_id"])
        got = views_mod.confirm_view(actors["jiaming"], out["memory_id"],
                                     o2["view_receipt"])
        with db.formal() as conn:
            after = ret_mod.get(conn, out["memory_id"])
        assert after["due_date"] == before["due_date"]  # 同日 due 相同
        assert got.get("same_day") is True

    def test_receipt_not_reusable_across_version(self, actors):
        out = hold_v2(actors, cats=["daily"])
        opened = views_mod.open_memory(actors["jiaming"], out["memory_id"])
        # 造成表示变化：遗忘→恢复会 bump representation_state；
        # 直接改 representation_state 模拟表示推进
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET representation_state=representation_state+1"
                " WHERE memory_id=?", (out["memory_id"],))
        with pytest.raises(ViewReceiptInvalid):
            views_mod.confirm_view(actors["jiaming"], out["memory_id"],
                                   opened["view_receipt"])


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

    def test_recollection_triggers_retain_hint_rev07(self, actors):
        out = hold_v2(actors)
        rec_mod.append(actors["jiaming"], out["memory_id"],
                       self._confirmed(actors, out["memory_id"]), "后来想起")
        with db.formal() as conn:
            r = ret_mod.get(conn, out["memory_id"])
        assert r["retain_hint"] == "has_recollections"

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
