"""2026-10-05 产品对齐批回归（江乔生裁定四项）。

1. why_remember 字段删除（解释槽归心情层；正文=唯一真相源）
2. 心情层放宽：跨窗口补记允许，evidence_state 如实标注
3. 原始标题必填 + ≤30 字符（全量浏览先扫标题）
4. 桶编号 = 分类字母 a..i + 四位序号，持久计数永不复用
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors, text="对齐批正文", title="对齐标题", cats=None, **kw):
    # 2026-10-06（审计 1005B-R4）：带心情的 hold 须显式 creation_mode
    # （服务端宁拒不猜）；夹具统一补"contemporaneous"，测试意图不变
    if "mood" in kw and "creation_mode" not in kw:
        kw["creation_mode"] = "contemporaneous"
    return memory.hold(actors["jiaming"], text=text,
                       original_title=title,
                       memory_date=kw.pop("memory_date", "2026-06-01"),
                       categories=cats or ["daily"], **kw)


class TestWhyRememberRemoved:
    def test_hold_rejects_why_argument(self, actors):
        """字段已删：经能力入口传 why_remember 是结构化拒绝。"""
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.hold", {
                "text": "正文", "original_title": "标题",
                "categories": ["daily"],
                "creation_mode": "contemporaneous",
                "why_remember": "理由"}, None)
        assert "why_remember" in str(ei.value)

    def test_update_rejects_why_argument(self, actors):
        h = _hold(actors)
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.update", {
                "memory_id": h["memory_id"], "expected_version": 1,
                "why_remember": "新理由"}, None)
        assert "why_remember" in str(ei.value)

    def test_get_no_longer_exposes_field(self, actors):
        h = _hold(actors)
        with db.formal() as conn:
            out = memory.get(conn, h["memory_id"])
        assert "why_remember" not in out


class TestTitleRequired:
    def test_missing_title_rejected(self, actors):
        with pytest.raises(Forbidden) as ei:
            memory.hold(actors["jiaming"], text="无标题正文",
                        memory_date="2026-06-01", categories=["daily"])
        assert ei.value.code == "ORIGINAL_TITLE_REQUIRED"

    def test_overlong_title_rejected(self, actors):
        with pytest.raises(Forbidden) as ei:
            _hold(actors, title="标" * 31)
        assert ei.value.code == "ORIGINAL_TITLE_TOO_LONG"

    def test_schema_level_required(self, actors):
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.hold", {
                "text": "正文", "categories": ["daily"]}, None)
        assert "original_title" in str(ei.value)


class TestBucketIds:
    def test_format_and_series(self, actors):
        """终版裁定：纯五位数全局编号，无分类字母。"""
        a = _hold(actors, cats=["daily"])["memory_id"]
        b = _hold(actors, cats=["sad"])["memory_id"]
        c = _hold(actors, cats=["daily"])["memory_id"]
        assert (a, b, c) == ("00001", "00002", "00003"), \
            "全局单一号段递增，与分类无关"

    def test_no_reuse_after_delete(self, actors):
        """号段永不复用：删除后的重建拿到下一号，不回卷旧号。"""
        from mariposa.deletion import service as ds
        mid = _hold(actors, cats=["daily"])["memory_id"]
        ds.direct_delete("jiaming", mid)
        again = _hold(actors, cats=["daily"])["memory_id"]
        assert mid == "00001" and again == "00002", \
            "此前从存活行取 MAX 会在删除后回卷复用 00001"

    def test_exhaustion_structured(self, actors):
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO bucket_id_counters(category, next)"
                " VALUES('_global', 100000)")
        with pytest.raises(Forbidden) as ei:
            _hold(actors, cats=["daily"])
        assert ei.value.code == "BUCKET_ID_EXHAUSTED"


class TestMoodWindowStillEnforced:
    def test_retrospective_mood_still_rejected(self, actors):
        """回滚守护：当时心情的窗口规矩不被"解释槽"裁定连带放开。"""
        with pytest.raises(Forbidden) as ei:
            _hold(actors, creation_mode="retrospective",
                  mood={"text": "后来补的", "tags": []})
        assert ei.value.code == "MOOD_WINDOW_REQUIRED"

    def test_contemporaneous_mood_labeled_so(self, actors):
        out = _hold(actors, creation_mode="contemporaneous",
                    mood={"text": "当时的心情", "tags": []})
        with db.formal() as conn:
            row = conn.execute(
                "SELECT evidence_state FROM memory_moods"
                " WHERE memory_id=?", (out["memory_id"],)).fetchone()
        assert row["evidence_state"] == "contemporaneous"


class TestLegacyImportNoAutoMood:
    def test_apply_creates_no_mood(self, actors, tmp_path):
        """终裁（不能补写）+ §7.2：旧库导入默认 mood 为空，
        旧 why 文本不迁为心情（mood.write 通道已删）。"""
        from mariposa import migration
        (tmp_path / "2026-07-01 10-00-00 终裁样本_aa11bb22cc33.md").write_text(
            "---\ntype: note\ndate: 2026-07-01\nwhy_remembered: 因为想留住那天\n---\n"
            "终裁迁移正文", encoding="utf-8")
        r = migration.dry_run(str(tmp_path), None)
        assert r["counts"]["total"] == 1
        from tests.unit.test_audit1004_batch2 import _last_report_path
        out = migration.apply_from_report(_last_report_path(tmp_path, r))
        # F-J-23（联合审计返修 2026-10-06）：正式合同无未分类默认值
        # （CATEGORY_REQUIRED）——缺分类列问题报告拒迁（宁拒不猜），
        # 迁移工具不再静默补"日常"落桶
        assert out["applied"] == 0
        assert any(p["issue"] == "categories_missing"
                   for p in out["problems"])
        assert "legacy_why_to_mood" not in out
        with db.formal() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM memory_moods").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM memories").fetchone()[0] == 0


class TestDirectBrowseEntries:
    """2026-10-05 裁定：日期/分类/心情三直达入口，标题优先，不进召回。"""

    def _seed(self, actors):
        a = _hold(actors, text="约会日的正文", title="海边约会",
                  cats=["date"], memory_date="2026-07-09",
                  mood={"text": "很开心想抱抱", "tags": ["开心"]})
        b = _hold(actors, text="日常正文", title="阳台晚餐",
                  cats=["daily"], memory_date="2026-07-09")
        c = _hold(actors, text="旧日常正文", title="搬家那天",
                  cats=["daily"], memory_date="2026-06-01")
        return a, b, c

    def test_by_date_uses_event_date_title_first(self, actors):
        from mariposa.memory import listing
        self._seed(actors)
        out = listing.by_date("2026-07-09")
        titles = {i["original_title"] for i in out["items"]}
        assert titles == {"海边约会", "阳台晚餐"}, \
            "按真实发生日期直达；标题优先返回"
        for i in out["items"]:
            assert "text" not in i and "hold_text" not in i, \
                "卡片不带正文（正文按需 memory.get）"

    def test_by_category_direct_and_paged(self, actors):
        from mariposa.memory import listing
        self._seed(actors)
        out = listing.by_category("daily", limit=1)
        assert [i["original_title"] for i in out["items"]] == ["阳台晚餐"]
        assert out["has_more"] is True
        page2 = listing.by_category(
            "daily", limit=1,
            cursor_date=out["next_cursor"]["memory_date"],
            cursor_id=out["next_cursor"]["memory_id"])
        assert [i["original_title"] for i in page2["items"]] == ["搬家那天"]
        assert page2["has_more"] is False

    def test_by_category_rejects_unknown(self, actors):
        from mariposa.memory import listing
        with pytest.raises(Forbidden):
            listing.by_category("not-a-cat")

    def test_by_emotion_optional_tag(self, actors):
        from mariposa.memory import listing
        self._seed(actors)
        allb = listing.by_emotion("")
        assert len(allb["items"]) == 3, "不选大类=全量返回"
        happy = listing.by_emotion("开心")
        assert [i["original_title"] for i in happy["items"]] == ["海边约会"]
        assert happy["items"][0]["mood_tags"] == ["开心"]

    def test_registry_paths_for_jiaming(self, actors):
        """周家明经 MCP 同口径直达（owners 授权 + 传参形状）。"""
        self._seed(actors)
        r1 = registry.invoke(actors["jiaming"], "memory.by_date",
                             {"date": "2026-07-09"}, None)
        assert r1["data"]["matched_by"] == "date"
        r2 = registry.invoke(actors["jiaming"], "memory.by_category",
                             {"category": "date"}, None)
        assert r2["data"]["items"][0]["original_title"] == "海边约会"
        r3 = registry.invoke(actors["jiaming"], "memory.by_emotion",
                             {"tag": "开心"}, None)
        assert r3["data"]["mood_tag"] == "开心"


class TestSemanticProviderErrorDegrades:
    def test_keyword_path_survives_provider_failure(self, actors, monkeypatch):
        """裁定 §二.5：模型挂了不 500、不偷偷兜底——关键词照常+如实降级。"""
        from mariposa import config as _cfg
        from mariposa.retrieval import search as rsearch, semantic
        _hold(actors, text="语义降级时的正文蓝风铃", title="降级测试")
        monkeypatch.setattr(_cfg, "SEMANTIC_PROVIDER", "local_bge_zh")

        def _boom(*a, **k):
            raise RuntimeError("model load failed (synthetic)")

        monkeypatch.setattr(semantic, "embed", _boom)
        with db.formal() as conn:
            out = rsearch.search(conn, "蓝风铃")
        assert out["hits"], "关键词主路径不受补充路径故障拖死"
        assert out["degraded"] == "semantic_provider_error"
        assert out["semantic"] == "error"
        assert out["mode"] == "keyword", "不得伪装 hybrid"


class TestMoodMajorCategoriesOnly:
    """冻结裁定（当晚修订）：标签槽只存 8 个固定大类；子心情不预
    定义——自由表达落 mood_note，可写可不写。"""

    def test_only_categories_accepted_at_hold(self, actors):
        from mariposa.errors import Forbidden as _F
        with pytest.raises(_F) as ei:
            _hold(actors, mood={"text": None, "tags": ["酸"]})
        assert ei.value.code == "MOOD_CATEGORY_REQUIRED"
        # 大类合法；子心情自由句落 note
        out = _hold(actors, mood={"text": "开心想抱抱", "tags": ["开心"]})
        with db.formal() as conn:
            got = memory.get(conn, out["memory_id"])["mood"]
        assert got["tags"] == ["开心"]
        assert got["text"] == "开心想抱抱"

    def test_max_three_categories(self, actors):
        from mariposa.errors import Forbidden as _F
        with pytest.raises(_F) as ei:
            _hold(actors, mood={"text": None,
                                "tags": ["开心", "爱", "吃醋", "生气"]})
        assert ei.value.code == "MOOD_TAGS_LIMIT"

    def test_empty_mood_still_legal(self, actors):
        out = _hold(actors)  # 不带 mood
        with db.formal() as conn:
            got = memory.get(conn, out["memory_id"])
        assert "mood" not in got

    def test_by_emotion_filters_by_category_only(self, actors):
        from mariposa.memory import listing
        from mariposa.errors import Forbidden as _F
        _hold(actors, text="占有桶", title="占有",
              mood={"text": None, "tags": ["吃醋"]})
        _hold(actors, text="亲密桶", title="亲密",
              mood={"text": None, "tags": ["爱"]})
        out = listing.by_emotion("吃醋")
        assert {i["original_title"] for i in out["items"]} == {"占有"}
        assert out["scope"] == "大类·吃醋"
        with pytest.raises(_F) as ei:  # 旧词不再是筛选键（吃醋已升为大类）
            listing.by_emotion("占有")
        assert ei.value.code == "MOOD_CATEGORY_REQUIRED"

class TestMoodVocabInterface:
    def test_open_interface_returns_taxonomy(self, actors):
        out = registry.invoke(actors["jiaming"], "memory.mood.vocab",
                              {}, None)["data"]
        assert out["categories"] == ["开心", "爱", "生气", "吃醋",
                                     "悲伤", "渴望", "不安"]
        assert out["rules"]["max_tags"] == 3
        assert "不能补写" in out["rules"]["write_window"]
        assert "不必" in out["rules"]["sub_mood"]

    def test_recall_filter_accepts_major_category(self, actors):
        """召回过滤与 by_emotion 同语义：按大类直筛。"""
        from mariposa.retrieval import search as rsearch
        a = _hold(actors, text="占有正文", title="占有",
                  mood={"text": None, "tags": ["吃醋"]})
        _hold(actors, text="无关正文", title="无关",
              mood={"text": None, "tags": ["开心"]})
        with db.formal() as conn:
            out = rsearch.recall(conn, filters={"mood_tags": ["吃醋"]})
        ids = {h["memory_id"] for h in out["hits"]}
        assert ids == {a["memory_id"]}, "大类·占有 只命中占有桶"

    def test_recall_filter_rejects_unknown_word(self, actors):
        from mariposa.retrieval import search as rsearch
        from mariposa.errors import Forbidden as _F
        with pytest.raises(_F) as ei:
            with db.formal() as conn:
                rsearch.recall(conn, filters={"mood_tags": ["占有"]})
        assert ei.value.code == "MOOD_CATEGORY_REQUIRED"


class TestEstomagoBuiltinBinding:
    """内置绑定（她的"内置感觉"裁定）：jiaming 名下受限白名单，
    只放行 hold 面两个能力，泄漏面最小化。"""

    def _binding(self, token: str):
        # F-J-55（联合审计返修 2026-10-06）：白名单与发币脚本同源（脚本有
        # __main__ 守卫，导入无副作用）——此前硬编码 2 项，R5 补的
        # hold.status 不在夹具里，未来加能力时夹具先行腐化
        import hashlib
        import json as _json
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "issue_estomago_hold_token",
            __file__.rsplit("tests", 1)[0]
            + "scripts/issue_estomago_hold_token.py")
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO client_bindings(binding_id, token_hash,"
                " principal_id, entry_source, capabilities_allowlist,"
                " created_at) VALUES(?,?,?,?,?,datetime('now'))",
                ("bdg_est_test", hashlib.sha256(token.encode()).hexdigest(),
                 "jiaming", "estomago_builtin",
                 _json.dumps(_mod.ALLOWLIST)))
        return identity.authenticate(token)

    def test_hold_and_vocab_allowed(self, actors):
        import secrets as _sec
        p = self._binding(_sec.token_urlsafe(16))
        out = registry.invoke(p, "memory.hold", {
            "text": "内置绑定写入的正文", "original_title": "内置绑定",
            "categories": ["daily"],
            "creation_mode": "contemporaneous"}, "es-builtin-1")
        assert out["data"]["memory_id"]
        vocab = registry.invoke(p, "memory.mood.vocab", {}, None)
        assert vocab["data"]["categories"][0] == "开心"
        # R5/F-J-55：hold.status 在脚本白名单内（5xx 后换窗恢复对账依赖）
        st = registry.invoke(p, "memory.hold.status",
                             {"operation_id": "es-builtin-1"}, None)
        assert st["data"]["completed"] is False

    def test_everything_else_denied(self, actors):
        import secrets as _sec
        p = self._binding(_sec.token_urlsafe(16))
        from mariposa.errors import Forbidden as _F
        # 读检索/直达/删除/维护面：白名单外一律拒（token 泄漏也只伤 hold）
        for cap, args in [("memory.search", {"query": "x"}),
                          ("memory.by_date", {"date": "2026-01-01"}),
                          ("memory.delete", {"memory_id": "x",
                                             "operation_id": "y"}),
                          ("maintenance.activity.list", {})]:
            with pytest.raises(_F) as ei:
                registry.invoke(p, cap, args, None)
            assert ei.value.code == "FORBIDDEN", cap

    def test_revocation_kills_binding(self, actors):
        import secrets as _sec
        token = _sec.token_urlsafe(16)
        self._binding(token)
        identity.revoke_binding("qiaosheng", "bdg_est_test")
        from mariposa.errors import Unauthenticated
        with pytest.raises(Unauthenticated):
            identity.authenticate(token)
