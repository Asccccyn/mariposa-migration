"""v2 审查闭环（P4）：V2-REV-01..14 子集 + 到期队列衔接。

生成者（worker）无正式写入权；林石见受限可改候选摘要/tags 并放行干净
到期项；原始字段只读；保留线索挂起；终裁 keep 是终局。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, ProposalStale
from mariposa.identity import service as identity
from mariposa.memory import retention as ret_mod
from mariposa.memory import service as memory
from mariposa.memory import views as views_mod
from mariposa.retrieval import search as rsearch
from mariposa.workspace import review as review_mod
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent",
                                     "gpt_chat", "bw"),
        "linshijian": identity.Principal("linshijian", "林石见", "agent",
                                         "review_mcp", "bl"),
    }


def make_due(actors, text="到期事件", cats=("daily",), **kw):
    """造一个已到期的 v2 桶（basis 直接拨回 N 天前）。"""
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    out = memory.hold(actors["jiaming"], text=text, memory_date="2026-06-01",
                      date_confidence="exact", original_title=kw.pop(
                          "title", "原标题"),
                      categories=list(cats), creation_mode="contemporaneous",
                      raw_pending=False, **kw)
    with db.formal() as conn:
        row = ret_mod.compute_row(list(cats), old)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE memory_retention SET basis_at=?, basis_date=?, due_date=?,"
            " next_due_at=? WHERE memory_id=?",
            (old, row["basis_date"], row["due_date"], row["next_due_at"],
             out["memory_id"]))
        conn.execute("COMMIT")
    return out


def gen(actors, mid, summary="平凡的夜晚，一起吃完饭收拾了厨房。",
        tags=None):
    review_mod.ensure_default_delegation()
    out = review_mod.generate(actors["worker"], memory_id=mid,
                              candidate_summary=summary,
                              candidate_tags=tags or ["日常"])
    return out["created"][0]


class TestPermissions:
    def test_worker_cannot_release_rev01(self, actors):
        m = make_due(actors)
        item = gen(actors, m["memory_id"])
        # worker 不能领审/提交
        with pytest.raises(Forbidden):
            review_mod.claim(actors["worker"])
        with pytest.raises(Forbidden):
            review_mod.submit(actors["worker"], item["item_id"], "release", 1)

    def test_linshijian_cannot_touch_original_fields_rev03(self, actors):
        m = make_due(actors)
        item = gen(actors, m["memory_id"])
        review_mod.claim(actors["linshijian"])
        for changes in ({"event_date": "2020-01-01"},
                        {"original_title": "改标题"},
                        {"categories": ["sad"]},
                        {"mood_tags": ["伤心"]},
                        {"event_text": "改正文"}):
            with pytest.raises(Forbidden) as e:
                review_mod.revise(actors["linshijian"], item["item_id"],
                                  changes)
            assert e.value.detail.get("code") == "FIELD_NOT_REVIEWABLE"

    def test_linshijian_can_revise_summary_and_tags_rev02(self, actors):
        m = make_due(actors)
        item = gen(actors, m["memory_id"])
        review_mod.claim(actors["linshijian"])
        out = review_mod.revise(actors["linshijian"], item["item_id"],
                                {"summary_body": "修订后的摘要。",
                                 "forget_tags": ["家务", "夜晚"]})
        assert out["revision"] == 2
        got = review_mod.get_item(actors["linshijian"], item["item_id"])
        assert got["versions"][0]["summary_body"].startswith("平凡的夜晚")
        assert got["versions"][1]["summary_body"] == "修订后的摘要。"  # 原稿留底
        assert got["tags_diff"]["changed"] is True


class TestReleaseFlow:
    def test_clean_due_release_applies_rev04(self, actors):
        m = make_due(actors, text="独有旧词霭晞的事件")
        item = gen(actors, m["memory_id"], summary="概括后的新词氤氲。")
        review_mod.claim(actors["linshijian"])
        out = review_mod.submit(actors["linshijian"], item["item_id"],
                                "release", 1)
        assert out["state"] == "forgotten"
        with db.formal() as conn:
            mem = memory.get(conn, m["memory_id"])
            sv = conn.execute(
                "SELECT summary_body, forget_tags FROM"
                " memory_summary_versions WHERE memory_id=?"
                " ORDER BY summary_version DESC LIMIT 1",
                (m["memory_id"],)).fetchone()
        assert mem["representation"] == "forgotten_summary"
        assert mem["text"] == "概括后的新词氤氲。"
        assert json.loads(sv["forget_tags"]) == ["日常"]
        # 检索切换：旧词失效、新词命中（SEARCH-11）
        with db.formal() as conn:
            assert not rsearch.recall(conn, "霭晞")["hits"]
            hits = rsearch.recall(conn, "氤氲")["hits"]
        assert any(h["memory_id"] == m["memory_id"] for h in hits)

    def test_open_after_generation_blocks_release_rev13_ret08(self, actors):
        m = make_due(actors)
        item = gen(actors, m["memory_id"])
        review_mod.claim(actors["linshijian"])
        # 生成后真实打开续期 → 旧批准失效（打开先提交：PROPOSAL_STALE /
        # NOT_DUE 皆为其体表现，均不得执行）
        o = views_mod.open_memory(actors["jiaming"], m["memory_id"])
        views_mod.confirm_view(actors["jiaming"], m["memory_id"],
                               o["view_receipt"])
        with pytest.raises((Forbidden, ProposalStale)):
            review_mod.submit(actors["linshijian"], item["item_id"],
                              "release", 1)

    def test_source_version_change_stale_rev13(self, actors):
        m = make_due(actors)
        item = gen(actors, m["memory_id"])
        review_mod.claim(actors["linshijian"])
        # 源内容版本推进（恢复/新版本）
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET current_version_no=current_version_no+1,"
                " updated_at=datetime('now') WHERE memory_id=?",
                (m["memory_id"],))
        with pytest.raises(ProposalStale):
            review_mod.submit(actors["linshijian"], item["item_id"],
                              "release", 1)


class TestRetainHints:
    def _escalated(self, actors, **holdkw):
        m = make_due(actors, **holdkw)
        item = gen(actors, m["memory_id"])
        review_mod.claim(actors["linshijian"])
        return m, item

    def test_shared_words_escalate_to_jiaming_rev05(self, actors):
        m, item = self._escalated(
            actors, text="两人都说了一句话",
            our_words=[{"speaker": "jiaming", "text": "今晚月色真美"},
                       {"speaker": "qiaosheng", "text": "今晚月色真美"}])
        got = review_mod.get_item(actors["linshijian"], item["item_id"])
        hints = got["retain_hints"]
        assert any(h["kind"] == "shared_expression" for h in hints)
        out = review_mod.submit(actors["linshijian"], item["item_id"],
                                "escalate_jiaming", 1)
        assert out["state"] == "needs_jiaming_decision"
        # 乔生不能裁共同话语
        with pytest.raises(Forbidden):
            review_mod.decide_retention(actors["qiaosheng"], item["item_id"],
                                        "keep")

    def test_first_time_keyword_hint_with_context_rev06(self, actors):
        m, item = self._escalated(actors, text="这是我们第一次一起看海")
        got = review_mod.get_item(actors["linshijian"], item["item_id"])
        kw_hints = [h for h in got["retain_hints"]
                    if h["kind"] == "keyword_retain_hint"]
        assert kw_hints and kw_hints[0]["context"]  # 附上下文，不机械定留
        out = review_mod.submit(actors["linshijian"], item["item_id"],
                                "escalate_retain", 1)
        assert out["state"] == "needs_owner_decision"

    def test_retain_hint_blocks_release_rev07(self, actors):
        from mariposa.memory import recollections as rec_mod
        # 先在未到期时写回忆（明确打开+确认+追加），再把周期推到期
        m = memory.hold(actors["jiaming"], text="普通事",
                        memory_date="2026-06-01", date_confidence="exact",
                        original_title="原标题", categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
        o = views_mod.open_memory(actors["jiaming"], m["memory_id"])
        views_mod.confirm_view(actors["jiaming"], m["memory_id"],
                               o["view_receipt"])
        rec_mod.append(actors["jiaming"], m["memory_id"], o["view_receipt"],
                       "后来又想起这件事")
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        with db.formal() as conn:
            row = ret_mod.compute_row(["daily"], old)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE memory_retention SET basis_at=?, basis_date=?,"
                " due_date=?, next_due_at=? WHERE memory_id=?",
                (old, row["basis_date"], row["due_date"], row["next_due_at"],
                 m["memory_id"]))
            conn.execute("COMMIT")
        item = gen(actors, m["memory_id"])
        review_mod.claim(actors["linshijian"])
        with pytest.raises(Forbidden) as e:
            review_mod.submit(actors["linshijian"], item["item_id"],
                              "release", 1)
        assert e.value.detail.get("code") == "RETAIN_HINT_PENDING"

    def test_keep_is_final_no_resubmission_ret11_rev08(self, actors):
        m, item = self._escalated(actors, text="带留线索的事 第一次")
        review_mod.submit(actors["linshijian"], item["item_id"],
                          "escalate_retain", 1)
        out = review_mod.decide_retention(actors["jiaming"], item["item_id"],
                                          "keep")
        assert out["state"] == "retained"
        with db.formal() as conn:
            r = ret_mod.get(conn, m["memory_id"])
        assert r["status"] == "retained" and r["due_date"] is None
        # 再生成被拒：确定留不再送审
        with pytest.raises(Forbidden):
            gen(actors, m["memory_id"])


class TestSummaryConstraints:
    def test_banned_terms_rejected_rev09(self, actors):
        m = make_due(actors)
        with pytest.raises(Forbidden) as e:
            gen(actors, m["memory_id"], summary="用户与AI助手的互动记录")
        assert e.value.detail.get("code") == "SUMMARY_TERM_BANNED"
        item = gen(actors, m["memory_id"], summary="正常摘要")
        review_mod.claim(actors["linshijian"])
        with pytest.raises(Forbidden):
            review_mod.revise(actors["linshijian"], item["item_id"],
                              {"summary_body": "角色扮演的夜晚"})

    def test_revision_mismatch_stale(self, actors):
        m = make_due(actors)
        item = gen(actors, m["memory_id"])
        review_mod.claim(actors["linshijian"])
        with pytest.raises(ProposalStale):
            review_mod.submit(actors["linshijian"], item["item_id"],
                              "release", 99)


class TestRegistryWiring:
    def test_full_loop_via_registry_and_mcp_profile(self, actors):
        m = make_due(actors)
        item = registry.invoke(actors["worker"], "workspace.forgetting.generate",
                               {"memory_id": m["memory_id"],
                                "candidate_summary": "一次平常的家务夜晚。",
                                "candidate_tags": ["日常"]}, None)["data"]["created"][0]
        claimed = registry.invoke(actors["linshijian"], "workspace.review.claim",
                                  {}, None)["data"]
        assert claimed["item_id"] == item["item_id"]
        out = registry.invoke(actors["linshijian"], "workspace.review.submit",
                              {"item_id": item["item_id"], "decision": "release",
                               "expected_revision": 1}, None)["data"]
        assert out["state"] == "forgotten"

    def test_linshijian_not_allowed_on_business_mcp(self, actors):
        # 维护 profile 可以；业务 profile 拒绝（路径不赋予角色，binding 决定）
        from fastapi.testclient import TestClient
        from mariposa.app import app
        with TestClient(app) as c:
            r = c.post("/mcp/maintenance", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Authorization": "Bearer tok-l"})
            names = {t["name"] for t in r.json()["result"]["tools"]}
            assert "mariposa_workspace_review_claim" in names
