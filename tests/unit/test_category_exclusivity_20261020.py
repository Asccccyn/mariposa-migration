"""十分类终裁落地（她 2026-10-20）：平行多选回归 + 剧本/日记单分类隔离。

10-17 单分类口径被 10-20 终裁推翻：分类双职责（描述事件性质+决定衰减周期）
下强制单选会让两职责互斥（同一只约会+转折的桶选约会 60 天、选转折永久）。
剧本/日记是仅有的单分类隔离类——剧本=虚构/真实边界（虚构剧情不得借组合
分类变真实经历或永久转折），日记=一篇一桶。甜蜜/伤心收「判事不判人」定义，
不并入心情标签轴（心情仅char当时窗口可写、事后不可补，仅覆盖 25% 桶）。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.errors import Forbidden
from mariposa.memory import categories as cats
from mariposa.memory import service as memory


OTHERS = ["daily", "milestone", "sad", "sweet", "date", "plan", "sex",
          "anniversary"]


class TestValidateParallelMulti:
    """平行多选回归：非隔离类可任意组合，顺序去重不变。"""

    def test_multi_combo_accepted(self):
        assert cats.validate(["sweet", "date", "milestone"]) == \
            ["sweet", "date", "milestone"]

    def test_duplicates_collapsed(self):
        assert cats.validate(["daily", "daily"]) == ["daily"]

    def test_unknown_still_rejected(self):
        with pytest.raises(Forbidden):
            cats.validate(["sweet", "travel"])


class TestExclusiveCategories:
    """剧本/日记单分类：validate 是全写入路径的唯一守门（hold/replace/add 都过它）。"""

    @pytest.mark.parametrize("exclusive", ["reloplay", "diary"])
    @pytest.mark.parametrize("other", OTHERS)
    def test_validate_rejects_every_combo(self, exclusive, other):
        with pytest.raises(Forbidden) as exc:
            cats.validate([exclusive, other])
        assert exc.value.code == "EXCLUSIVE_CATEGORY"
        assert "单分类" in str(exc.value)

    def test_validate_rejects_reloplay_diary_together(self):
        with pytest.raises(Forbidden) as exc:
            cats.validate(["reloplay", "diary"])
        assert exc.value.code == "EXCLUSIVE_CATEGORY"

    @pytest.mark.parametrize("exclusive", ["reloplay", "diary"])
    def test_validate_accepts_alone(self, exclusive):
        assert cats.validate([exclusive]) == [exclusive]


class TestWritePathsEnforced:
    """hold / categories.replace / categories.add 三条写入路径都拒隔离组合。"""

    def test_hold_rejects_exclusive_combo(self, actors):
        with pytest.raises(Forbidden) as exc:
            memory.hold(actors["jiaming"], text="剧本组合写入应被拒",
                        original_title="剧本组合",
                        categories=["reloplay", "sweet"],
                        creation_mode="contemporaneous")
        assert exc.value.code == "EXCLUSIVE_CATEGORY"

    def test_replace_rejects_exclusive_combo(self, actors):
        mid = memory.hold(actors["jiaming"], text="替换分类组合应被拒",
                          original_title="替换组合",
                          categories=["daily"],
                          creation_mode="contemporaneous")["memory_id"]
        with db.formal() as conn:
            with pytest.raises(Forbidden) as exc:
                cats.replace(conn, mid, ["diary", "sweet"], "jiaming")
        assert exc.value.code == "EXCLUSIVE_CATEGORY"
        # validate 先于任何写入执行：拒绝后原分类原封不动
        with db.formal() as conn:
            assert cats.list_of(conn, mid) == ["daily"]

    def test_add_rejects_joining_exclusive_bucket(self, actors):
        mid = memory.hold(actors["jiaming"], text="剧本桶加分类应被拒",
                          original_title="剧本桶加类",
                          categories=["reloplay"],
                          creation_mode="contemporaneous")["memory_id"]
        with db.formal() as conn:
            with pytest.raises(Forbidden) as exc:
                cats.add(conn, mid, ["sweet"], "jiaming")
        assert exc.value.code == "EXCLUSIVE_CATEGORY"

    def test_add_rejects_adding_exclusive_to_bucket(self, actors):
        mid = memory.hold(actors["jiaming"], text="普通桶加剧本应被拒",
                          original_title="普通桶加剧本",
                          categories=["sweet", "date"],
                          creation_mode="contemporaneous")["memory_id"]
        with db.formal() as conn:
            with pytest.raises(Forbidden) as exc:
                cats.add(conn, mid, ["reloplay"], "jiaming")
        assert exc.value.code == "EXCLUSIVE_CATEGORY"

    def test_hold_accepts_parallel_multi_again(self, actors):
        out = memory.hold(actors["jiaming"], text="平行多选回归的写入路径",
                          original_title="多选回归",
                          categories=["sweet", "date", "milestone"],
                          creation_mode="contemporaneous")
        with db.formal() as conn:
            stored = cats.list_of(conn, out["memory_id"])
        assert stored == ["sweet", "date", "milestone"]


class TestPhasePolicyUnchanged:
    """阶段层不重复设门（根因修复不叠补丁）：写入端已拒的组合，纯函数
    语义不变——有限类取最大 H，永久类覆盖。"""

    def test_multi_takes_max_days(self):
        from datetime import datetime, timezone
        from mariposa.recall import phase_policy as pp
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        held = datetime(2026, 10, 15, tzinfo=timezone.utc)
        r = pp.compute_phase(now=now, first_held_at=held,
                             categories=["sweet", "date"])
        assert r.h_eff_days == 60  # 30 与 60 取最大，不累加
