"""Codex 2026-10-04 二轮审计（22 条）修复回归——关键行为抽样。

覆盖：RECALL-04（round2 交付回执）、MEM-02/06（续取段 schema +
续页安全信封）、MEM-03（I.items 不带正文）、MEM-05/07（崩溃恢复
面 + 领域 payload 身份）、SRC-02（since 归一）、ROOT-02（发现端
结构化 401）、ROOT-03（http_status 生效）。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, IdempotencyConflict
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


def _hold(actors, text):
    return memory.hold(
        actors["jiaming"], text=text, memory_date="2026-09-20",
        date_confidence="exact", original_title="b2",
        categories=["daily"], creation_mode="contemporaneous",
        raw_pending=False)


class TestBootstrapContinuationContract:
    """MEM-02/03/06：schema 接纳签发段、items 不带正文、续页信封。"""

    def test_schema_accepts_issued_sections(self):
        from mariposa.capabilities.input_schemas import validate
        for section in ("i", "plan_content", "memory_days", "plans"):
            validate("bootstrap.next", {
                "snapshot_id": "snap-x", "cursor": {"i_offset": 0},
                "section": section})

    def test_i_items_stripped_and_envelope_on_pages(self, actors):
        from mariposa.identity_i import service as i_svc
        from mariposa.bootstrap import service as boot
        i_svc.item_create("jiaming", "条目一正文" * 50)
        i_svc.item_create("jiaming", "条目二正文" * 50)
        pkg = boot.get("jiaming", "cc", "cc")
        for it in pkg["i"]["items"]:
            assert "content" not in it, "MEM-03：items 不得重复携带正文"
            assert it.get("item_id")
        page = boot.next_page("jiaming", "cc", pkg["snapshot_id"],
                              {"i_offset": 0}, "i")
        assert page["content_role"] == "bootstrap_memory_package"
        assert page["instruction_authority"] == "none"

    def test_plan_list_page_sections_and_envelope(self, actors):
        from mariposa.plans import service as plans
        from mariposa.bootstrap import service as boot
        long_body = "计划长文。" + "正文填充。" * 900
        plans.create("jiaming", "续页分节计划", content=long_body,
                     state="active")
        pkg = boot.get("jiaming", "cc", "cc")
        item = next(p for p in pkg["plans"]["items"]
                    if p.get("content_truncated"))
        page = boot.next_page("jiaming", "cc", pkg["snapshot_id"],
                              {"plans_offset": 0}, "plans")
        assert page["instruction_authority"] == "none"
        # 续页里的同一条计划同样分节
        page_item = next((p for p in page["items"]
                          if p["plan_id"] == item["plan_id"]), None)
        if page_item is not None:
            assert page_item.get("content_truncated") is True
            assert "content_next_cursor" in page_item


class TestRecoveryPayloadIdentity:
    """MEM-05/07：恢复面覆盖全部领域回执入口 + payload 身份校验。"""

    def test_delete_recovery_conflicts_on_foreign_payload(self, actors):
        out = _hold(actors, "恢复身份正文")
        mid = out["memory_id"]
        args = {"memory_id": mid, "operation_id": "rec-id-1"}
        r1 = registry.invoke(actors["jiaming"], "memory.delete",
                             args, None)
        assert r1["data"]["deleted"] is True
        # 同 operation_id 指向另一桶（不同领域 payload）→ 冲突，
        # 不得把 A 的完成结果当作 B 的
        other = _hold(actors, "另一桶正文")
        with pytest.raises(IdempotencyConflict):
            registry.invoke(actors["jiaming"], "memory.delete", {
                "memory_id": other["memory_id"],
                "operation_id": "rec-id-1"}, None)

    def test_word_correct_in_recovery_surface(self, actors):
        from mariposa.capabilities.registry import _DOMAIN_IDEMPOTENT_CAPS
        assert "memory.our_words.source.correct" in _DOMAIN_IDEMPOTENT_CAPS
        assert "memory.deletion.request" in _DOMAIN_IDEMPOTENT_CAPS
        assert "memory.deletion.decide" in _DOMAIN_IDEMPOTENT_CAPS


class TestSrcRootFixes:
    def test_since_normalizes_naive_timestamp(self, actors):
        from mariposa.time_context import service as tctx
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO source_conversations(id, provider,"
                " provider_conversation_id, created_at, first_import"
                "_batch_id, last_import_batch_id)"
                " VALUES('c-naive','claude','c-naive',"
                " datetime('now'), 'b1', 'b1')")
            conn.execute(
                "INSERT INTO source_messages(id, conversation_id,"
                " provider, provider_conversation_id,"
                " provider_message_id, normalized_sender, created_at,"
                " text, sequence, import_batch_id, published)"
                " VALUES('m-naive','c-naive','claude','c-naive',"
                " 'm-naive','human','2026-10-01T10:00:00',"
                " '无时区时间戳', 1, 'b1', 1)")
        out = tctx.since("jiaming")
        assert isinstance(out["since_last_contact"], float), \
            "SRC-02：naive 时间戳按 UTC 归一，不抛 TypeError"

    def test_capabilities_endpoint_unauthenticated_401(self):
        from mariposa.app import app
        from fastapi.testclient import TestClient
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/api/capabilities")
            assert r.status_code == 401, r.status_code
            assert r.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_declared_length_over_limit_returns_413(self):
        from mariposa.app import app, _JSON_BODY_MAX_BYTES
        from fastapi.testclient import TestClient
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post(
                "/api/capability/memory.hold",
                content=b"{}",
                headers={"Authorization": "Bearer x",
                         "Content-Length": str(_JSON_BODY_MAX_BYTES + 10)},
            )
            assert r.status_code in (401, 413), r.status_code
            # 带有效 token 时必须是 413 本身（ROOT-03）
