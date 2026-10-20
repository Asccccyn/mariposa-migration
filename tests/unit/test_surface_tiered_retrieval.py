"""P2 分级检索 + 确定性免 JEV（她 2026-10-20 二批复，八条硬约束场景逐条具名）。

真实 SQLite（reset_all 隔离根）+ 真实 hold 链 + registry 面 prepare/validate；
JEV 用假 transport（默认答 0.5=中间带，.98 下语义路径永不确认——与生产
默认一致）；Dense 用假 provider/spy，**绝不加载任何模型**。
"""
from __future__ import annotations

import json

import pytest

from mariposa import config, db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import judge_policy
from mariposa.retrieval import candidate_service, qwen_embed, semantic
from mariposa.surfacing import contract as C
from mariposa.surfacing import judge as surface_judge
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    surface_judge.clear_transport_override()
    qwen_embed._state.update({"model": None, "tokenizer": None, "model_root": None})
    semantic._provider = None
    return {
        "qiaosheng": identity.Principal("qiaosheng", "宿主用户", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "char", "agent", "claude_chat", "bj"),
    }


def _rev() -> int:
    with db.formal() as conn:
        row = conn.execute("SELECT revision FROM recall_judge_policy WHERE id=1").fetchone()
        return (row["revision"] if row else 0) + 1


def _policy_on(monkeypatch):
    monkeypatch.setattr(config, "SURFACE_ENABLED", True)
    monkeypatch.setattr(config, "SURFACE_DAILY_JUDGE_COST_CAP_USD", 5.0)
    judge_policy.update_policy("qiaosheng", expected_revision=_rev(), enabled=True,
                               provider="typesafe_jev", model_id="fake-model")
    with db.formal() as conn:
        conn.execute("UPDATE recall_judge_policy SET allowed_data=? WHERE id=1",
                     (json.dumps({"event_excerpt": True, "structured_metadata": True,
                                  "current_dialogue": True}),))


def _hold(actors, text, *, date="2026-07-09", conf="exact", title="标题",
          mode="contemporaneous", **kw):
    return memory.hold(actors["jiaming"], text=text, memory_date=date,
                       date_confidence=conf, original_title=title,
                       categories=["sweet"], creation_mode=mode,
                       raw_pending=False, **kw)


class Transport:
    def __init__(self, value=0.5):
        self.value, self.calls, self.keys = value, 0, []

    def __call__(self, payload):
        self.calls += 1
        self.keys.extend(payload["questions"])
        return {"answers": {k: {"noul": self.value} for k in payload["questions"]},
                "model": "jev-fake"}


def _prepare(actors, text, op="op-1", occurred_at=None):
    args = {
        "operation_id": op, "conversation_scope": "room-1",
        "source_input": {"message_id": "m1", "revision": 1, "sequence": 10,
                         "text": text, "body_hash": "a" * 64,
                         "occurred_at": occurred_at},
        "recent_dialogue": [],
        "event_hypotheses": [{"hypothesis_id": "h1", "start_cp": 0,
                              "end_cp": len(text), "inherited_date_span": None}],
    }
    out = registry.invoke(actors["jiaming"], "memory.surfacing.prepare", args, None)
    return out["data"]


def _det(data):
    return data["coverage"]["deterministic"]["ev_1"]


# ------------------------------------------------------------ 她的八条

def test_01_zai_yiqi_vs_yiqi_qu_haibian_not_confirmed(actors, monkeypatch):
    """同一天"在一起" vs "一起去海边"：不得仅凭 2-gram "一起" 确认。"""
    _policy_on(monkeypatch)
    _hold(actors, "7月9日，我们一起去了海边", title="去海边")
    t = Transport(0.5)
    surface_judge.set_transport_for_tests(t)
    data = _prepare(actors, "7月9日我们在一起了")
    det = _det(data)
    assert det["outcome"] == C.UNKNOWN
    assert det["reason"] == C.DET_NO_STRONG_IDENTITY
    assert data["status"] != "prepared" and data["selected_count"] == 0
    # 候选相关（召回命中）但无身份 → 交语义路径：JEV 问了、.98 下不确认
    assert t.calls == 1 and data["coverage"]["judge_requests_prepare"] == 1


