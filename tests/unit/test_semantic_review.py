"""quotes 语义校对受控管线：无 provider 挂起；双步判定；狭窄修正；撤下不复活。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.quotes import semantic_review, service as quotes
from tests.conftest import reset_all


class FakeClassifier:
    def __init__(self, label1, label2=None):
        self.label1, self.label2 = label1, label2 or label1
        self.calls = 0

    def classify(self, a, b):
        self.calls += 1
        label = self.label1 if self.calls % 2 == 1 else self.label2
        return {"label": label, "confidence": 0.9, "reason": "fake"}


@pytest.fixture()
def actors():
    reset_all()
    semantic_review.set_classifier_for_testing(None)
    yield {
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
    }
    semantic_review.set_classifier_for_testing(None)


def _versions(quote_id):
    with db.formal() as conn:
        return conn.execute(
            "SELECT version_no, text, semantic_status FROM quote_versions"
            " WHERE quote_id=? ORDER BY version_no", (quote_id,)).fetchall()


class TestPipeline:
    def test_no_provider_suspends_without_write(self, actors):
        q = quotes.keep("jiaming", "她说：下个月去体检。")
        out = semantic_review.run_review(q["quote_id"], raw_text="原文")
        assert out["status"] == "suspended"
        assert out["reason"] == "semantic_provider_unavailable"
        assert len(_versions(q["quote_id"])) == 1  # 无任何写动作

    def test_withdrawn_never_revived(self, actors):
        q = quotes.keep("jiaming", "她说：一段会被撤下的话。")
        quotes.withdraw("qiaosheng", q["quote_id"])
        semantic_review.set_classifier_for_testing(
            FakeClassifier("material_conflict"))
        with pytest.raises(Forbidden):
            semantic_review.run_review(q["quote_id"], raw_text="任何原文")

    def test_no_source_does_not_modify(self, actors):
        q = quotes.keep("jiaming", "她说：没有原文对照。")
        semantic_review.set_classifier_for_testing(FakeClassifier("no_source"))
        out = semantic_review.run_review(q["quote_id"])  # 无 raw_text
        assert out["status"] == "no_source"
        assert len(_versions(q["quote_id"])) == 1

    def test_equivalent_two_steps_no_change(self, actors):
        q = quotes.keep("jiaming", "她说：今晚想喝热可可。")
        semantic_review.set_classifier_for_testing(FakeClassifier("equivalent"))
        out = semantic_review.run_review(q["quote_id"], raw_text="今晚想喝热可可呀")
        assert out["status"] == "equivalent"
        assert len(_versions(q["quote_id"])) == 1

    def test_material_conflict_applied(self, actors):
        q = quotes.keep("jiaming", "她说：下周一开始晨跑。")  # 复述把暂缓写成承诺
        semantic_review.set_classifier_for_testing(
            FakeClassifier("material_conflict"))
        out = semantic_review.run_review(q["quote_id"], raw_text="晨跑先缓一缓，等入冬再说")
        assert out["status"] == "material_conflict_applied"
        assert out["new_version"] == 2
        vs = _versions(q["quote_id"])
        assert vs[0]["text"] == "她说：下周一开始晨跑。"  # 原复述版本保留
        assert vs[1]["text"] == "晨跑先缓一缓，等入冬再说"
        assert vs[1]["semantic_status"] == "material_conflict"
        with db.formal() as conn:
            audited = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE event_type="
                "'quote.semantic_corrected'").fetchone()["c"]
        assert audited == 1

    def test_two_steps_disagree_goes_uncertain(self, actors):
        q = quotes.keep("jiaming", "她说：年底前读完这本书。")
        # 一步判 equivalent、一步判 material_conflict -> 不稳定 -> uncertain 挂起
        semantic_review.set_classifier_for_testing(
            FakeClassifier("equivalent", "material_conflict"))
        out = semantic_review.run_review(q["quote_id"], raw_text="这本书读不完了再说")
        assert out["status"] == "uncertain"
        assert len(_versions(q["quote_id"])) == 1  # 未修改
        items = semantic_review.reviews_list(states=["deferred"])
        assert any(i["quote_id"] == q["quote_id"] for i in items)

    def test_auto_apply_off_goes_manual(self, actors, monkeypatch):
        from mariposa import config
        monkeypatch.setattr(config, "QUOTE_SEMANTIC_AUTO_APPLY", False)
        q = quotes.keep("jiaming", "她说：明天交稿。")
        semantic_review.set_classifier_for_testing(
            FakeClassifier("material_conflict"))
        out = semantic_review.run_review(q["quote_id"], raw_text="交稿日期延后了")
        assert out["status"] == "pending_manual_apply"
        assert len(_versions(q["quote_id"])) == 1  # 不自动改
        items = semantic_review.reviews_list(states=["submitted"])
        assert any(i["quote_id"] == q["quote_id"] for i in items)
