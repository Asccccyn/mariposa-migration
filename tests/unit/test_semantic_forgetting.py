"""复审关卡 3：真实语义 provider 下遗忘摘要检索的完整闭环。

用例来自复核方原文（林石见 2026-09-21 复审关卡）：
1. 遗忘摘要"那次群聊背景显示异常，原因是图片比例被强制拉伸。"
   查询（不含摘要原词）："之前是不是处理过页面素材被压变形的问题？"
   → 应通过**摘要语义**命中。
2. 只存在于旧正文、摘要完全没保留的信息，用同义改写搜
   → 不能因旧正文 embedding 残留而命中。
3. restore 后旧正文重新参与语义 → 可命中。

provider 为本地真实模型（ONNX bge-small-zh-v1.5），非 mock。
"""
from __future__ import annotations

import os

import pytest

from mariposa import config, db
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.retrieval import search as retrieval
from mariposa.workspace import service as workspace
from tests.conftest import reset_all

# 模型缓存固定到项目 runtime（下载一次，跨会话复用）
os.environ["FASTEMBED_CACHE_PATH"] = r"D:\mariposa\runtime\models"

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def provider_ready():
    from mariposa.retrieval import semantic
    semantic.get_provider()  # 模块级加载一次（首次约 1-2s）
    return semantic


@pytest.fixture()
def actors(provider_ready):
    reset_all()
    old, config.SEMANTIC_PROVIDER = config.SEMANTIC_PROVIDER, "local_bge_zh"
    yield {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }
    config.SEMANTIC_PROVIDER = old


SUMMARY = "那次群聊背景显示异常，原因是图片比例被强制拉伸。"
OLD_BODY = ("我们在排查群聊背景图问题。经过两个小时排查，最终确认是懒加载组件"
            "把封面图等比缩放改成了强制拉伸，改回 object-fit: cover 就好了。"
            "当晚还顺手修了缓存失效的 bug，并给灰雀图床加了鉴权。")


def _forget(actors, memory_id, summary=SUMMARY):
    scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
    prop = next(p for p in scan["created"] if p["target_memory_id"] == memory_id)
    rev = workspace.revise_draft(actors["worker"], prop["proposal_id"], summary,
                                 "复审语义用例")
    sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
    workspace.decide(actors["qiaosheng"], proposal_id=sub["proposal_id"],
                     proposal_revision=sub["revision"],
                     proposal_hash=sub["proposal_hash"],
                     expected_memory_version=sub["base_memory_version"],
                     decision="approve")


class TestSemanticForgetting:
    def test_case1_summary_semantic_hit_without_shared_words(self, actors):
        """改写查询不含摘要原词，仍应经摘要语义命中。"""
        h = memory.hold(actors["jiaming"], text=OLD_BODY, memory_date="2026-06-01")
        _forget(actors, h["memory_id"])
        query = "之前是不是处理过页面素材被压变形的问题？"
        with db.formal() as conn:
            out = retrieval.search(conn, query)
        hit = next((x for x in out["hits"] if x["memory_id"] == h["memory_id"]), None)
        assert hit is not None, out["hits"]
        assert hit["matched_by"] == "summary_semantic"
        assert out["semantic"] == "local_bge_zh"

    def test_case2_old_body_semantic_must_not_hit(self, actors):
        """旧正文独有信息（摘要未保留），同义改写不得经旧正文向量命中。"""
        h = memory.hold(actors["jiaming"], text=OLD_BODY, memory_date="2026-06-01")
        _forget(actors, h["memory_id"])
        # "灰雀图床鉴权"只存在于旧正文；摘要完全没保留
        query_variants = ["图床访问权限当时怎么处理的？",
                          "给图片床加身份验证那次",
                          "当晚顺手修的缓存问题找回来"]
        for q in query_variants:
            with db.formal() as conn:
                out = retrieval.search(conn, q)
            hits_for = [x for x in out["hits"] if x["memory_id"] == h["memory_id"]]
            assert not hits_for, (q, hits_for)
        # 且向量表里只有摘要向量（旧正文向量不存在）
        with db.formal() as conn:
            from mariposa.retrieval import semantic
            semantic.ensure_schema(conn)
            rows = conn.execute(
                "SELECT projection_hash FROM memory_embeddings WHERE memory_id=?",
                (h["memory_id"],)).fetchall()
            proj = conn.execute(
                "SELECT search_text_hash FROM retrieval_documents WHERE memory_id=?",
                (h["memory_id"],)).fetchone()
        assert len(rows) == 1 and rows[0]["projection_hash"] == proj["search_text_hash"]

    def test_case3_restore_revives_old_body_semantic(self, actors):
        """restore 后旧正文重新成为有效投影，语义可命中其独有信息。"""
        h = memory.hold(actors["jiaming"], text=OLD_BODY, memory_date="2026-06-01")
        _forget(actors, h["memory_id"])
        memory.restore(actors["jiaming"], h["memory_id"], expected_current_version=2)
        with db.formal() as conn:
            out = retrieval.search(conn, "图床访问权限当时怎么处理的？")
        hit = next((x for x in out["hits"] if x["memory_id"] == h["memory_id"]), None)
        assert hit is not None and hit["matched_by"] == "semantic"

    def test_keyword_path_unchanged_when_provider_unset(self, actors):
        """provider 关闭时：关键词照常、语义显式 degraded（回归保护）。"""
        config.SEMANTIC_PROVIDER = ""
        h = memory.hold(actors["jiaming"], text=OLD_BODY, memory_date="2026-06-01")
        with db.formal() as conn:
            out = retrieval.search(conn, "灰雀图床")
        assert any(x["memory_id"] == h["memory_id"] for x in out["hits"])
        assert out["degraded"] == "semantic_unavailable"