def test_02_single_candidate_without_strong_identity_is_unknown(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, "7月9日那天发生了一些事", title="七月记录")
    surface_judge.set_transport_for_tests(Transport(0.5))
    data = _prepare(actors, "7月9日我们在一起了")
    det = _det(data)
    assert det["outcome"] == C.UNKNOWN
    assert det["reason"] in (C.DET_NO_STRONG_IDENTITY, C.DET_NO_CANDIDATE)
    assert det["coverage"]["date_set_size"] == 1
    assert data["selected_count"] == 0


def test_03_date_set_cap_or_truncation_never_claims_unique(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, "7月9日我们把记忆库建成了", title="记忆库建成")
    _hold(actors, "7月9日另一件事", title="另一件事")
    surface_judge.set_transport_for_tests(Transport(0.5))
    # 日期集规模超上限
    monkeypatch.setattr(config, "SURFACE_DETERMINISTIC_MAX_DATE_SET", 1)
    det = _det(_prepare(actors, "7.9记忆库建成了", op="cap"))
    assert (det["outcome"], det["reason"]) == (C.UNKNOWN, C.DET_DATE_SET_TOO_LARGE)
    # 池被安全阀截断（RECALL_POOL_MAX_BUCKETS=1）→ 日期集 truncated → 同样不声明唯一
    monkeypatch.setattr(config, "SURFACE_DETERMINISTIC_MAX_DATE_SET", 200)
    monkeypatch.setattr(config, "RECALL_POOL_MAX_BUCKETS", 1)
    det = _det(_prepare(actors, "7.9记忆库建成了", op="trunc"))
    assert det["outcome"] == C.UNKNOWN
    assert det["reason"] == C.DET_DATE_SET_TOO_LARGE
    assert det["coverage"]["date_set_truncated"] is True


def test_03b_lexical_pool_truncated_is_unknown(actors, monkeypatch):
    """日期集完整（1 条）但全作用域词法池被截断 → LEXICAL_TRUNCATED。"""
    _policy_on(monkeypatch)
    _hold(actors, "7月9日我们把记忆库建成了", title="记忆库建成")
    _hold(actors, "8月1日别的事", date="2026-08-01", title="别的")
    surface_judge.set_transport_for_tests(Transport(0.5))
    monkeypatch.setattr(config, "RECALL_POOL_MAX_BUCKETS", 1)
    det = _det(_prepare(actors, "2026年7月9日记忆库建成了"))
    assert det["coverage"]["date_set_truncated"] is False
    assert (det["outcome"], det["reason"]) == (C.UNKNOWN, C.DET_LEXICAL_TRUNCATED)


def test_04_yearless_date_cross_year_competition_not_unique(actors, monkeypatch):
    """无年份日期：不补年；两年都有同月日同身份候选 → NOT_UNIQUE。"""
    _policy_on(monkeypatch)
    _hold(actors, "2025年7月9日我们把记忆库建成了", date="2025-07-09", title="记忆库建成")
    _hold(actors, "2026年7月9日我们把记忆库建成了", date="2026-07-09", title="记忆库建成")
    surface_judge.set_transport_for_tests(Transport(0.5))
    det = _det(_prepare(actors, "7.9记忆库建成了", occurred_at="2026-10-20T10:00:00+08:00"))
    assert (det["outcome"], det["reason"]) == (C.UNKNOWN, C.DET_NOT_UNIQUE)
    assert det["competitors_checked"] == 1
    assert det["coverage"]["date_set_size"] == 2
    assert det["date_constraint"] == [{"year": None, "month": 7, "day": 9}]
    # 给了年份即可唯一
    det2 = _det(_prepare(actors, "2026年7月9日记忆库建成了", op="y"))
    assert det2["outcome"] == C.CONFIRMED_DETERMINISTIC
    assert det2["candidate_ref"].startswith("memory:")


class _FakeTextEmbedding:  # 类名即 dense_ready 对 bge 的只读判据
    pass


_FakeTextEmbedding.__name__ = "TextEmbedding"


