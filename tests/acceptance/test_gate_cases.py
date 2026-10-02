"""118 条验收用例映射测试（第一批：ID/WS/FOR/RET/RAW/QUOTE）。

每条测试 docstring 标注对应 `02_验收用例.md` 的用例 ID。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, NotFound, SnapshotStale
from mariposa.identity import service as identity
from mariposa.memory import listing, reengagement, relations, service as memory
from mariposa.retrieval import search as retrieval
from tests.conftest import TOKENS, reset_all


def _qid_of(memory_id):
    with db.formal() as conn:
        row = conn.execute(
            "SELECT q.id FROM quotes q JOIN quote_versions v ON"
            " v.quote_id=q.id WHERE v.raw_ref LIKE ?",
            (f"%{memory_id}%",)).fetchone()
    return row["id"]


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "jiaming_cc": identity.Principal("jiaming", "周家明", "agent", "cc", "bjcc"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }


def _hold(actors, text="验收桶", date="2026-06-01"):
    return memory.hold(actors["jiaming"], text=text, memory_date=date, categories=["daily"])


class TestID:
    def test_T_ID_01_same_principal_two_entries(self, actors):
        """T-ID-01：两入口同主体——作者均 jiaming，来源只在审计。"""
        a = memory.hold(actors["jiaming"], text="来自 Chat 的记忆",
                        memory_date="2026-06-01", entry_source="claude_chat", categories=["daily"])
        b = memory.hold(actors["jiaming_cc"], text="来自 CC 的记忆",
                        memory_date="2026-06-01", entry_source="cc", categories=["daily"])
        for h in (a, b):
            with db.formal() as conn:
                v = conn.execute(
                    "SELECT authored_by FROM memory_versions WHERE memory_id=?"
                    " AND version_no=1", (h["memory_id"],)).fetchone()
            assert v["authored_by"] == "jiaming"
        with db.formal() as conn:
            payloads = " ".join(str(r[0]) for r in conn.execute(
                "SELECT payload FROM audit_events WHERE event_type="
                "'memory.created'").fetchall())
        assert "claude_chat" in payloads and "cc" in payloads

    def test_T_ID_02_self_reported_actor_ignored(self, actors):
        """T-ID-02：arguments 注入 actor=jiaming 仍按 worker 拒绝。"""
        with pytest.raises(Forbidden):
            registry.invoke(actors["worker"], "memory.hold",
                            {"text": "冒充", "actor": "jiaming",
                             "principal": "jiaming"}, None)

    def test_T_ID_06_revoked_binding_rejected(self, actors):
        """T-ID-06：撤销绑定后旧 token 立即失效。"""
        identity.revoke_binding("qiaosheng", "binding_worker")
        with pytest.raises(Exception):
            identity.authenticate(TOKENS["worker"])
        # 恢复供后续测试
        identity.seed(TOKENS)



class TestRET:
    def test_T_RET_05_late_embedding_rejected(self, actors):
        """T-RET-05：迟到向量（旧 projection_hash）不参与命中。"""
        from mariposa import config as _cfg
        old = _cfg.SEMANTIC_PROVIDER
        _cfg.SEMANTIC_PROVIDER = "local_bge_zh"
        try:
            import os
            os.environ.setdefault("FASTEMBED_CACHE_PATH",
                                  r"D:\mariposa\runtime\models")
            h = _hold(actors, "迟到的向量测试：阳台的三角梅开了两朵")
            with db.formal() as conn:
                from mariposa.retrieval import semantic
                semantic.reindex(conn, h["memory_id"])
                good_vec = conn.execute(
                    "SELECT vector FROM memory_embeddings WHERE memory_id=?",
                    (h["memory_id"],)).fetchone()["vector"]
                # 模拟"迟到旧 job"：插入带过期 hash 的向量行
                conn.execute(
                    "INSERT OR REPLACE INTO memory_embeddings(memory_id, model,"
                    " dim, projection_hash, vector, created_at)"
                    " VALUES(?, '__late_job__', 512, 'stale', ?, 't')",
                    (h["memory_id"], good_vec))
                hits = semantic.semantic_search(conn, "三角梅开花", 5)
                rows = conn.execute(
                    "SELECT model, projection_hash FROM memory_embeddings"
                    " WHERE memory_id=?", (h["memory_id"],)).fetchall()
                proj = conn.execute(
                    "SELECT search_text_hash FROM retrieval_documents WHERE"
                    " memory_id=?", (h["memory_id"],)).fetchone()
            models = {r["model"]: r["projection_hash"] for r in rows}
            assert models["__late_job__"] == "stale"  # 迟到行从未被安装
            # WP03：有效向量的 model 身份=名|语料 generation
            assert models.get(
                "BAAI/bge-small-zh-v1.5|eventbody-v1") == proj[
                "search_text_hash"]
            assert models.get(
                "BAAI/bge-small-zh-v1.5|eventbody-v1") == proj["search_text_hash"]
            assert h["memory_id"] in {x["memory_id"] for x in hits}  # 自愈用新向量
        finally:
            _cfg.SEMANTIC_PROVIDER = old

    def test_T_RET_11_filter_before_vector(self, actors):
        """T-RET-11：禁用资源不进语义候选（hidden 桶高相关也不命中）。"""
        from mariposa import config as _cfg
        old = _cfg.SEMANTIC_PROVIDER
        _cfg.SEMANTIC_PROVIDER = "local_bge_zh"
        try:
            import os
            os.environ.setdefault("FASTEMBED_CACHE_PATH",
                                  r"D:\mariposa\runtime\models")
            h = _hold(actors, "语义过滤：窗台薄荷长势旺盛")
            with db.formal() as conn:
                from mariposa.retrieval import semantic
                semantic.reindex(conn, h["memory_id"])
                conn.execute("UPDATE memories SET visibility='hidden' WHERE"
                             " memory_id=?", (h["memory_id"],))
                hits = semantic.semantic_search(conn, "薄荷长得怎么样", 5)
            assert not any(x["memory_id"] == h["memory_id"] for x in hits)
        finally:
            _cfg.SEMANTIC_PROVIDER = old





