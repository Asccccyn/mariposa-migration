"""Memory/Bootstrap 域审计修复回归（2026-10-02 基线审计 P2 批）。

- CB-044：I 当前条目与 aggregate version 同一读快照（i_mixed_
  snapshot——item_revise 插在两次 SELECT 间曾产出旧正文挂新版本）。
- CB-045：Bootstrap 公开 schema 与现行 handler/service 对齐（续页
  object cursor + section 枚举、loaded_snapshot_id 不再被拒）。
- CB-046：state hash 覆盖 mood 文本/标签与 categories（bootstrap_
  mood_hash / bootstrap_category_hash——单项变更后不得 unchanged）。
- CB-049：memory.list 复合 keyset（同日多桶完整续页；memory_
  pagination——同日 3 条 limit2 曾永久漏 1 条）。
"""
from __future__ import annotations

import threading

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.identity_i import service as i_svc
from mariposa.memory import service as memory
from mariposa.bootstrap import service as boot
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-10-01",
                date_confidence="exact", original_title="mb",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


# ---------------------------------------------------------------- CB-044

class TestIConsistentSnapshot:

    def test_items_and_version_same_snapshot(self, actors, monkeypatch):
        """item_revise 插在 items 与 document 两次读之间——返回的
        items 聚合与 version 必须同源（不再旧正文挂新版本）。"""
        i_svc.item_create("jiaming", "修订前正文")
        orig_items = i_svc._current_items

        fired = [False]

        def hooked_items(conn):
            rows = orig_items(conn)
            if rows and not fired[0]:
                fired[0] = True
                # 合法交错：_current_items 完成后、i_documents 读前，
                # 经独立连接提交 revise（读事务在 WAL 下不阻塞写，
                # 但本事务继续读旧快照——一致性正来自这里）
                import subprocess, sys, os, json as _j
                env = dict(os.environ,
                           MARIPOSA_ROOT=os.environ["MARIPOSA_ROOT"],
                           MARIPOSA_ALLOW_CREATE="1")
                code = (
                    "import sys; sys.path.insert(0, 'backend');"
                    " sys.path.insert(0, '.')\n"
                    "from mariposa.identity_i import service as i_svc\n"
                    f"i_svc.item_revise('jiaming', {rows[0]['item_id']!r},"
                    f" '修订后正文', {rows[0]['revision']})\n")
                r = subprocess.run([sys.executable, "-c", code],
                                   capture_output=True, text=True, env=env,
                                   cwd=os.getcwd(), timeout=60)
                assert r.returncode == 0, r.stderr
            return rows

        monkeypatch.setattr(i_svc, "_current_items", hooked_items)
        out = i_svc.get()
        monkeypatch.undo()
        # 修订已发生：要么整体旧快照（items 旧 + version 旧），要么
        # 整体新——不得 items 旧 revision1 + version2 混合
        version_of_items = max(i["revision"] for i in out["items"])
        assert out["version"] in (version_of_items, version_of_items - 1), \
            f"聚合 version 与 items 必须同快照：{out['version']} vs" \
            f" {version_of_items}"
        assert "修订后正文" in (out["content"] or "") or \
            "修订前正文" in (out["content"] or "")


# ---------------------------------------------------------------- CB-045