def test_05_insufficient_bm25_with_dense_ready_runs_dense_within_budget(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, "7月9日，我们一起去了海边", title="去海边")
    surface_judge.set_transport_for_tests(Transport(0.5))
    monkeypatch.setattr(config, "SEMANTIC_PROVIDER", "local_bge_zh")
    monkeypatch.setattr(semantic, "_provider", _FakeTextEmbedding())
    assert semantic.dense_ready() is True
    calls = []

    def spy(conn, plan, terms, phrases, where, params, rejected, cov, degraded,
            reindex_budget=None):
        calls.append({"query": plan.get("semantic_query"), "budget": reindex_budget})
        cov["dense_event"] = "complete_within_scope"
        return []
    monkeypatch.setattr(candidate_service, "dense_tail", spy)
    data = _prepare(actors, "7月9日我们在一起了")
    assert _det(data)["outcome"] == C.UNKNOWN
    assert len(calls) == 1 and calls[0]["budget"] == 0
    assert data["coverage"]["tiers"]["ev_1"]["dense"]["status"] == "complete_within_scope"


def test_06_dense_not_ready_or_vectors_stale_no_cold_load(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, "7月9日，我们一起去了海边", title="去海边")
    surface_judge.set_transport_for_tests(Transport(0.5))
    monkeypatch.setattr(config, "SEMANTIC_PROVIDER", "qwen3e4b")

    def boom():
        raise AssertionError("请求路径触发了 Qwen 冷加载")
    monkeypatch.setattr(qwen_embed, "_load", boom)
    monkeypatch.setattr(semantic, "semantic_search",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调 dense")))
    data = _prepare(actors, "7月9日我们在一起了")
    tiers = data["coverage"]["tiers"]["ev_1"]
    assert tiers["dense"]["status"] == "skipped_cold"
    assert "dense_skipped_cold" in data["coverage"]["degraded"]
    # 向量失效：reindex_budget=0 → pending 回报 → 覆盖不完整，不把空结果当"库里没有"
    qwen_embed._state.update({"model": object(), "tokenizer": object(),
                              "model_root": qwen_embed.model_root()})
    assert qwen_embed.is_resident() is True

    def pending_dense(conn, plan, terms, phrases, where, params, rejected, cov, degraded,
                      reindex_budget=None):
        assert reindex_budget == 0
        cov["dense_event"] = "partial_vectors_pending"
        cov["dense_pending_vectors"] = 3
        return []
    monkeypatch.setattr(candidate_service, "dense_tail", pending_dense)
    data = _prepare(actors, "7月9日我们在一起了", op="pend")
    d = data["coverage"]["tiers"]["ev_1"]["dense"]
    assert (d["status"], d["pending"]) == ("partial_vectors_pending", 3)
    assert "dense_partial_vectors_pending" in data["coverage"]["degraded"]
    # 身份不一致（加载目录≠配置）→ 不算常驻
    qwen_embed._state["model_root"] = "/elsewhere"
    assert qwen_embed.is_resident() is False


def test_07_deterministic_prepare_zero_jev_delivery_accounted_separately(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, "7月9日我们把记忆库建成了，那天很开心", title="记忆库建成")
    t = Transport(0.99)
    surface_judge.set_transport_for_tests(t)
    data = _prepare(actors, "还记得7.9记忆库建成的事吗")
    det = _det(data)
    assert det["outcome"] == C.CONFIRMED_DETERMINISTIC
    assert det["strong_match"] == {"kind": "title", "text": "记忆库建成", "field": "original_title"}
    assert det["rules"] == {"deterministic": "deterministic-v1", "anchors": "anchors-v1",
                            "facts": "facts-v2"}
    assert det["evidence_ref"].startswith("memory:")
    assert data["status"] == "prepared" and data["selected_count"] == 1
    assert t.calls == 0 and data["coverage"]["judge_requests_prepare"] == 0
    assert data["coverage"]["tiers"]["ev_1"]["dense"]["status"] == "not_requested"
    # 交付仍按 P0 合同调 JEV（same_occurrence + relevant_now）——单独记账
    vargs = {"operation_id": "val-1", "conversation_scope": "room-1",
             "candidate_snapshot_ref": data["candidate_snapshot_ref"],
             "source_input_ref": {"message_id": "m1", "revision": 1},
             "target_input": {"message_id": "m2", "revision": 1, "sequence": 11,
                              "text": "那天怎么样", "body_hash": "b" * 64, "occurred_at": None},
             "recent_dialogue": [], "window_id": "w1",
             "deadline_at": "2999-01-01T00:00:00+00:00"}
    vout = registry.invoke(actors["jiaming"], "memory.surfacing.validate", vargs, None)["data"]
    assert t.calls == 1, "delivery 恰一批请求（prepare 0 / delivery 1）"
    assert any(k.endswith("_relevant_now") for k in t.keys)
    assert vout["status"] == "ready"
    with db.recall_runtime() as conn:
        rows = conn.execute(
            "SELECT phase, SUM(request_count) n FROM surface_judge_ledger"
            " GROUP BY phase").fetchall()
    by_phase = {r["phase"]: r["n"] for r in rows}
    assert by_phase.get("prepare", 0) == 0 and by_phase.get("delivery", 0) == 1


def test_08_date_conflict_rejects_and_never_reaches_jev(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, "8月19日我们把记忆库建成了", date="2026-08-19", title="记忆库建成")
    t = Transport(0.99)
    surface_judge.set_transport_for_tests(t)
    data = _prepare(actors, "7月9日记忆库建成了")
    det = _det(data)
    assert det["outcome"] == C.UNKNOWN and det["non_competitors"] == 1
    assert det["reason"] == C.DET_NO_CANDIDATE
    assert t.calls == 0, "日期冲突候选不送 JEV"
    assert data["selected_count"] == 0
    assert data["coverage"]["judge_requests_prepare"] == 0


# ------------------------------------------------------------ 补充

def test_time_unknown_with_strong_identity_blocks_deterministic(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, "7月9日我们把记忆库建成了", title="记忆库建成")
    # 真正无日期（补记模式不回填）：time unknown ∧ 强身份 → 竞争者
    _hold(actors, "我们把记忆库建成了，具体哪天忘了", date=None, conf="unknown",
          title="记忆库建成", mode="retrospective")
    surface_judge.set_transport_for_tests(Transport(0.5))
    det = _det(_prepare(actors, "2026年7月9日记忆库建成了"))
    assert (det["outcome"], det["reason"]) == (C.UNKNOWN, C.DET_NOT_UNIQUE)
    assert det["competitors_checked"] == 1


def test_unreliable_filled_date_with_strong_identity_still_competes(actors, monkeypatch):
    """同期 hold 不给日期会回填写入日（date_confidence 由调用方给，这里 unknown）：
    回填日与约束"冲突"，但不可靠日期**不能**用来排除强身份候选 → 竞争者。"""
    _policy_on(monkeypatch)
    _hold(actors, "7月9日我们把记忆库建成了", title="记忆库建成")
    b = _hold(actors, "我们把记忆库建成了", date=None, conf="unknown", title="记忆库建成")
    with db.formal() as conn:
        filled = conn.execute("SELECT memory_date FROM memories WHERE memory_id=?",
                              (b["memory_id"],)).fetchone()["memory_date"]
    assert filled and filled != "2026-07-09"       # 已回填为写入日
    surface_judge.set_transport_for_tests(Transport(0.5))
    det = _det(_prepare(actors, "2026年7月9日记忆库建成了"))
    assert (det["outcome"], det["reason"]) == (C.UNKNOWN, C.DET_NOT_UNIQUE)
    assert det["competitors_checked"] == 1


def test_noise_does_not_block_and_is_counted(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, "7月9日我们把记忆库建成了", title="记忆库建成")
    _hold(actors, "7月9日下午下了一场雨", title="下雨")        # 同日无锚点关系=噪声
    _hold(actors, "8月19日记忆库建成了", date="2026-08-19", title="记忆库建成")  # 日期冲突=噪声
    surface_judge.set_transport_for_tests(Transport(0.5))
    det = _det(_prepare(actors, "2026年7月9日记忆库建成了"))
    assert det["outcome"] == C.CONFIRMED_DETERMINISTIC
    assert det["competitors_checked"] == 0 and det["non_competitors"] == 2


def test_inferred_date_cannot_confirm(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, "7月9日我们把记忆库建成了", conf="inferred", title="记忆库建成")
    surface_judge.set_transport_for_tests(Transport(0.5))
    det = _det(_prepare(actors, "2026年7月9日记忆库建成了"))
    assert (det["outcome"], det["reason"]) == (C.UNKNOWN, C.DET_DATE_NOT_EXACT)


def test_strong_identity_kinds_core_phrase_and_binding_ref(actors, monkeypatch):
    _policy_on(monkeypatch)
    m = _hold(actors, "我们一起去海边看日落，很美", title="日落之约")   # 标题不在原话 → 走 (B)
    surface_judge.set_transport_for_tests(Transport(0.5))
    det = _det(_prepare(actors, "7月9日还记得我们一起去海边看日落吗"))
    assert det["outcome"] == C.CONFIRMED_DETERMINISTIC
    assert det["strong_match"] == {"kind": "core_phrase", "text": "一起去海边看日落",
                                   "field": "event_text"}
    det2 = _det(_prepare(actors, f"7月9日 memory:{m['memory_id']} 那条", op="ref"))
    assert det2["outcome"] == C.CONFIRMED_DETERMINISTIC
    assert det2["strong_match"]["kind"] == "binding_ref"


def test_no_date_recall_never_deterministic_goes_semantic(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, "我们一起去海边看日落", title="海边")
    t = Transport(0.5)
    surface_judge.set_transport_for_tests(t)
    data = _prepare(actors, "还记得我们一起去海边看日落那次吗")
    assert _det(data)["reason"] == C.DET_NO_DATE_CONSTRAINT
    assert t.calls == 1 and data["selected_count"] == 0     # .98 下语义路径不确认


def test_admit_deterministic_is_the_only_producer():
    assert C.admit(C.SEM_CONFIRMED, {"time": {"status": "pass", "required": True},
                                     "participants": {"status": "not_required", "required": False},
                                     "evidence": {"status": "pass"}}).outcome == C.CONFIRMED_SEMANTIC
    policy = {"deterministic_max_date_set": 200, "deterministic_rules_version": "deterministic-v1",
              "anchors_rules_version": "anchors-v1", "facts_rules_version": "facts-v2"}
    cand = {"resource_ref": "memory:a", "content_version": "1", "date_confidence": "exact",
            "recall_hit": True, "strong_match": {"kind": "title", "text": "x", "field": "original_title"},
            "evidence_ref": "memory:a",
            "facts": {"time": {"status": "pass", "required": True},
                      "participants": {"status": "not_required", "required": False},
                      "evidence": {"status": "pass"}}}
    adm = C.admit_deterministic([cand], {"dates": [{"year": 2026, "month": 7, "day": 9}]},
                                {"date_set_size": 1, "date_set_truncated": False,
                                 "lexical_coverage": "complete_within_scope"}, policy)
    assert adm.outcome == C.CONFIRMED_DETERMINISTIC
    assert adm.as_dict()["rules"]["deterministic"] == "deterministic-v1"


def test_date_set_query_uses_date_index_on_large_db(actors):
    """EXPLAIN QUERY PLAN（2 万行合成）：memory_date 臂显式 INDEXED BY 走
    idx_memories_date（无统计信息时规划器会偏向 visibility 索引后全扫）；
    occurred 臂无索引可用——如实记录为 occurred_start 非空行扫描。"""
    import datetime as _dt
    from mariposa.surfacing import candidates as cand
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with db.formal() as conn:
        rows = [(f"bench{i:06d}", 1, f"20{10 + (i % 17):02d}-{1 + (i % 12):02d}-{1 + (i % 28):02d}",
                 "exact", "active", "full", now, now) for i in range(20000)]
        conn.executemany(
            "INSERT INTO memories(memory_id, current_version_no, memory_date, date_confidence,"
            " visibility, compression_state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)", rows)
        plan = " ".join(r["detail"] for r in conn.execute(
            "EXPLAIN QUERY PLAN " + cand.DATE_ARM_SQL,
            ("2026-07-09", "2026-07-09", "", 500)).fetchall())
        assert "idx_memories_date" in plan, plan
        assert "SCAN" not in plan.split("USING INDEX idx_memories_date")[0], plan
        plan2 = " ".join(r["detail"] for r in conn.execute(
            "EXPLAIN QUERY PLAN " + cand.OCCURRED_ARM_SQL,
            ("2026-07-09", "2026-07-09", "", 500)).fetchall())
        assert "idx_memories_date" not in plan2      # 如实：occurred 臂无日期索引可用
        # 真实枚举结果与朴素查询一致（20000 行里 7/9 的桶）
        ids, truncated = cand._keyset_ids(conn, cand.DATE_ARM_SQL, ["2026-07-09", "2026-07-09"])
        naive = {r["memory_id"] for r in conn.execute(
            "SELECT memory_id FROM memories WHERE visibility='active' AND memory_date='2026-07-09'")}
        assert set(ids) == naive and not truncated
