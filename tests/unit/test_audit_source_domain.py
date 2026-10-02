"""Source 域审计修复回归（2026-10-02 基线审计 P2/P3 批）。

- CB-020：around_seq 窗口总条数 ≤ limit（审计反例 source-around-exact-
  group：7 条同序号、limit=1 返 7 条且 has_more=false）。
- CB-022：外部会话 ID 跨 provider 碰撞明确拒绝（fetchone 静默选行）。
- CB-027：会话聚合与发布同事务（导入 3 条后 message_count 不再是 0）。
- CB-028：幂等短路分支释放 staging（重复导入不再积累 .part）。
- CB-029：区间正文字节预算按 UTF-8 实际编码计（中文不再 3 倍突破）。
- CB-030：Source 公开读取统一携带无指令权身份标记。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from mariposa import config as cfg
from mariposa import db
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.source import importer, query
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _export_file(convs: list[dict], prefix: str) -> str:
    tmp = Path(tempfile.mkdtemp())
    f = tmp / f"{prefix}.json"
    f.write_text(json.dumps(convs, ensure_ascii=False), encoding="utf-8")
    return str(f)


def _staging_parts() -> int:
    d = cfg.SOURCE_INCOMING_DIR
    if not d.exists():
        return 0
    return len(list(d.glob("*.part")))


# ---------------------------------------------------------------- CB-020

class TestAroundSeqBounded:

    def test_duplicate_sequence_group_respects_limit(self, actors):
        """审计反例：同一会话第 1 序号经 7 次合法增量导出写入 7 个
        不同 UUID——around_seq=1, limit=1 此前返 7 条；现在 ≤ limit
        且 has_more 如实为真。"""
        for i in range(7):
            # 同一会话、每次一条新 UUID 消息（合法增量导出形态：
            # 每个快照内该消息都排在首位 → sequence 同为 1）
            f = _export_file([{"uuid": "c-dup", "chat_messages": [{
                "uuid": f"m-dup{i}", "sender": "human",
                "created_at": "2026-09-28T10:00:00Z",
                "content": [{"type": "text",
                             "text": f"同序号消息 {i}"}]}]}],
                f"dup{i}")
            importer.import_file("jiaming", f)
        with db.formal() as conn:
            conv_id = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='c-dup'").fetchone()["id"]
            n = conn.execute(
                "SELECT COUNT(*) c FROM source_messages WHERE"
                " conversation_id=? AND sequence=0", (conv_id,)
            ).fetchone()["c"]
        assert n == 7, f"前置：同序号组应有 7 行，实际 {n}"
        out = query.get_conversation(conv_id, around_seq=0, limit=1)
        assert len(out["messages"]) <= 1, \
            f"around 窗口必须受 limit 约束：{len(out['messages'])}"
        assert out["has_more"] is True, "组被截断必须如实披露"
        # 复合游标续页可达其余成员
        cursor = out["next_after_cursor"]
        page2 = query.get_conversation(conv_id, after_seq=cursor, limit=100)
        assert len(page2["messages"]) >= 6

    def test_normal_around_window_still_works(self, actors):
        msgs = [{"uuid": f"m-n{i}", "sender": "human",
                 "created_at": f"2026-09-28T10:{i:02d}:00Z",
                 "content": [{"type": "text", "text": f"正常消息 {i}"}]}
                for i in range(5)]
        f = _export_file([{"uuid": "c-normal", "chat_messages": msgs}],
                         "normal")
        importer.import_file("jiaming", f)
        with db.formal() as conn:
            conv_id = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='c-normal'").fetchone()["id"]
        out = query.get_conversation(conv_id, around_seq=3, limit=3)
        assert [m["sequence"] for m in out["messages"]] == [2, 3, 4]


# ---------------------------------------------------------------- CB-022

class TestConversationIdentityDisambiguation:

    def test_cross_provider_external_id_rejected(self, actors):
        """审计反例：同一外部会话 ID 指向两个 provider——fetchone 静默
        选了 claude；现在明确 AMBIGUOUS。"""
        f1 = _export_file([{"uuid": "shared-ext-id", "chat_messages": [{
            "uuid": "m-p1", "sender": "human",
            "created_at": "2026-09-28T10:00:00Z",
            "content": [{"type": "text", "text": "provider1 消息"}]}]}],
            "p1")
        importer.import_file("jiaming", f1)
        # 直接插一个满足当前唯一约束的第二 provider 会话与消息
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO source_conversations(id, provider,"
                " provider_conversation_id, title, created_at, updated_at,"
                " message_count, last_import_batch_id,"
                " first_import_batch_id)"
                " VALUES('conv-other', 'other_export', 'shared-ext-id',"
                " 't', '2026-09-28T00:00:00Z', '2026-09-28T00:00:00Z',"
                " 0, 'batch-synthetic', 'batch-synthetic')")
            conn.execute(
                "INSERT INTO source_messages(id, provider,"
                " conversation_id, provider_conversation_id,"
                " provider_message_id, normalized_sender, sequence,"
                " import_batch_id, published, created_at)"
                " VALUES('msg-other', 'other_export', 'conv-other',"
                " 'shared-ext-id', 'm-p1', 'human', 1,"
                " 'batch-synthetic', 1, '2026-09-28T10:00:00Z')")
        with pytest.raises(Forbidden) as ei:
            query.get_conversation("shared-ext-id")
        assert ei.value.code == "AMBIGUOUS_CONVERSATION_ID"
        # 内部 ID 精确路径不受影响
        with db.formal() as conn:
            internal = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider='claude'").fetchone()["id"]
        out = query.get_conversation(internal)
        assert out["conversation"]["id"] == internal


# ---------------------------------------------------------------- CB-027

class TestAggregatesWithPublish:

    def test_message_count_reflects_published(self, actors):
        """审计反例：导入 3 条有效消息，conversation.get 标题区显示
        0 条而 messages/published_total 是 3。"""
        msgs = [{"uuid": f"m-agg{i}", "sender": "human",
                 "created_at": f"2026-09-28T11:{i:02d}:00Z",
                 "content": [{"type": "text", "text": f"聚合消息 {i}"}]}
                for i in range(3)]
        f = _export_file([{"uuid": "c-agg", "chat_messages": msgs}], "agg")
        importer.import_file("jiaming", f)
        with db.formal() as conn:
            conv_id = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='c-agg'").fetchone()["id"]
        out = query.get_conversation(conv_id)
        assert out["conversation"]["message_count"] == 3
        assert out["published_total"] == 3
        assert len(out["messages"]) == 3


# ---------------------------------------------------------------- CB-028

class TestIdempotentImportStaging:

    def test_already_imported_leaves_no_staging_copy(self, actors):
        f = _export_file([{"uuid": "c-idem", "chat_messages": [{
            "uuid": "m-idem", "sender": "human",
            "created_at": "2026-09-28T12:00:00Z",
            "content": [{"type": "text", "text": "幂等导入消息"}]}]}],
            "idem")
        importer.import_file("jiaming", f)
        before = _staging_parts()
        r2 = importer.import_file("jiaming", f)
        assert r2["status"] == "already_imported"
        assert _staging_parts() == before, \
            "幂等短路不得遗留完整 staging 副本"


# ---------------------------------------------------------------- CB-029

class TestRangeByteBudget:

    def test_cjk_text_budget_in_utf8_bytes(self, actors, monkeypatch):
        msgs = [{"uuid": "m-byte", "sender": "human",
                 "created_at": "2026-09-28T13:00:00Z",
                 "content": [{"type": "text", "text": "八个中文字符的字"}]}]
        f = _export_file([{"uuid": "c-byte", "chat_messages": msgs}], "byte")
        importer.import_file("jiaming", f)
        with db.formal() as conn:
            conv_id = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='c-byte'").fetchone()["id"]
        old = cfg.SOURCE_RANGE_MAX_TEXT_BYTES
        monkeypatch.setattr(cfg, "SOURCE_RANGE_MAX_TEXT_BYTES", 10)
        try:
            # 9 个中文字符 = 27 UTF-8 字节 > 10 字节预算；旧实现按
            # len(str)=9 计数会放行
            with pytest.raises(Forbidden):
                query.open_range(conv_id, "m-byte", "m-byte")
        finally:
            pass
        assert old


# ---------------------------------------------------------------- CB-030

class TestTrustMarkers:

    def _seed(self, actors):
        msgs = [{"uuid": "m-trust", "sender": "human",
                 "created_at": "2026-09-28T14:00:00Z",
                 "content": [{"type": "text", "text": "标记检查消息"}]}]
        f = _export_file([{"uuid": "c-trust", "chat_messages": msgs}],
                         "trust")
        importer.import_file("jiaming", f)
        with db.formal() as conn:
            conv_id = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='c-trust'").fetchone()["id"]
        return conv_id

    def test_all_public_reads_carry_markers(self, actors):
        conv_id = self._seed(actors)
        conv = query.get_conversation(conv_id)
        assert conv["content_role"] == "retrieved_memory"
        assert conv["instruction_authority"] == "none"
        assert all(m["content_role"] == "retrieved_memory"
                   and m["instruction_authority"] == "none"
                   for m in conv["messages"])
        srch = query.search("标记检查")
        assert srch["content_role"] == "retrieved_memory"
        assert all(h["instruction_authority"] == "none"
                   for h in srch["hits"])
        rng = query.open_range(conv_id, "m-trust", "m-trust")
        assert rng["content_role"] == "retrieved_memory"
        assert rng["instruction_authority"] == "none"
        msg = query.get_message(provider_message_id="m-trust")
        assert msg["content_role"] == "retrieved_memory"
        assert msg["instruction_authority"] == "none"
        assert msg["message"]["instruction_authority"] == "none"
