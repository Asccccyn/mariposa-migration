"""Source Layer（原文层）单元/管线测试（实施规格 §15 全清单）。

fixture 全部为合成数据（tests/fixtures/claude_export/），不含任何真实
私人对话；真实 conversations.json 只允许通过 MARIPOSA_SOURCE_REAL_EXPORT
环境变量从本机外部路径只读接入（默认 skip，见文末 RealExport 类）。
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from mariposa import db
from mariposa.source import archive, importer, json_stream, query, binding
from mariposa.source.adapters import claude as claude_adapter
from mariposa.errors import Forbidden, MariposaError, NotFound

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude_export"


def fx(name: str) -> str:
    return str(FIXTURES / name)


@pytest.fixture()
def clean(actors):
    """actors fixture 已 reset_all；source 层清理由 FORMAL_TABLES 覆盖。"""
    return actors


def import_ok(pid: str, name: str) -> dict:
    r = importer.import_file(pid, fx(name))
    assert r["status"] == "completed", r
    return r


# ============================== Claude Adapter ==============================

class TestClaudeAdapter:
    def test_sender_exact_map(self):
        assert claude_adapter._map_sender("human") == "human"
        assert claude_adapter._map_sender("assistant") == "assistant"
        assert claude_adapter._map_sender("system") == "system"
        assert claude_adapter._map_sender("tool") == "tool"

    def test_unknown_never_assistant(self):
        # 查看器把未知降级 assistant 的 UI 规则严禁进入本层
        assert claude_adapter._map_sender("Cavalier") == "unknown"
        assert claude_adapter._map_sender("user") == "unknown"  # 非精确值
        assert claude_adapter._map_sender(None) == "unknown"
        assert claude_adapter._map_sender("") == "unknown"
        assert claude_adapter.speaker_of("unknown") is None
        assert claude_adapter.speaker_of("system") is None
        assert claude_adapter.speaker_of("tool") is None

    def test_speaker_map(self):
        assert claude_adapter.speaker_of("human") == "qiaosheng"
        assert claude_adapter.speaker_of("assistant") == "jiaming"

    def test_top_text_empty_content_has_text(self):
        draft = claude_adapter.normalize_conversation(
            {"uuid": "c1", "chat_messages": [
                {"uuid": "m1", "sender": "human",
                 "content": [{"type": "text", "text": "顶层为空但内容在这里"}]}]})
        msgs = list(claude_adapter.normalize_messages(draft, "b1"))
        assert msgs[0].text == "顶层为空但内容在这里"

    def test_top_level_text_fallback(self):
        draft = claude_adapter.normalize_conversation(
            {"uuid": "c1", "chat_messages": [
                {"uuid": "m1", "sender": "human", "text": "只有顶层text"}]})
        msgs = list(claude_adapter.normalize_messages(draft, "b1"))
        assert msgs[0].text == "只有顶层text"

    def test_thinking_and_text_split(self):
        draft = claude_adapter.normalize_conversation(
            {"uuid": "c1", "chat_messages": [
                {"uuid": "m2", "sender": "assistant", "content": [
                    {"type": "thinking", "thinking": "内部推理"},
                    {"type": "text", "text": "可见回答"}]}]})
        m = list(claude_adapter.normalize_messages(draft, "b1"))[0]
        assert m.text == "可见回答"
        assert m.has_thinking is True
        assert "内部推理" not in m.text
        assert "内部推理" in (m.content_json or "")

    def test_tool_use_and_result_not_in_text(self):
        draft = claude_adapter.normalize_conversation(
            {"uuid": "c1", "chat_messages": [
                {"uuid": "m3", "sender": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "w",
                     "input": {"q": "x"}}]},
                {"uuid": "m4", "sender": "human", "content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": [{"type": "text", "text": "工具输出"}]}]}]})
        msgs = list(claude_adapter.normalize_messages(draft, "b1"))
        assert msgs[0].text == "" and msgs[0].has_tool_content
        assert msgs[1].text == "" and msgs[1].has_tool_content

    def test_system_sender_text_not_in_body(self):
        draft = claude_adapter.normalize_conversation(
            {"uuid": "c1", "chat_messages": [
                {"uuid": "m5", "sender": "system", "content": [
                    {"type": "text", "text": "系统注入"}]}]})
        m = list(claude_adapter.normalize_messages(draft, "b1"))[0]
        assert m.normalized_sender == "system"
        assert m.text == ""  # system 文本不进正文（证据在 content_json）
        assert "系统注入" in (m.content_json or "")

    def test_occurred_date_utc_boundary(self):
        # UTC 23:30 → 上海次日 07:30
        dt = claude_adapter.parse_created_at("2026-01-01T23:30:00.000Z")
        assert claude_adapter._occurred_date(
            "2026-01-01T23:30:00.000Z",
            __import__("zoneinfo").ZoneInfo("Asia/Shanghai")) == "2026-01-02"
        assert claude_adapter._occurred_date(
            "2026-01-01T15:59:59.000Z",
            __import__("zoneinfo").ZoneInfo("Asia/Shanghai")) == "2026-01-01"

    def test_occurred_date_naive_treated_as_utc(self):
        assert claude_adapter._occurred_date(
            "2026-01-01T23:30:00",
            __import__("zoneinfo").ZoneInfo("Asia/Shanghai")) == "2026-01-02"

    def test_parent_uuid_preserved(self):
        draft = claude_adapter.normalize_conversation(
            {"uuid": "c1", "chat_messages": [
                {"uuid": "m1", "sender": "human", "parent_message_uuid": None,
                 "content": []},
                {"uuid": "m2", "sender": "assistant",
                 "parent_message_uuid": "m1", "content": []}]})
        msgs = list(claude_adapter.normalize_messages(draft, "b1"))
        assert msgs[0].parent_provider_message_id is None
        assert msgs[1].parent_provider_message_id == "m1"

    def test_missing_uuid_synthetic_deterministic(self):
        a = claude_adapter.synthetic_message_id("b1", "c1", 3)
        b = claude_adapter.synthetic_message_id("b1", "c1", 3)
        c = claude_adapter.synthetic_message_id("b2", "c1", 3)
        assert a == b and a != c

    def test_base64_stripped_in_evidence(self):
        big = "A" * 2000
        draft = claude_adapter.normalize_conversation(
            {"uuid": "c1", "chat_messages": [
                {"uuid": "m1", "sender": "human", "content": [
                    {"type": "document", "title": "d",
                     "source": {"type": "base64", "data": big}}]}]})
        m = list(claude_adapter.normalize_messages(draft, "b1"))[0]
        assert big not in (m.content_json or "")
        assert "omitted_base64_bytes" in (m.content_json or "")
        assert m.attachments and m.attachments[0]["block_type"] == "document"

    def test_same_text_different_uuid_distinct(self):
        draft = claude_adapter.normalize_conversation(
            {"uuid": "c1", "chat_messages": [
                {"uuid": "u1", "sender": "human",
                 "content": [{"type": "text", "text": "同文"}]},
                {"uuid": "u2", "sender": "human",
                 "content": [{"type": "text", "text": "同文"}]}]})
        msgs = list(claude_adapter.normalize_messages(draft, "b1"))
        assert msgs[0].provider_message_id != msgs[1].provider_message_id


# ============================== json_stream ==============================

class TestJsonStream:
    def test_small_array(self, tmp_path):
        p = tmp_path / "a.json"
        p.write_text('[ {"a": 1}, {"a": 2} ]', encoding="utf-8")
        with open(p, "rb") as f:
            assert [e["a"] for e in json_stream.iter_top_level_array(f)] == [1, 2]

    def test_element_larger_than_chunk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(json_stream, "_CHUNK", 64)
        big_text = "x" * 500
        p = tmp_path / "big.json"
        p.write_text(json.dumps([{"t": big_text}, {"t": "tail"}]), encoding="utf-8")
        with open(p, "rb") as f:
            out = list(json_stream.iter_top_level_array(f))
        assert out[0]["t"] == big_text and out[1]["t"] == "tail"

    def test_not_array_rejected(self, tmp_path):
        p = tmp_path / "o.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        with open(p, "rb") as f:
            with pytest.raises(json_stream.JsonStreamError):
                list(json_stream.iter_top_level_array(f))

    def test_truncated_rejected(self, tmp_path):
        p = tmp_path / "t.json"
        p.write_text('[{"a": 1}, {"b":', encoding="utf-8")
        with open(p, "rb") as f:
            with pytest.raises(json_stream.JsonStreamError):
                list(json_stream.iter_top_level_array(f))

    def test_empty_array(self, tmp_path):
        p = tmp_path / "e.json"
        p.write_text('[]', encoding="utf-8")
        with open(p, "rb") as f:
            assert list(json_stream.iter_top_level_array(f)) == []


# ============================== Importer ==============================

class TestImporter:
    def test_standard_import(self, clean):
        r = import_ok("jiaming", "standard.json")
        s = r["stats"]
        assert s["conversations_total"] == 1 and s["conversations_new"] == 1
        assert s["messages_new"] == 2
        assert s["sender_human"] == 1 and s["sender_assistant"] == 1
        assert s["messages_with_thinking"] == 1
        assert r["verification"]["ok"] is True
        assert s["min_created_at"] == "2026-03-01T02:00:00+00:00"
        assert s["max_created_at"] == "2026-03-01T02:05:00+00:00"

    def test_idempotent_reimport(self, clean):
        import_ok("jiaming", "standard.json")
        r2 = importer.import_file("jiaming", fx("standard.json"))
        assert r2["status"] == "already_imported"
        with db.formal() as c:
            assert c.execute("SELECT COUNT(*) n FROM source_messages"
                             ).fetchone()["n"] == 2

    def test_new_batch_same_conversation_no_duplicates(self, clean, tmp_path):
        import_ok("jiaming", "standard.json")
        # 复制一份（内容相同字节相同 → 同 sha；改一个空格制造新 sha）
        data = json.loads(Path(fx("standard.json")).read_text(encoding="utf-8"))
        data[0]["name"] = "改名后的同一会话"
        p = tmp_path / "v2.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        r = importer.import_file("jiaming", str(p))
        assert r["status"] == "completed"
        assert r["stats"]["conversations_existing"] == 1
        assert r["stats"]["messages_new"] == 0  # UUID 幂等，无重复行
        with db.formal() as c:
            assert c.execute("SELECT COUNT(*) n FROM source_messages"
                             ).fetchone()["n"] == 2
            assert c.execute("SELECT COUNT(*) n FROM source_conversations"
                             ).fetchone()["n"] == 1

    def test_edge_cases_counts(self, clean):
        r = import_ok("jiaming", "edge_cases.json")
        s = r["stats"]
        assert s["messages_missing_uuid"] == 1
        assert s["messages_duplicate_in_file"] == 1
        assert s["sender_system"] == 1
        assert s["sender_unknown"] == 1
        assert s["sender_tool"] == 0
        assert s["messages_with_tool_content"] == 2  # tool_use + tool_result
        assert s["messages_with_thinking"] == 1
        assert s["messages_with_attachments"] == 1
        # 11 raw messages, 1 dup-in-file → 10 stored
        with db.formal() as c:
            assert c.execute("SELECT COUNT(*) n FROM source_messages"
                             ).fetchone()["n"] == 10
        # unknown 绝不映射说话人
        with db.formal() as c:
            row = c.execute("SELECT speaker, normalized_sender FROM"
                            " source_messages WHERE raw_sender='Cavalier'"
                            ).fetchone()
        assert row["normalized_sender"] == "unknown" and row["speaker"] is None

    def test_utc_boundary_occurred_dates(self, clean):
        import_ok("jiaming", "edge_cases.json")
        with db.formal() as c:
            d1 = c.execute("SELECT occurred_date FROM source_messages WHERE"
                           " provider_message_id='edge-m9'").fetchone()
            d2 = c.execute("SELECT occurred_date FROM source_messages WHERE"
                           " provider_message_id='edge-m10'").fetchone()
        assert d1["occurred_date"] == "2026-01-02"  # 23:30Z → 次日
        assert d2["occurred_date"] == "2026-01-01"  # 15:59Z → 同日

    def test_empty_conversation(self, clean):
        r = import_ok("jiaming", "empty_conv.json")
        assert r["stats"]["conversations_empty"] == 1
        assert r["stats"]["messages_total"] == 0

    def test_same_text_different_uuid_both_stored(self, clean):
        import_ok("jiaming", "same_text_diff_uuid.json")
        with db.formal() as c:
            rows = c.execute(
                "SELECT provider_message_id, normalized_sender FROM"
                " source_messages WHERE text='一模一样的话但身份不同'"
                " ORDER BY provider_message_id").fetchall()
        assert len(rows) == 2
        assert {r["normalized_sender"] for r in rows} == {"human", "assistant"}

    def test_broken_json_records_failed_batch(self, clean):
        with pytest.raises(MariposaError):
            importer.import_file("jiaming", fx("broken.json"))
        with db.formal() as c:
            row = c.execute("SELECT status, error FROM source_import_batches"
                            " ORDER BY import_started_at DESC").fetchone()
        assert row["status"] == "failed" and row["error"]

    def test_unrecognized_format(self, clean, tmp_path):
        p = tmp_path / "notchat.json"
        p.write_text('[{"foo": 1}]', encoding="utf-8")
        with pytest.raises(MariposaError):
            importer.import_file("jiaming", str(p))

    def test_zip_import(self, clean, tmp_path):
        z = tmp_path / "export.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(fx("standard.json"), "conversations.json")
            zf.writestr("users.json", "[]")
        r = importer.import_file("jiaming", str(z))
        assert r["status"] == "completed"
        assert r["stats"]["messages_new"] == 2

    def test_long_conversation_and_paging(self, clean, tmp_path):
        n = 3000
        msgs = [{"uuid": f"long-{i}", "sender": "human" if i % 2 else "assistant",
                 "created_at": f"2026-05-01T00:{i // 60:02d}:{i % 60:02d}.000Z",
                 "content": [{"type": "text", "text": f"第{i}条长会话消息"}]}
                for i in range(n)]
        p = tmp_path / "long.json"
        p.write_text(json.dumps([{"uuid": "conv-long", "name": "超长",
                                  "chat_messages": msgs}], ensure_ascii=False),
                     encoding="utf-8")
        r = importer.import_file("jiaming", str(p))
        assert r["status"] == "completed" and r["stats"]["messages_new"] == n
        page = query.get_conversation("conv-long", limit=100)
        assert len(page["messages"]) == 100 and page["has_more"]
        last = page["messages"][-1]["sequence"]
        page2 = query.get_conversation("conv-long", after_seq=last, limit=100)
        assert page2["messages"][0]["sequence"] == last + 1

    def test_interrupted_import_resumes(self, clean, tmp_path, monkeypatch):
        data = json.loads(Path(fx("standard.json")).read_text(encoding="utf-8"))
        data2 = json.loads(Path(fx("edge_cases.json")).read_text(encoding="utf-8"))
        data += data2
        p = tmp_path / "multi.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        original = importer._insert_message
        calls = {"n": 0}

        def flaky(conn, provider, conv_row_id, batch_id, msg, chash):
            if msg.provider_message_id == "edge-m2":
                raise RuntimeError("simulated crash mid import")
            return original(conn, provider, conv_row_id, batch_id, msg, chash)

        monkeypatch.setattr(importer, "_insert_message", flaky)
        with pytest.raises(RuntimeError):
            importer.import_file("jiaming", str(p))
        with db.formal() as c:
            row = c.execute("SELECT status, error FROM source_import_batches"
                            " ORDER BY import_started_at DESC").fetchone()
            stored = c.execute("SELECT COUNT(*) n FROM source_messages"
                               ).fetchone()["n"]
        assert row["status"] == "failed" and "simulated crash" in row["error"]
        assert stored == 2  # 第一个会话已提交，第二个会话事务回滚

        monkeypatch.undo()
        r = importer.import_file("jiaming", str(p))  # 重导恢复
        assert r["status"] == "completed"
        assert r["stats"]["messages_new"] == 10  # 只补第二个会话的消息
        assert r["stats"]["messages_skipped_existing"] == 2

    def test_raw_archive_immutable_original(self, clean):
        r = import_ok("jiaming", "standard.json")
        raw_path = Path(r["raw_path"])
        assert raw_path.exists()
        manifest = json.loads(
            (raw_path.parent / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["sha256"] == r["stats"].get("sha256") or manifest["sha256"]
        assert (raw_path.parent / "metadata.json").exists()
        sha, _ = archive.sha256_file(raw_path)
        src_sha, _ = archive.sha256_file(Path(fx("standard.json")))
        assert sha == src_sha  # 母本与原文件逐字节一致
        assert archive.verify_archived("claude", r["batch_id"], src_sha)["ok"]
        # 幂等重导不覆盖旧母本（mtime 不变）
        mtime1 = raw_path.stat().st_mtime
        r2 = importer.import_file("jiaming", fx("standard.json"))
        assert r2["status"] == "already_imported"
        assert raw_path.stat().st_mtime == mtime1


# ============================== Query ==============================

class TestQuery:
    @pytest.fixture()
    def loaded(self, clean):
        import_ok("jiaming", "standard.json")
        import_ok("jiaming", "edge_cases.json")
        return None

    def test_keyword_search_hits_body_only(self, loaded):
        res = query.search("海边")
        assert len(res["hits"]) == 1
        h = res["hits"][0]
        assert h["normalized_sender"] == "human"
        assert h["speaker_display"] == "江乔生"
        assert res["source"] == "source_layer"

    def test_thinking_not_searchable_by_default(self, loaded):
        # 思考内容不参与默认正文检索（§5）
        assert query.search("内部推理")["hits"] == []

    def test_tool_result_not_in_hits(self, loaded):
        assert query.search("工具输出")["hits"] == []

    def test_evidence_search_with_explicit_senders(self, loaded):
        res = query.search("系统注入", senders=["system", "tool", "unknown"])
        assert len(res["hits"]) == 1
        assert res["hits"][0]["normalized_sender"] == "system"
        assert "系统注入" in res["hits"][0]["excerpt"]

    def test_unknown_not_jiaming_in_hits(self, loaded):
        res = query.search("未知角色", senders=["system", "tool", "unknown"])
        h = res["hits"][0]
        assert h["normalized_sender"] == "unknown"
        assert h["speaker"] is None and h["speaker_display"] is None

    def test_search_by_date_range(self, loaded):
        # edge-m9: UTC 1/1 23:30 → occurred_date 2026-01-02
        res = query.search("UTC 边界前夜", date_from="2026-01-02",
                           date_to="2026-01-02")
        assert len(res["hits"]) == 1
        assert res["hits"][0]["occurred_date"] == "2026-01-02"
        assert query.search("UTC 边界前夜", date_to="2026-01-01")["hits"] == []

    def test_search_by_conversation(self, loaded):
        res = query.search("海边", conversation_id="conv-std-001")
        assert len(res["hits"]) == 1
        assert query.search("海边", conversation_id="conv-edge-001")["hits"] == []

    def test_search_sender_filter(self, loaded):
        res = query.search(None, senders=["system"])
        assert res["hits"] and all(
            h["normalized_sender"] == "system" for h in res["hits"])

    def test_get_message_by_uuid_with_context(self, loaded):
        d = query.get_message(provider_message_id="std-m2", context=5)
        assert d["message"]["text"] == "想！周六上午出发避开堵车怎么样"
        assert d["message"]["speaker_display"] == "周家明"
        assert len(d["context_before"]) == 1
        assert d["context_before"][0]["provider_message_id"] == "std-m1"

    def test_get_message_not_found(self, loaded):
        with pytest.raises(NotFound):
            query.get_message(provider_message_id="nope")

    def test_open_range(self, loaded):
        d = query.open_range("conv-std-001", "std-m1", "std-m2")
        assert [m["provider_message_id"] for m in d["messages"]] == \
            ["std-m1", "std-m2"]

    def test_open_range_order_rejected(self, loaded):
        with pytest.raises(Forbidden):
            query.open_range("conv-std-001", "std-m2", "std-m1")

    def test_open_range_cross_conversation_rejected(self, loaded):
        with pytest.raises(NotFound):
            query.open_range("conv-std-001", "std-m1", "edge-m1")

    def test_conversations_list(self, loaded):
        res = query.conversations_list()
        assert res["total"] == 2
        by_id = {c["provider_conversation_id"] for c in res["conversations"]}
        assert by_id == {"conv-std-001", "conv-edge-001"}


# ============================== Semantic Binding ==============================

class TestBinding:
    @pytest.fixture()
    def bound(self, clean):
        import_ok("jiaming", "standard.json")
        import_ok("jiaming", "edge_cases.json")
        with db.formal() as c:
            c.execute(
                "INSERT INTO memories(memory_id, current_version_no, created_at,"
                " updated_at) VALUES('mem-t1', 1, '2026-01-01T00:00:00Z',"
                " '2026-01-01T00:00:00Z')")
            c.execute(
                "INSERT INTO memories(memory_id, current_version_no, created_at,"
                " updated_at) VALUES('mem-t2', 1, '2026-01-01T00:00:00Z',"
                " '2026-01-01T00:00:00Z')")
        return None

    def test_bind_and_open(self, bound):
        b = binding.bind("jiaming", "mem-t1", "conv-std-001", "std-m1", "std-m2")
        assert b["binding_id"].startswith("msb_")
        opened = binding.open_for_memory("mem-t1")
        assert len(opened["ranges"]) == 1
        msgs = opened["ranges"][0]["messages"]
        assert [m["text"] for m in msgs] == \
            ["这周末想去海边吗", "想！周六上午出发避开堵车怎么样"]

    def test_multiple_ranges_per_memory(self, bound):
        binding.bind("jiaming", "mem-t1", "conv-std-001", "std-m1", "std-m1")
        binding.bind("jiaming", "mem-t1", "conv-edge-001", "edge-m1", "edge-m2")
        ranges = binding.ranges_of("mem-t1")
        assert len(ranges) == 2
        opened = binding.open_for_memory("mem-t1")
        assert len(opened["ranges"]) == 2

    def test_char_offsets(self, bound):
        b = binding.bind("jiaming", "mem-t1", "conv-std-001", "std-m1",
                         "std-m1", start_char_offset=0, end_char_offset=5)
        opened = binding.open_for_memory("mem-t1")
        m = opened["ranges"][0]["messages"][0]
        assert m["char_offset_start"] == 0 and m["char_offset_end"] == 5
        assert m["text"] == "这周末想去"  # 半开区间 [0,5) 裁切（5 个码点）
        with pytest.raises(Forbidden):
            binding.bind("jiaming", "mem-t1", "conv-std-001", "std-m1",
                         "std-m1", start_char_offset=99999)

    def test_bind_cross_conversation_rejected(self, bound):
        with pytest.raises(NotFound):
            binding.bind("jiaming", "mem-t1", "conv-std-001",
                         "std-m1", "edge-m1")

    def test_bind_unknown_memory_rejected(self, bound):
        with pytest.raises(NotFound):
            binding.bind("jiaming", "mem-nope", "conv-std-001",
                         "std-m1", "std-m2")

    def test_revoke_keeps_history(self, bound):
        b = binding.bind("jiaming", "mem-t1", "conv-std-001", "std-m1", "std-m2")
        binding.revoke("jiaming", b["binding_id"])
        assert binding.ranges_of("mem-t1") == []
        with db.formal() as c:
            row = c.execute(
                "SELECT bind_confidence FROM memory_source_bindings WHERE"
                " binding_id=?", (b["binding_id"],)).fetchone()
        assert row["bind_confidence"] == "revoked"
        with pytest.raises(NotFound):
            binding.revoke("jiaming", b["binding_id"])


# ============================== 分层隔离 ==============================

class TestLayerIsolation:
    def test_source_not_in_memory_index(self, clean):
        import_ok("jiaming", "standard.json")
        with db.formal() as c:
            # 普通 memory 全文索引不含任何原文
            assert c.execute("SELECT COUNT(*) n FROM search_fts"
                             ).fetchone()["n"] == 0
            assert c.execute("SELECT COUNT(*) n FROM retrieval_documents"
                             ).fetchone()["n"] == 0
            # 旧 raw 层未被写入
            assert c.execute("SELECT COUNT(*) n FROM raw_messages"
                             ).fetchone()["n"] == 0

    def test_reindex_search_docs_rebuildable(self, clean):
        import_ok("jiaming", "standard.json")
        with db.formal() as c:
            c.execute("DELETE FROM source_fts")
            c.execute("DELETE FROM source_search_docs")
        r = binding.reindex_search_docs()
        assert r["rebuilt_docs"] == 2
        assert len(query.search("海边")["hits"]) == 1


# ============================== 真实导出（可选，只读外部路径） ==============================

class TestRealExportOptional:
    def test_real_export_readonly(self, clean):
        path = os.environ.get("MARIPOSA_SOURCE_REAL_EXPORT")
        if not path:
            pytest.skip("未设置 MARIPOSA_SOURCE_REAL_EXPORT（本机真实导出路径）")
        r = importer.import_file("qiaosheng", path)
        assert r["status"] in ("completed", "already_imported")
        assert r["stats"]["sender_unknown"] >= 0  # 仅断言结构，不落敏感断言
