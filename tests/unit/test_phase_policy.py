"""v1.7 阶段策略测试（§5.7 矩阵 + §6.4 gate + 分类必填）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from mariposa.recall import phase_policy as pp

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def held(days_ago: int, hour=10, minute=0):
    return NOW - timedelta(days=days_ago)


class TestIntegerPhases:
    @pytest.mark.parametrize("cat,h", [("daily", 20), ("sex", 20), ("sad", 30),
                                       ("sweet", 30), ("date", 60),
                                       ("reloplay", 7)])
    @pytest.mark.parametrize("k", [70, 100, 105, 110, 120, 150])
    def test_matrix_wide_wide_mid_mid_core(self, cat, h, k):
        h_eff = pp.effective_days(h, k)
        for age, want in ((0, "WIDE"), (h_eff - 1, "WIDE"), (h_eff, "MID"),
                          (2 * h_eff - 1, "MID"), (2 * h_eff, "CORE")):
            r = pp.compute_phase(now=NOW, first_held_at=held(age),
                                 categories=[cat], k_percent=k)
            assert r.stage == want, (cat, k, age, r.stage, want)
            assert r.h_eff_days == h_eff

    def test_multi_category_takes_max_not_sum(self):
        r = pp.compute_phase(now=NOW, first_held_at=held(59),
                             categories=["date", "daily"])  # max(60,20)=60
        assert r.stage == "WIDE"  # 59 < 60
        assert r.h_eff_days == 60

    def test_reloplay_plus_date_is_60(self):
        r = pp.compute_phase(now=NOW, first_held_at=held(10),
                             categories=["reloplay", "date"])
        assert r.h_eff_days == 60  # 取最长，不是 7

    def test_shanghai_day_boundary(self):
        # 上海 23:50 hold，次日 00:10 计算 → D=1（§5.1 例）
        now = datetime(2026, 9, 2, 0, 10, tzinfo=TZ)
        first = datetime(2026, 9, 1, 23, 50, tzinfo=TZ)
        r = pp.compute_phase(now=now, first_held_at=first,
                             categories=["reloplay"])  # H=7
        assert r.age_days == 1
        # UTC 日期相减再除 86400 的错误实现会得到 0

    def test_naive_datetime_rejected(self):
        with pytest.raises(pp.PolicyError):
            pp.compute_phase(now=datetime(2026, 9, 28),  # 无时区
                             first_held_at=datetime(2026, 9, 1, tzinfo=TZ),
                             categories=["daily"])


class TestPermanentAndPlan:
    def test_milestone_permanent_wide(self):
        r = pp.compute_phase(now=NOW, first_held_at=held(3650),
                             categories=["milestone"])
        assert r.stage == "WIDE" and r.reason == "PERMANENT_CATEGORY"
        assert r.h_eff_days is None

    def test_anniversary_permanent(self):
        assert pp.compute_phase(now=NOW, first_held_at=held(999),
                                categories=["anniversary"]).stage == "WIDE"

    def test_reloplay_plus_anniversary_permanent(self):
        r = pp.compute_phase(now=NOW, first_held_at=held(365),
                             categories=["reloplay", "anniversary"])
        assert r.stage == "WIDE" and r.reason == "PERMANENT_CATEGORY"

    @pytest.mark.parametrize("st", ["planned", "active", "waiting", "blocked"])
    def test_open_plan_wide(self, st):
        r = pp.compute_phase(now=NOW, first_held_at=held(365),
                            categories=["plan"], resource_kind="plan",
                            plan_status=st)
        assert r.stage == "WIDE" and r.reason == "OPEN_PLAN"

    @pytest.mark.parametrize("st", ["done", "cancelled"])
    def test_terminal_plan_immediate_core(self, st):
        r = pp.compute_phase(now=NOW, first_held_at=held(0),
                             categories=["plan"], resource_kind="plan",
                             plan_status=st)
        assert r.stage == "CORE" and r.reason == "TERMINAL_PLAN"

    def test_keep_beats_terminal_plan(self):
        r = pp.compute_phase(now=NOW, first_held_at=held(365),
                             categories=["plan"], resource_kind="plan",
                             plan_status="done",
                             active_keep_owners=["qiaosheng"])
        assert r.stage == "WIDE" and r.reason == "EXPLICIT_KEEP"

    def test_plan_only_bucket_without_mapping_is_gap(self):
        with pytest.raises(pp.DataGap):
            pp.compute_phase(now=NOW, first_held_at=held(1),
                             categories=["plan"])  # memory 资源、仅 plan 类

    def test_related_plan_state_must_not_override_event(self):
        with pytest.raises(pp.PolicyError):
            pp.compute_phase(now=NOW, first_held_at=held(1),
                             categories=["daily"], plan_status="done")


class TestOpenRewarm:
    def test_explicit_open_resets_basis(self):
        r = pp.compute_phase(now=NOW, first_held_at=held(100),
                             categories=["daily"],
                             last_explicit_open_at=held(5))
        assert r.basis_date == held(5).astimezone(TZ).date().isoformat()
        assert r.stage == "WIDE"  # D=5 < 20

    def test_display_a_not_used_in_stage(self):
        r = pp.compute_phase(now=NOW, first_held_at=held(100),
                             categories=["daily"])
        a = pp.display_availability(r)
        assert a is not None and float(a) <= 1.0
        assert r.stage == "CORE"


class TestCategoryRequired:
    def test_empty_rejected(self):
        with pytest.raises(pp.PolicyError):
            pp.normalize_categories([])

    def test_none_rejected(self):
        with pytest.raises(pp.PolicyError):
            pp.normalize_categories(None)

    def test_unknown_rejected(self):
        with pytest.raises(pp.PolicyError):
            pp.normalize_categories(["daily", "未知类"])

    def test_aliases_map(self):
        assert pp.normalize_categories(["伤心的事"]) == frozenset({"sad"})
        assert pp.normalize_categories(["剧本"]) == frozenset({"reloplay"})


class TestFields654:
    def test_field_sets(self):
        assert pp.compute_phase(now=NOW, first_held_at=held(0),
                                categories=["daily"]).first_round_fields == \
            ("categories", "event_date", "event_text", "mood_tags",
             "original_title", "our_words")
        r = pp.compute_phase(now=NOW, first_held_at=held(25),
                             categories=["daily"])  # MID
        assert set(r.first_round_fields) == pp.MID_FIELDS
        r = pp.compute_phase(now=NOW, first_held_at=held(50),
                             categories=["daily"])  # CORE
        assert set(r.first_round_fields) == pp.CORE_FIELDS
        assert "original_title" not in pp.CORE_FIELDS
        assert "our_words" not in pp.CORE_FIELDS


class TestRound2Gate:
    def test_all_gates_true_allows(self):
        assert pp.allow_raw_round2(
            same_session=True, same_query_revision=True, same_scope=True,
            round1_complete_receipt=True, judge_complete=True,
            raw_search_authorized=True, budget_available=True,
            reason="EVIDENCE_INSUFFICIENT")

    def test_any_gate_false_blocks(self):
        base = dict(same_session=True, same_query_revision=True,
                    same_scope=True, round1_complete_receipt=True,
                    judge_complete=True, raw_search_authorized=True,
                    budget_available=True, reason="NO_DELIVERABLE_CANDIDATE")
        for key in ("same_session", "same_query_revision", "same_scope",
                    "round1_complete_receipt", "judge_complete",
                    "raw_search_authorized", "budget_available"):
            kw = dict(base); kw[key] = False
            assert not pp.allow_raw_round2(**kw), key

    def test_bad_reason_blocks(self):
        assert not pp.allow_raw_round2(
            same_session=True, same_query_revision=True, same_scope=True,
            round1_complete_receipt=True, judge_complete=True,
            raw_search_authorized=True, budget_available=True,
            reason="JUDGE_UNAVAILABLE")  # §6.4 明确不构成理由


class TestFactsFromDb:
    def test_phase_of_with_keeps_and_open(self, actors):
        from mariposa import db
        from mariposa.memory import service as memory
        from mariposa.memory import keep as keep_mod
        out = memory.hold(actors["jiaming"], text="阶段事实桶",
                          memory_date="2026-09-20", date_confidence="exact",
                          categories=["daily"], original_title="测试标题")
        mid = out["memory_id"]
        with db.formal() as c:
            c.execute("UPDATE memories SET held_at=? WHERE memory_id=?",
                      ((NOW - timedelta(days=100)).isoformat(), mid))
            c.execute(
                "INSERT INTO memory_keeps(mark_id, memory_id, owner,"
                " recollection_id, recollection_version, created_at)"
                " VALUES('mk1', ?, 'jiaming', 'rc1', 1, ?)",
                (mid, NOW.isoformat()))
        r = pp.phase_of(mid, now=NOW)
        assert r.stage == "WIDE" and r.reason == "EXPLICIT_KEEP"
        with db.formal() as c:
            c.execute("UPDATE memory_keeps SET revoked_at=? WHERE mark_id='mk1'",
                      (NOW.isoformat(),))
        r2 = pp.phase_of(mid, now=NOW)
        assert r2.stage == "CORE" and r2.reason == "INTEGER_AGE"  # D=100≥40
