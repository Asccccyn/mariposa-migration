"""P2 覆盖率报告（她 1020 二批复：要的是两组数——多少条可安全免 JEV，
多少条因证据不足**正确地**弃权；不强制 S01/S03/S13 走确定性）。

A 组：17 个历史语义场景（审计评估脚本 SCENARIOS 的合成池）播进真实库、
跑真实 prepare。合成池标题全是单字 "t"、正文用"建成"而原话用"做成"——
严格确定性条件下预期大多数为 UNKNOWN，这是正确结果，不放宽。
B 组：本批构造的 5 条"原话含标题/≥4 字核心段"的正例，展示确定性路径在
什么样的表述下才会放行（作者构造，非真人标注）。
全部离线：JEV 假 transport（0.5=中间带，.98 下语义路径不确认）、无 Dense。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from mariposa import config, db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import judge_policy
from mariposa.surfacing import contract as C
from mariposa.surfacing import judge as surface_judge
from tests.conftest import reset_all

EVAL_SCRIPT = Path("/Users/owner/Projects/devspace-workspace/"
                   "audit-verify-20261019/mariposa/scripts/jev_real_eval_20261019.py")
pytestmark = pytest.mark.skipif(not EVAL_SCRIPT.exists(), reason="历史评估脚本不在本机")


def _scenarios():
    spec = importlib.util.spec_from_file_location("_jev_eval_hist2", EVAL_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return [s for s in mod.SCENARIOS if s.get("phase") != "delivery"]


def _actors():
    reset_all()
    surface_judge.clear_transport_override()
    return {"jiaming": identity.Principal("jiaming", "char", "agent", "claude_chat", "bj")}


def _policy_on(monkeypatch):
    monkeypatch.setattr(config, "SURFACE_ENABLED", True)
    monkeypatch.setattr(config, "SURFACE_DAILY_JUDGE_COST_CAP_USD", 5.0)
    with db.formal() as conn:
        row = conn.execute("SELECT revision FROM recall_judge_policy WHERE id=1").fetchone()
        rev = (row["revision"] if row else 0) + 1
    judge_policy.update_policy("qiaosheng", expected_revision=rev, enabled=True,
                               provider="typesafe_jev", model_id="fake-model")
    with db.formal() as conn:
        conn.execute("UPDATE recall_judge_policy SET allowed_data=? WHERE id=1",
                     (json.dumps({"event_excerpt": True, "structured_metadata": True,
                                  "current_dialogue": True}),))


class Transport:
    def __init__(self, value=0.5):
        self.value, self.calls = value, 0

    def __call__(self, payload):
        self.calls += 1
        return {"answers": {k: {"noul": self.value} for k in payload["questions"]},
                "model": "jev-fake"}


def _seed(actors, cands: list[dict]):
    """合成池 → 真实 hold（date None 用补记模式，避免同期回填写入日）。"""
    for c in cands:
        memory.hold(actors["jiaming"], text=c["event_text"], memory_date=c.get("memory_date"),
                    date_confidence=c.get("date_confidence") or "unknown",
                    original_title=c.get("original_title") or "t", categories=["sweet"],
                    creation_mode="contemporaneous" if c.get("memory_date") else "retrospective",
                    raw_pending=False)


def _prepare(actors, text, hyps, op):
    args = {"operation_id": op, "conversation_scope": "room-1",
            "source_input": {"message_id": "m1", "revision": 1, "sequence": 10, "text": text,
                             "body_hash": "a" * 64, "occurred_at": "2026-10-20T10:00:00+08:00"},
            "recent_dialogue": [],
            "event_hypotheses": [{"hypothesis_id": h["hypothesis_id"], "start_cp": h["start_cp"],
                                  "end_cp": min(h["end_cp"], len(text)),
                                  "inherited_date_span": None} for h in hyps]}
    return registry.invoke(actors["jiaming"], "memory.surfacing.prepare", args, None)["data"]


def test_group_a_historical_scenarios_two_numbers(monkeypatch):
    rows = []
    for sc in _scenarios():
        actors = _actors()
        _policy_on(monkeypatch)
        _seed(actors, sc["pool"])
        t = Transport(0.5)
        surface_judge.set_transport_for_tests(t)
        data = _prepare(actors, sc["source"], sc["hyps"], f"cov-{sc['id']}")
        dets = data["coverage"]["deterministic"]
        rows.append({
            "id": sc["id"], "status": data["status"], "selected": data["selected_count"],
            "deterministic": {ev: (d["outcome"], d["reason"]) for ev, d in dets.items()},
            "jev_prepare": data["coverage"]["judge_requests_prepare"],
            "expect": sc["expect"],
        })
    free = [r for r in rows if any(o == C.CONFIRMED_DETERMINISTIC for o, _ in r["deterministic"].values())]
    abstain = [r for r in rows if r not in free
               and all(o == C.UNKNOWN for o, _ in r["deterministic"].values())]
    zero_jev_rejects = [r["id"] for r in rows if r["jev_prepare"] == 0
                        and any(x == "not_confirmed" for x in r["expect"].values())
                        and not any(o == C.CONFIRMED_DETERMINISTIC for o, _ in r["deterministic"].values())]
    print("\n[P2 coverage A/historical] safe_free_of_jev=%d correct_abstain=%d total=%d"
          % (len(free), len(abstain), len(rows)))
    for r in rows:
        print("  %-32s det=%s jev_prepare=%d status=%s" % (
            r["id"], {ev: f"{o}:{reason}" for ev, (o, reason) in r["deterministic"].items()},
            r["jev_prepare"], r["status"]))
    # 零误确认：期望 not_confirmed 的候选所在场景不得出现确定性确认
    for r in rows:
        if any(o == C.CONFIRMED_DETERMINISTIC for o, _ in r["deterministic"].values()):
            assert "confirmed" in r["expect"].values(), r
    # 日期冲突场景（S05/S12）：facts 先行，prepare 零 JEV
    assert {"S05-date-conflict", "S12-same-topic-different-day"} <= set(zero_jev_rejects)
    # 严格条件下合成池（标题 "t"、做成≠建成）预期全部 UNKNOWN——不放宽
    assert len(free) + len(abstain) == len(rows)
    # .98 下语义路径亦不确认：零赢家
    assert all(r["selected"] == 0 for r in rows)


AUTHORED_POSITIVES = [
    # (原话, 候选正文, 标题, 期望强身份种类)
    ("还记得2026年7月9日我们的记忆库建成那天吗", "7月9日我们把记忆库建成了，很开心", "记忆库建成", "title"),
    ("7.9也是我们的结婚纪念日吧", "2026年7月9日是我们的结婚纪念日", "结婚纪念日", "title"),
    ("7月9日还记得我们一起去海边看日落吗", "那天我们一起去海边看日落，很美", "日落之约", "core_phrase"),
    ("2026年7月9日那次深夜长谈你还记得吗", "7月9日我们进行了一次深夜长谈", "七月之夜", "core_phrase"),
    ("7月9日 memory:00001 那条你记得吗", "7月9日的一条记录", "记录", "binding_ref"),
]


def test_group_b_authored_positives_and_delivery_accounting(monkeypatch):
    results = []
    for text, body, title, kind in AUTHORED_POSITIVES:
        actors = _actors()
        _policy_on(monkeypatch)
        memory.hold(actors["jiaming"], text=body, memory_date="2026-07-09", date_confidence="exact",
                    original_title=title, categories=["sweet"], creation_mode="contemporaneous",
                    raw_pending=False)
        t = Transport(0.99)
        surface_judge.set_transport_for_tests(t)
        data = _prepare(actors, text, [{"hypothesis_id": "h1", "start_cp": 0, "end_cp": len(text)}],
                        "cov-b")
        det = data["coverage"]["deterministic"]["ev_1"]
        results.append((text, det["outcome"], det["reason"], (det.get("strong_match") or {}).get("kind"),
                        t.calls))
        assert det["outcome"] == C.CONFIRMED_DETERMINISTIC, (text, det)
        assert det["strong_match"]["kind"] == kind
        assert t.calls == 0 and data["coverage"]["judge_requests_prepare"] == 0
        # 交付分账：delivery 按现行 same-turn 合同（aaf4cfe/她 1024 裁定：浮现绑定
        # 同一条消息同一修订、当轮使用——target==source，迟到结果不挪到下一条）
        # 出站一批 JEV（same_occurrence + relevant_now）。
        # 旧测试按 P0 合同传 m1→m2（下一条消息）必然 SURFACE_TARGET_MISMATCH 零派发。
        vargs = {"operation_id": "cov-val", "conversation_scope": "room-1",
                 "candidate_snapshot_ref": data["candidate_snapshot_ref"],
                 "source_input_ref": {"message_id": "m1", "revision": 1},
                 "target_input": {"message_id": "m1", "revision": 1, "sequence": 10,
                                  "text": text, "body_hash": "a" * 64,
                                  "occurred_at": "2026-10-20T10:00:00+08:00"},
                 "recent_dialogue": [], "window_id": "w1",
                 "deadline_at": "2999-01-01T00:00:00+00:00"}
        vres = registry.invoke(actors["jiaming"], "memory.surfacing.validate", vargs, None)
        assert t.calls == 1, (text, vres)
    print("\n[P2 coverage B/authored] deterministic=%d/%d (prepare JEV 0 each; delivery JEV 1 each)"
          % (len(results), len(AUTHORED_POSITIVES)))
    for r in results:
        print("  %s -> %s (%s) prepare_jev=%d" % (r[0], r[1], r[3], r[4]))