class TestBootstrapPublicContract:

    def test_next_page_object_cursor_accepted(self, actors):
        b1 = registry.invoke(actors["jiaming"], "bootstrap.get",
                             {"profile": "claude_chat"}, None)["data"]
        cur = (b1.get("memory_days", {}).get("next_cursor")
               or b1.get("plans", {}).get("next_cursor"))
        if not cur:
            pytest.skip("首屏无续页游标（数据不足一页）")
        out = registry.invoke(actors["jiaming"], "bootstrap.next", {
            "snapshot_id": b1["snapshot_id"], "cursor": cur,
            "section": "memory_days" if "memory_days" in b1 and
            b1["memory_days"].get("next_cursor") else "plans"}, None)
        assert out["ok"] is True, "object cursor 续页不得被 schema 拒绝"

    def test_loaded_snapshot_id_accepted(self, actors):
        b1 = registry.invoke(actors["jiaming"], "bootstrap.get",
                             {"profile": "claude_chat"}, None)["data"]
        out = registry.invoke(actors["jiaming"], "bootstrap.get", {
            "profile": "claude_chat",
            "loaded_snapshot_id": b1["snapshot_id"]}, None)["data"]
        assert out.get("unchanged") is True, \
            "loaded_snapshot_id 去重参数必须被公开 schema 接受"

    def test_known_snapshot_id_mapped(self, actors):
        """RA-007：旧公开参数映射为去重语义（不再被静默忽略）。"""
        b1 = registry.invoke(actors["jiaming"], "bootstrap.get",
                             {"profile": "claude_chat"}, None)["data"]
        out = registry.invoke(actors["jiaming"], "bootstrap.get", {
            "profile": "claude_chat",
            "known_snapshot_id": b1["snapshot_id"]}, None)["data"]
        assert out.get("unchanged") is True, \
            "known_snapshot_id 必须实现去重（审计反例：返回整新包）"


# ---------------------------------------------------------------- CB-046

class TestStateHashCompleteness:

    def test_mood_change_invalidates(self, actors):
        """CB-046：mood 行变更使旧快照失效（mood.write 已删，直改行
        模拟数据变化——快照指纹语义与写入通道无关）。"""
        out = _hold(actors, "快照正文",
                    mood={"text": "初版心情", "tags": ["开心"]})
        mid = out["memory_id"]
        b1 = boot.get("jiaming", "claude_chat", "claude_chat")
        with db.formal() as conn:
            conn.execute(
                "UPDATE memory_moods SET mood_text='变更为新心情'"
                " WHERE memory_id=?", (mid,))
        # mood 单项变更必须使旧快照失效（薄响应路径抛 SnapshotStale）
        with pytest.raises(Exception) as ei:
            boot.get("jiaming", "claude_chat", "claude_chat",
                     loaded_snapshot_id=b1["snapshot_id"])
        assert "changed" in str(ei.value) or ei.value.__class__.__name__ \
            == "SnapshotStale"

    def test_categories_change_invalidates(self, actors):
        _hold(actors, "分类指纹正文")
        b1 = boot.get("jiaming", "claude_chat", "claude_chat")
        with db.formal() as conn:
            mid = conn.execute(
                "SELECT memory_id FROM memories ORDER BY created_at DESC"
            ).fetchone()["memory_id"]
        registry.invoke(actors["jiaming"], "memory.categories.replace", {
            "memory_id": mid, "categories": ["sweet"]}, None)
        with pytest.raises(Exception) as ei:
            boot.get("jiaming", "claude_chat", "claude_chat",
                     loaded_snapshot_id=b1["snapshot_id"])
        assert ei.value.__class__.__name__ == "SnapshotStale", \
            "分类单项变更必须使旧快照失效"


# ---------------------------------------------------------------- CB-049

class TestMemoryListKeyset:

    def test_same_day_pagination_complete(self, actors):
        ids = [_hold(actors, f"同日桶{i}")["memory_id"] for i in range(3)]
        p1 = registry.invoke(actors["jiaming"], "memory.list",
                             {"limit": 2}, None)["data"]
        assert len(p1["items"]) == 2
        cur = p1["next_cursor"]
        assert cur and "memory_id" in cur, "复合游标必须含同日 tiebreaker"
        p2 = registry.invoke(actors["jiaming"], "memory.list",
                             {"limit": 2, "cursor": cur}, None)["data"]
        seen = {i["memory_id"] for i in p1["items"] + p2["items"]}
        assert set(ids) <= seen, \
            f"同日三桶必须完整可达：缺 {set(ids) - seen}"

    def test_legacy_cursor_date_still_works(self, actors):
        _hold(actors, "旧游标桶")
        out = registry.invoke(actors["jiaming"], "memory.list",
                              {"limit": 2, "cursor_date": "2027-01-01"},
                              None)["data"]
        assert out["count"] >= 1
