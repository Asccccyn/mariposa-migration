"""2026-09-23 独立审计修复回归（对应 V2-RET-08/09/11、V2-REV-05/13、
V2-SEARCH-03/05、V2-OPS-02、V2-BOOT-05、V2-PLAN-02）。

每条用例对应审计发现的真实缺陷（复现脚本 .pytest_tmp/audit_repro.py
口径），修复前后行为差异见 docs/AUDIT_REPORT_v2_20260923.md。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.capabilities import input_schemas, registry
from mariposa.errors import Forbidden, NotFound, ProposalStale, SnapshotStale
from mariposa.identity import service as identity
from mariposa.memory import retention as ret_mod
from mariposa.memory import service as memory
from mariposa.memory import views as views_mod
from mariposa.retrieval import search as rsearch
from mariposa.plans import service as plans
from mariposa.workspace import due_queue
from mariposa.workspace import review as review_mod
from mariposa.workspace import service as workspace
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    review_mod.ensure_default_delegation()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent",
                                     "gpt_chat", "bw"),
        "linshijian": identity.Principal("linshijian", "林石见", "human",
                                         "mcp", "bl"),
    }


def hold_due(actors, key: str, cats=("daily",)) -> str:
    """v2 hold 并把首次 hold 时刻拨到 40 天前（已到期）。"""
    out = memory.hold(
        actors["jiaming"], text=f"事件正文-{key}",
        original_title=f"标题-{key}", categories=list(cats),
        creation_mode="contemporaneous", memory_date="2026-08-01")
    mid = out["memory_id"]
    past = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    with db.formal() as conn:
        ret_mod.create_for_memory(conn, mid, ["daily"], past)
    return mid


def queue_targets() -> set[str]:
    return {r["target_id"] for r in due_queue.due_items()}


class TestRetainedQueueResidue:
    """V2-RET-11：keep 终局后队列不残留、generate 不被终局目标中断。"""

    def test_keep_clears_queue_and_next_generate_survives(self, actors):
        m1 = hold_due(actors, "m1")
        m2 = hold_due(actors, "m2")
        g = review_mod.generate(actors["worker"], candidate_summary="候选稿")
        assert len(g["created"]) == 2
        item = g["created"][0]["item_id"]
        with db.workspace() as wconn:
            wconn.execute(
                "UPDATE v2_review_items SET claimed_by='linshijian'"
                " WHERE item_id=?", (item,))
        review_mod.submit(actors["linshijian"], item, "escalate_retain", 1)
        kept = review_mod.decide_retention(actors["qiaosheng"], item, "keep")
        assert kept["state"] == "retained"
        kept_target = g["created"][0]["target_id"]
        assert kept_target not in queue_targets()  # 终局桶出队
        # 第二轮 generate：队列里仍有另一桶，不抛 RETAINED_FINAL
        g2 = review_mod.generate(actors["worker"], candidate_summary="次轮")
        assert "skipped" not in g2 or all(
            s["reason"] == "RETAINED_FINAL" for s in g2.get("skipped", []))

    def test_retained_target_reports_skipped_not_crash(self, actors):
        """终局桶即使因旧残留仍在队里，也只记 skipped 不中断整批。"""
        m1 = hold_due(actors, "keepme")
        m2 = hold_due(actors, "other")
        g = review_mod.generate(actors["worker"], memory_id=m1,
                                candidate_summary="稿")
        item = g["created"][0]["item_id"]
        with db.workspace() as wconn:
            wconn.execute(
                "UPDATE v2_review_items SET claimed_by='linshijian'"
                " WHERE item_id=?", (item,))
        review_mod.submit(actors["linshijian"], item, "escalate_retain", 1)
        review_mod.decide_retention(actors["qiaosheng"], item, "keep")
        # 人为塞回一条 pending 行模拟旧版本残留
        with db.formal() as conn:
            due_queue.enqueue(conn, "memory", m1,
                              "2020-01-01")
        out = review_mod.generate(actors["worker"], candidate_summary="批")
        assert any(s["memory_id"] == m1 and s["reason"] == "RETAINED_FINAL"
                   for s in out.get("skipped", []))
        assert any(c["target_id"] == m2 for c in out["created"])


class TestOwnerContinueReverify:
    """V2-RET-08/REV-13：终裁 continue 生效前全量核验。"""

    def _escalated_item(self, actors, key="oc"):
        mid = hold_due(actors, key)
        g = review_mod.generate(actors["worker"], memory_id=mid,
                                candidate_summary="候选摘要")
        item_id = g["created"][0]["item_id"]
        with db.workspace() as wconn:
            wconn.execute(
                "UPDATE v2_review_items SET claimed_by='linshijian'"
                " WHERE item_id=?", (item_id,))
        review_mod.submit(actors["linshijian"], item_id, "escalate_retain", 1)
        return mid, item_id

    def test_renewal_blocks_owner_continue(self, actors):
        """明确打开续期后，旧候选稿不得凭 owner-continue 生效。"""
        mid, item_id = self._escalated_item(actors)
        opened = views_mod.open_memory(actors["jiaming"], mid)
        views_mod.confirm_view(actors["jiaming"], mid, opened["view_receipt"])
        with db.formal() as conn:
            assert not ret_mod.is_due(conn, mid)
        with pytest.raises((ProposalStale, Forbidden)):
            review_mod.decide_retention(actors["qiaosheng"], item_id,
                                        "continue")
        with db.formal() as conn:
            m = conn.execute(
                "SELECT compression_state FROM memories WHERE memory_id=?",
                (mid,)).fetchone()
        assert m["compression_state"] == "full"  # 未被遗忘

    def test_permanent_category_change_blocks_owner_continue(self, actors):
        """分类改成永久类后（版本号未动），continue 仍必须拒绝。"""
        mid, item_id = self._escalated_item(actors, key="perm")
        from mariposa.memory import categories as cats_mod
        with db.formal() as conn:
            cats_mod.replace(conn, mid, ["milestone"], "jiaming")
            ret_mod.recompute(conn, mid)
        with pytest.raises((ProposalStale, Forbidden)):
            review_mod.decide_retention(actors["qiaosheng"], item_id,
                                        "continue")
        with db.formal() as conn:
            m = conn.execute(
                "SELECT compression_state FROM memories WHERE memory_id=?",
                (mid,)).fetchone()
        assert m["compression_state"] == "full"


class TestStaleSelfHealing:
    """状态机"未执行阶段→stale"：续期/源变后的旧项不再卡死同桶。"""

    def test_renewal_stales_item_then_next_cycle_recreates(self, actors):
        mid = hold_due(actors, "stale1")
        g = review_mod.generate(actors["worker"], memory_id=mid,
                                candidate_summary="候选")
        item_id = g["created"][0]["item_id"]
        with db.workspace() as wconn:
            wconn.execute(
                "UPDATE v2_review_items SET claimed_by='linshijian'"
                " WHERE item_id=?", (item_id,))
        # 打开续期 → release 拒绝（NOT_DUE）且项落 stale
        opened = views_mod.open_memory(actors["jiaming"], mid)
        views_mod.confirm_view(actors["jiaming"], mid, opened["view_receipt"])
        with pytest.raises((ProposalStale, Forbidden)):
            review_mod.submit(actors["linshijian"], item_id, "release", 1)
        with db.workspace() as wconn:
            st = wconn.execute(
                "SELECT state FROM v2_review_items WHERE item_id=?",
                (item_id,)).fetchone()["state"]
        assert st == "stale"
        # 下一轮到期（把 due 再拨回过去）后可建新审查项，不再被旧项占位
        past = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        with db.formal() as conn:
            ret_mod.create_for_memory(conn, mid, ["daily"], past)
        g2 = review_mod.generate(actors["worker"], memory_id=mid,
                                 candidate_summary="新候选")
        assert g2["created"]


class TestSharedExpressionRouting:
    """V2-REV-05：共同话语终裁限定周家明（不被升级路由绕过）。"""

    def _shared_item(self, actors):
        mid = memory.hold(
            actors["jiaming"], text="两人同句",
            original_title="标题", categories=["daily"],
            our_words=[{"speaker": "jiaming", "text": "今晚月色真美"},
                       {"speaker": "qiaosheng", "text": "今晚月色真美"}],
            creation_mode="contemporaneous")
        mid = mid["memory_id"]
        past = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        with db.formal() as conn:
            ret_mod.create_for_memory(conn, mid, ["daily"], past)
        g = review_mod.generate(actors["worker"], memory_id=mid,
                                candidate_summary="候选")
        item_id = g["created"][0]["item_id"]
        with db.workspace() as wconn:
            wconn.execute(
                "UPDATE v2_review_items SET claimed_by='linshijian'"
                " WHERE item_id=?", (item_id,))
        return mid, item_id

    def test_escalate_owner_rejected_for_jiaming_owned_hint(self, actors):
        _, item_id = self._shared_item(actors)
        with pytest.raises(Forbidden) as ei:
            review_mod.submit(actors["linshijian"], item_id,
                              "escalate_owner", 1)
        assert ei.value.code == "ESCALATE_TARGET_REQUIRED"

    def test_qiaosheng_cannot_decide_jiaming_owned_even_if_mislabeled(
            self, actors):
        """即使项被标成 needs_owner_decision，共同话语仍限定周家明。"""
        _, item_id = self._shared_item(actors)
        review_mod.submit(actors["linshijian"], item_id, "escalate_jiaming", 1)
        # 人为改状态模拟路由错误/旧数据
        with db.workspace() as wconn:
            wconn.execute(
                "UPDATE v2_review_items SET state='needs_owner_decision'"
                " WHERE item_id=?", (item_id,))
        with pytest.raises(Forbidden):
            review_mod.decide_retention(actors["qiaosheng"], item_id, "keep")


class TestV1LoopIsolation:
    """v1 遗忘闭环对 v2 分层桶关闭（REV 双轨隔离）。"""

    def test_scan_skips_v2_managed(self, actors):
        mid = hold_due(actors, "v2m")
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        assert all(p["target_memory_id"] != mid for p in scan["created"])

    def test_v1_submit_rejects_v2_target(self, actors):
        """v1 提案通道拒绝 v2 桶（即使 work_items 被人为塞入草稿）。"""
        mid = hold_due(actors, "v2t")
        now = datetime.now(timezone.utc).isoformat()
        with db.workspace() as wconn:
            wconn.execute(
                "INSERT INTO work_items(item_id, item_type, target_memory_id,"
                " state, current_revision, created_by, created_at, updated_at)"
                " VALUES('wi_fake','forget_proposal',?, 'draft', 1, 'worker',"
                ' ?, ?)', (mid, now, now))
            wconn.execute(
                "INSERT INTO proposal_versions(proposal_id, revision, payload,"
                " payload_hash, created_by)"
                " VALUES('wi_fake', 1, ?, 'h', 'worker')",
                ('{"compressed_summary": "s", "base_memory_version": 1}',))
        with pytest.raises(Forbidden) as ei:
            workspace.submit(actors["worker"], "wi_fake", 1)
        assert ei.value.code == "V2_MANAGED_TARGET"

    def test_v1_defer_requires_approver(self, actors):
        """worker 不能挂起 submitted 提案（defer 与 approve 同权限）。"""
        # v1 路径走一条 v1 桶（无 retention 行）
        out = memory.hold(actors["jiaming"], text="旧式桶",
                          memory_date="2026-01-01",
                          date_confidence="exact", raw_pending=False)
        mid = out["memory_id"]
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"]
                    if p["target_memory_id"] == mid)
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     "摘要。", "r")
        sub = workspace.submit(actors["worker"], prop["proposal_id"],
                               rev["revision"])
        with pytest.raises(Forbidden):
            workspace.decide(actors["worker"], sub["proposal_id"],
                             sub["revision"], sub["proposal_hash"],
                             sub["base_memory_version"], "defer")


class TestSearchDedupAndLabels:
    """V2-SEARCH-05：search 合并去重；matched_fields 如实标注。"""

    def test_related_of_dedup(self, actors):
        from mariposa.memory import relations as rel
        a = memory.hold(actors["jiaming"], text="烧烤探针事件甲",
                        memory_date="2026-09-01", date_confidence="exact",
                        raw_pending=False)["memory_id"]
        b = memory.hold(actors["jiaming"], text="无关正文乙",
                        memory_date="2026-09-02", date_confidence="exact",
                        raw_pending=False)["memory_id"]
        rel.link("jiaming", a, b, "related_to")
        with db.formal() as conn:
            out = rsearch.search(conn, "烧烤", related_of=b)
        ids = [h["memory_id"] for h in out["hits"]]
        assert ids.count(a) == 1  # 关键词+关联命中只出现一次

    def test_matched_fields_legacy_projection(self, actors):
        """v1 桶 why 层命中的词不冒充 event_text（D17 祖父条款如实标注）。"""
        memory.hold(actors["jiaming"], text="正文内容", why_remember="旧行为什么值得记",
                    memory_date="2026-09-01", date_confidence="exact",
                    raw_pending=False)
        with db.formal() as conn:
            out = rsearch.recall(conn, "值得记")
        assert out["hits"]
        assert out["hits"][0]["matched_fields"] == ["legacy_projection"]

    def test_occurred_range_persisted_and_filterable(self, actors):
        """SEARCH-03：occurred 区间落库（不再吞参）且参与日期筛选。"""
        out = memory.hold(actors["jiaming"], text="跨零点的事件",
                          memory_date="2026-09-10",
                          date_confidence="exact", raw_pending=False,
                          occurred_start="2026-09-09T22:00:00+08:00",
                          occurred_end="2026-09-10T01:30:00+08:00")
        mid = out["memory_id"]
        with db.formal() as conn:
            got = memory.get(conn, mid)
        assert got["occurred_start"].startswith("2026-09-09")
        # memory_date 不在窗口内，但 occurred 区间与 09-09 重叠 → 命中
        with db.formal() as conn:
            out2 = rsearch.recall(conn, filters={
                "event_date": {"from": "2026-09-09", "to": "2026-09-09"}})
        assert mid in {h["memory_id"] for h in out2["hits"]}


class TestQueuePagination:
    """V2-RET-09：due 队列 keyset 分页；generate 翻页越过阻塞项。"""

    def test_due_items_after_cursor(self, actors):
        today = ret_mod.business_today().isoformat()
        rows = [(f"due_{i:03d}", "2020-01-01") for i in range(55)]
        with db.formal() as conn:
            for rid, due in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO forgetting_due_queue(item_id,"
                    " target_kind, target_id, due_date, status, created_at,"
                    " updated_at) VALUES(?, 'memory', ?, ?, 'pending', ?, ?)",
                    (rid, rid, due, "2026-01-01T00:00:00+00:00",
                     "2026-01-01T00:00:00+00:00"))
        page1 = due_queue.due_items(today, limit=50)
        assert len(page1) >= 50
        after = (page1[-1]["due_date"], page1[-1]["target_id"])
        page2 = due_queue.due_items(today, limit=50, after=after)
        assert page2 and page2[0]["target_id"] > after[1]
        # 全量翻页无遗漏
        seen = {r["target_id"] for r in page1} | {r["target_id"] for r in page2}
        assert all(rid in seen for rid, _ in rows)

    def test_generate_pages_past_blockers(self, actors):
        """队头 55 条不可建项的到期行不挡第 56 条真实桶建项。"""
        with db.formal() as conn:
            for i in range(55):
                conn.execute(
                    "INSERT OR IGNORE INTO forgetting_due_queue(item_id,"
                    " target_kind, target_id, due_date, status, created_at,"
                    " updated_at) VALUES(?, 'memory', ?, '2020-01-01',"
                    " 'pending', ?, ?)",
                    (f"ghost_{i:03d}", f"ghost_{i:03d}",
                     "2026-01-01T00:00:00+00:00",
                     "2026-01-01T00:00:00+00:00"))
        real = hold_due(actors, "real-late")  # due 更晚 → 排在 ghost 之后
        out = review_mod.generate(actors["worker"], candidate_summary="稿")
        assert any(c["target_id"] == real for c in out["created"])


class TestPlansTerminalEnqueue:
    """V2-PLAN-02/03：直建终态计划入到期队列。"""

    def test_create_done_enqueues(self, actors):
        out = plans.create("qiaosheng", "直建完成计划", state="done")
        pid = out["plan_id"]
        with db.formal() as conn:
            row = conn.execute(
                "SELECT status, due_date FROM forgetting_due_queue WHERE"
                " target_kind='plan' AND target_id=?", (pid,)).fetchone()
        assert row and row["status"] == "pending" and row["due_date"]


class TestSchemaDepthValidation:
    """V2-OPS-02：嵌套 required / 数组 minItems 真实生效。"""

    def test_our_words_missing_text_rejected(self, actors):
        with pytest.raises(Forbidden):
            input_schemas.validate("memory.hold", {
                "text": "正文", "our_words": [{"speaker": "jiaming"}]})

    def test_empty_categories_rejected(self, actors):
        with pytest.raises(Forbidden):
            input_schemas.validate("memory.categories.replace", {
                "memory_id": "m", "categories": []})

    def test_revise_schema_rejects_unknown_change_field(self, actors):
        with pytest.raises(Forbidden):
            input_schemas.validate("workspace.review.revise", {
                "item_id": "r1", "changes": {"event_text": "x"}})

    def test_revise_requires_changes_object(self, actors):
        with pytest.raises(Forbidden):
            input_schemas.validate("workspace.review.revise", {
                "item_id": "r1"})


class TestBootstrapSnapshotComponents:
    """V2-BOOT-05/07：快照指纹含业务日期；跨日续页拒绝。"""

    def test_next_page_cross_day_stale(self, actors):
        from mariposa.bootstrap import service as boot
        got = boot.get("jiaming", "cc", "cc")
        snap_id = got["snapshot_id"]
        # 正常续页可用（同日、状态未变）
        page = boot.next_page("jiaming", "cc", snap_id,
                              {"plans_offset": 0}, section="plans")
        assert page["section"] == "plans"
        # 人为把快照业务日期拨到另一天 → 跨日续页必须 SNAPSHOT_STALE
        with db.formal() as conn:
            conn.execute(
                "UPDATE bootstrap_snapshots SET business_date=? WHERE"
                " snapshot_id=?", ("2000-01-01", snap_id))
        with pytest.raises(SnapshotStale):
            boot.next_page("jiaming", "cc", snap_id,
                           {"plans_offset": 0}, section="plans")


class TestIdempotencyBusinessFailure:
    """V2-OPS-05：干净业务拒绝不伪装成 OUTCOME_UNKNOWN。"""

    def test_handler_rejection_marks_failed_and_allows_retry(self, actors):
        # 第一次：周家明跨窗口补记当时心情 → handler 内 MOOD_WINDOW_REQUIRED
        # （占位后拒绝，事务回滚无副作用）→ 记录 failed 而非 running
        bad = {"text": "幂等重试正文", "memory_date": "2026-02-02",
               "date_confidence": "exact", "raw_pending": False,
               "creation_mode": "retrospective",
               "mood": {"text": "开心", "tags": ["开心"]}}
        with pytest.raises(Forbidden):
            registry.invoke(actors["jiaming"], "memory.hold", bad,
                            "audit-bizfail-1")
        with db.formal() as conn:
            row = conn.execute(
                "SELECT status FROM idempotency_records WHERE"
                " idempotency_key='audit-bizfail-1'").fetchone()
        assert row and row["status"] == "failed"
        # 同 key 修正后重试：立即执行，不再 OUTCOME_UNKNOWN
        args = {"text": "幂等重试正文", "memory_date": "2026-02-02",
                "date_confidence": "exact", "raw_pending": False}
        out = registry.invoke(actors["jiaming"], "memory.hold", args,
                              "audit-bizfail-1")
        assert out["ok"] and "memory_id" in out["data"]
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM idempotency_records WHERE"
                " idempotency_key='audit-bizfail-1'").fetchone()["c"]
            mems = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE memory_date="
                "'2026-02-02'").fetchone()["c"]
        assert n == 1 and mems == 1  # 恰一条记录、恰一次副作用


class TestReviewHardening:
    """审查杂项加固：revise 类型校验、委托动作边界。"""

    def test_revise_non_string_summary_rejected(self, actors):
        mid = hold_due(actors, "rv")
        g = review_mod.generate(actors["worker"], memory_id=mid,
                                candidate_summary="候选")
        item_id = g["created"][0]["item_id"]
        review_mod.claim(actors["linshijian"])
        with pytest.raises(Forbidden):
            review_mod.revise(actors["linshijian"], item_id,
                              {"summary_body": 123})

    def test_narrow_delegation_blocks_release(self, actors):
        """窄权限委托（只 revise）不能放行。"""
        with db.formal() as conn:
            conn.execute(
                "UPDATE review_delegations SET allowed_actions=? WHERE"
                " reviewed_principal='linshijian'",
                ('["revise_summary"]',))
        mid = hold_due(actors, "narrow")
        g = review_mod.generate(actors["worker"], memory_id=mid,
                                candidate_summary="候选")
        item_id = g["created"][0]["item_id"]
        review_mod.claim(actors["linshijian"])
        with pytest.raises(Forbidden) as ei:
            review_mod.submit(actors["linshijian"], item_id, "release", 1)
        assert ei.value.code == "DELEGATION_ACTION_NOT_GRANTED"
