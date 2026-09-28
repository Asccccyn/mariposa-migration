"""Source Layer 定点补修测试（复核 v1.1；SL-01..SL-11 新语义）。

本文件先于修复编写，用于复现《复核证据 v1.1》的全部缺口；全部为合成
数据，不读真实聊天，不触碰生产库（conftest 保险丝隔离临时根）。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from mariposa import config, db
from mariposa.errors import Forbidden, MariposaError, NotFound
from mariposa.source import archive, binding, importer, json_stream, query


# ---------------------------------------------------------------- helpers

def conv(cid: str, msgs: list[dict], name: str = "合成会话") -> dict:
    return {"uuid": cid, "name": name,
            "created_at": "2026-07-01T00:00:00.000Z",
            "updated_at": "2026-07-01T01:00:00.000Z",
            "chat_messages": msgs}


def msg(mid: str, sender: str = "human", text: str = "正文",
        parent: str | None = None, content: list | None = None,
        created: str = "2026-07-01T00:00:30.000Z") -> dict:
    m: dict = {"uuid": mid, "sender": sender, "created_at": created}
    if parent is not None:
        m["parent_message_uuid"] = parent
    if content is None:
        m["content"] = [{"type": "text", "text": text}]
    else:
        m["content"] = content
        if text:
            m["text"] = text
    return m


def write(tmp_path: Path, name: str, payload) -> str:
    p = tmp_path / name
    if isinstance(payload, bytes):
        p.write_bytes(payload)
    else:
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(p)


def imp(path: str, pid: str = "jiaming") -> dict:
    return importer.import_file(pid, path)


def must_fail_import(path: str, code: str | None = None) -> MariposaError:
    with pytest.raises(MariposaError) as ei:
        imp(path)
    if code:
        assert ei.value.code == code, f"expect {code}, got {ei.value.code}"
    return ei.value


# ================= SL-09 严格 JSON 文法 =================

class TestStrictJson:
    def test_trailing_comma_rejected(self, actors, tmp_path):
        p = write(tmp_path, "tc.json",
                  '[{"uuid":"c1","chat_messages":[]},]')
        must_fail_import(p, "SOURCE_BAD_JSON")
        with db.formal() as c:
            assert c.execute("SELECT status FROM source_import_batches"
                             " ORDER BY import_started_at DESC"
                             ).fetchone()["status"] == "failed"

    def test_trailing_garbage_rejected(self, actors, tmp_path):
        raw = json.dumps([conv("c1", [])]) + " }]"
        p = write(tmp_path, "tg.json", raw.encode("utf-8"))
        must_fail_import(p, "SOURCE_BAD_JSON")

    def test_invalid_utf8_rejected(self, actors, tmp_path):
        raw = b'[{"uuid":"c1","chat_messages":[],"name":"\xff\xfe"}]'
        p = write(tmp_path, "bad.bin", raw)
        must_fail_import(p, "SOURCE_BAD_JSON")  # 不得替换 U+FFFD 后成功

    def test_bom_allowed(self, actors, tmp_path):
        raw = b"\xef\xbb\xbf" + json.dumps(
            [conv("c1", [msg("m1")])], ensure_ascii=False).encode("utf-8")
        r = imp(write(tmp_path, "bom.json", raw))
        assert r["status"] == "completed"

    def test_empty_array_is_valid_zero_conversations(self, actors, tmp_path):
        r = imp(write(tmp_path, "e.json", []))
        assert r["status"] == "completed"
        assert r["stats"]["conversations_total"] == 0

    def test_nonfinite_numbers_rejected(self, actors, tmp_path):
        raw = '[{"uuid":"c1","chat_messages":[],"score":NaN}]'
        p = write(tmp_path, "nan.json", raw.encode("utf-8"))
        must_fail_import(p, "SOURCE_BAD_JSON")

    def test_stream_unit_level(self, tmp_path):
        """流式层直接验证：三种非法输入必须抛 JsonStreamError。"""
        cases = [
            b'[{"a":1},]',
            b'[{"a":1}] trailing',
            b'[{"a":"\xff"}]',
            b'[{"a":1}, {"b":NaN}]',
        ]
        for i, raw in enumerate(cases):
            p = tmp_path / f"s{i}.json"
            p.write_bytes(raw)
            with open(p, "rb") as f:
                with pytest.raises(json_stream.JsonStreamError):
                    list(json_stream.iter_top_level_array(f))


# ================= SL-01 completed 门禁 =================

class TestCompletedGate:
    def test_parse_failure_blocks_completed(self, actors, tmp_path):
        data = [conv("c-ok", [msg("ok-1")]), 42]
        r = imp(write(tmp_path, "mixed.json", data))
        assert r["status"] == "failed"
        assert r["stats"]["parse_failures"] >= 1
        with db.formal() as c:
            row = c.execute("SELECT status, error FROM source_import_batches"
                            " ORDER BY import_started_at DESC").fetchone()
        assert row["status"] == "failed" and row["error"]

    def test_verify_problem_blocks_completed(self, actors, tmp_path,
                                             monkeypatch):
        monkeypatch.setattr(
            importer, "_verify_integrity",
            lambda provider, batch_id: {"ok": False, "problems": [
                {"check": "synthetic_failed_check"}], "sample_checks": {}})
        r = imp(write(tmp_path, "v.json", [conv("c1", [msg("m1")])]))
        assert r["status"] == "failed"
        assert any(p["check"] == "synthetic_failed_check"
                   for p in r["stats"]["verify_problems"])

    def test_bad_file_retry_structured(self, actors, tmp_path):
        p = write(tmp_path, "bad.json", b'[{"uuid": ')
        must_fail_import(p)
        # 同一坏文件再导：仍是结构化失败，不是未处理异常
        must_fail_import(p)


# ================= SL-02 解析对象 = 已归档字节 =================

class TestArchivedParseSource:
    def test_parse_reads_archived_payload(self, actors, tmp_path, monkeypatch):
        opened = []
        real_open = importer.open_element_stream

        def spy(path):
            opened.append(str(path))
            return real_open(path)

        monkeypatch.setattr(importer, "open_element_stream", spy)
        r = imp(write(tmp_path, "a.json", [conv("c1", [msg("m1", text="A")])]))
        assert r["status"] == "completed"
        # 检测+解析阶段所有打开都必须指向受控文件（归档/暂存），绝不再开原始路径
        src_abs = str(Path(tmp_path, "a.json").resolve())
        assert src_abs not in opened, "不得重新打开可变原始路径解析"
        assert all("source" + "/" + "raw" in p or "incoming" in p
                   or ".part" in p for p in opened), opened

    def test_input_mutated_after_archive_ignored(self, actors, tmp_path,
                                                 monkeypatch):
        p = Path(tmp_path) / "race.json"
        p.write_text(json.dumps(
            [conv("c1", [msg("m1", text="ARCHIVED_BODY")])]), encoding="utf-8")
        real_publish = archive.publish_snapshot

        def publish_then_mutate(*a, **kw):
            out = real_publish(*a, **kw)
            p.write_text(json.dumps(  # 归档后、解析前改写原路径（TOCTOU）
                [conv("c1", [msg("m1", text="CHANGED_AFTER_ARCHIVE")])]),
                encoding="utf-8")
            return out

        monkeypatch.setattr(archive, "publish_snapshot", publish_then_mutate)
        r = imp(str(p))
        assert r["status"] == "completed"
        with db.formal() as c:
            text = c.execute("SELECT text FROM source_messages"
                             ).fetchone()["text"]
        assert text == "ARCHIVED_BODY"

    def test_manifest_pins_payload_path(self, actors, tmp_path):
        r = imp(write(tmp_path, "原始名 conversations.json",
                      [conv("c1", [msg("m1")])]))
        d = Path(r["raw_path"]).parent
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["payload"] == Path(r["raw_path"]).name
        # 内部文件名不得使用原始文件名（隔离注入面）
        assert Path(r["raw_path"]).name.startswith("payload-")
        # 归档校验按 manifest 精确路径
        sha, _ = archive.sha256_file(Path(r["raw_path"]))
        assert archive.verify_archived("claude", r["batch_id"], sha)["ok"]

    def test_ambiguous_zip_rejected(self, actors, tmp_path):
        z = tmp_path / "dup.zip"
        with zipfile.ZipFile(z, "w") as zf:
            body = json.dumps([conv("c1", [msg("m1")])])
            zf.writestr("a/conversations.json", body)
            zf.writestr("b/conversations.json", body)
        must_fail_import(str(z), "SOURCE_AMBIGUOUS_ARCHIVE")


# ================= SL-10 失败批次默认不可见 =================

class TestFailureVisibility:
    def test_failed_batch_messages_unsearchable(self, actors, tmp_path,
                                                monkeypatch):
        data = [conv("c-a", [msg("fa-1", text="失败前已提交正文")]),
                conv("c-b", [msg("fb-1", text="第二批会触发崩溃")])]
        p = write(tmp_path, "two.json", data)
        real = importer._insert_message

        def crash_on(conn, provider, conv_row_id, batch_id, m, chash):
            if m.provider_message_id == "fb-1":
                raise RuntimeError("simulated crash")
            return real(conn, provider, conv_row_id, batch_id, m, chash)

        monkeypatch.setattr(importer, "_insert_message", crash_on)
        with pytest.raises(RuntimeError):
            imp(p)
        # c-a 已提交入库，但批次失败 → 默认检索/列表不可见
        with db.formal() as c:
            assert c.execute("SELECT COUNT(*) n FROM source_messages"
                             ).fetchone()["n"] == 1
            assert c.execute("SELECT published FROM source_messages"
                             ).fetchone()["published"] == 0
        assert query.search("失败前已提交正文")["hits"] == []
        assert query.conversations_list()["conversations"] == []

        # 重导恢复：同文件 → completed → 发布 → 可见
        monkeypatch.undo()
        r = imp(p)
        assert r["status"] == "completed"
        assert len(query.search("失败前已提交正文")["hits"]) == 1
        with db.formal() as c:
            assert c.execute("SELECT COUNT(*) n FROM source_messages"
                             " WHERE published=1").fetchone()["n"] == 2

    def test_old_batch_not_hidden_by_new_failure(self, actors, tmp_path,
                                                 monkeypatch):
        imp(write(tmp_path, "good.json",
                  [conv("c-keep", [msg("keep-1", text="旧批成功正文")])]))
        assert len(query.search("旧批成功正文")["hits"]) == 1
        # 新批次失败不得隐藏旧成功批次
        p2 = write(tmp_path, "bad2.json",
                   [conv("c-x", [msg("x-1")]), 42])
        r = imp(p2)
        assert r["status"] == "failed"
        assert len(query.search("旧批成功正文")["hits"]) == 1


# ================= SL-11 speaker NULL 必须检出 =================

class TestSpeakerVerify:
    def test_null_human_speaker_detected(self, actors, tmp_path):
        r0 = imp(write(tmp_path, "v.json", [conv("c1", [msg("m1")])]))
        with db.formal() as c:
            c.execute("UPDATE source_messages SET speaker=NULL"
                      " WHERE normalized_sender='human' AND import_batch_id=?",
                      (r0["batch_id"],))
        r = importer._verify_integrity("claude", r0["batch_id"])
        assert any(p["check"] == "speaker_mapping" for p in r["problems"])

    def test_verify_checks_projection_hash_sample(self, actors, tmp_path):
        r0 = imp(write(tmp_path, "v.json", [conv("c1", [msg("m1", text="正文X")])]))
        with db.formal() as c:
            c.execute("UPDATE source_search_docs SET text_hash='deadbeef'"
                      " WHERE message_id IN (SELECT id FROM source_messages"
                      " WHERE import_batch_id=?)", (r0["batch_id"],))
        r = importer._verify_integrity("claude", r0["batch_id"])
        assert any(p["check"] == "projection_hash" for p in r["problems"])


# ================= SL-03 excerpt 只出自正文 =================

class TestExcerptFromBody:
    @pytest.fixture()
    def probe(self, actors, tmp_path):
        data = [conv("c-ex", [msg(
            "ex-1", sender="assistant", text="",
            content=[{"type": "thinking",
                      "thinking": "alpha beta SYNTHETIC_THINKING_ONLY"},
                     {"type": "text", "text": "alpha,beta"}],
            created="2026-07-01T00:01:00.000Z")])]
        imp(write(tmp_path, "ex.json", data))
        return None

    def test_excerpt_never_from_thinking(self, probe):
        res = query.search("alpha beta")
        assert len(res["hits"]) == 1
        h = res["hits"][0]
        assert h["text"] == "alpha,beta"
        assert "SYNTHETIC_THINKING_ONLY" not in h["excerpt"]
        assert "," in h["excerpt"] or "alpha" in h["excerpt"]

    def test_punctuation_normalization_maps_back(self, probe):
        # 归一化命中必须映射回原字符串安全上下文，而不是退回证据
        res = query.search("alpha beta")
        assert res["hits"][0]["matched_fields"] == ["text"]

    def test_evidence_search_only_with_explicit_senders(self, probe):
        res = query.search("SYNTHETIC", senders=["system", "tool", "unknown"])
        assert res["hits"] == []  # assistant 的 thinking 不因传 system 就混入


# ================= SL-04 顶层 text 回退条件 =================

class TestTopLevelTextFallback:
    def _import_one(self, actors, tmp_path, content, top_text=None,
                    sender="human"):
        m = msg("t-1", sender=sender, text=top_text or "", content=content)
        if not top_text:
            m.pop("text", None)
        imp(write(tmp_path, "t.json", [conv("c-t", [m])]))
        with db.formal() as c:
            row = c.execute("SELECT text, has_tool_content, has_thinking"
                            " FROM source_messages").fetchone()
        return dict(row)

    def test_tool_only_content_top_text_not_body(self, actors, tmp_path):
        row = self._import_one(
            actors, tmp_path,
            [{"type": "tool_result",
              "content": [{"type": "text", "text": "工具块"}]}],
            top_text="SYNTHETIC_TOOL_OUTPUT")
        assert row["text"] == "" and row["has_tool_content"] == 1

    def test_thinking_only_content_top_text_not_body(self, actors, tmp_path):
        row = self._import_one(
            actors, tmp_path,
            [{"type": "thinking", "thinking": "只思考"}],
            top_text="TOP_TEXT_WITH_THINKING", sender="assistant")
        assert row["text"] == "" and row["has_thinking"] == 1

    def test_missing_content_top_text_is_body(self, actors, tmp_path):
        row = self._import_one(actors, tmp_path, [], top_text="纯顶层正文")
        assert row["text"] == "纯顶层正文"

    def test_string_content_is_body(self, actors, tmp_path):
        m = {"uuid": "t-1", "sender": "human",
             "created_at": "2026-07-01T00:00:30.000Z",
             "content": "字符串 content 也是正文"}
        imp(write(tmp_path, "s.json", [conv("c-s", [m])]))
        with db.formal() as c:
            assert c.execute("SELECT text FROM source_messages"
                             ).fetchone()["text"] == "字符串 content 也是正文"


# ================= SL-05 偏移语义与片段裁切 =================

class TestOffsetSemantics:
    @pytest.fixture()
    def ten(self, actors, tmp_path):
        data = [conv("c-off", [
            msg("off-1", text="0123456789"),
            msg("off-2", sender="assistant", text="第二条消息",
                parent="off-1"),
        ])]
        imp(write(tmp_path, "off.json", data))
        with db.formal() as c:
            c.execute("INSERT INTO memories(memory_id, current_version_no,"
                      " created_at, updated_at) VALUES('mem-fx', 1,"
                      " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
        return None

    def test_half_open_slice(self, ten):
        binding.bind("jiaming", "mem-fx", "c-off", "off-1", "off-1",
                     start_char_offset=2, end_char_offset=4)
        opened = binding.open_for_memory("mem-fx")
        m = opened["ranges"][0]["messages"][0]
        assert m["text"] == "23"
        assert m.get("excerpt", "23") == "23"

    def test_content_json_sliced_no_full_body_leak(self, ten):
        ro = query.open_range("c-off", "off-1", "off-1",
                              start_char_offset=2, end_char_offset=4,
                              include_content=True)
        m = ro["messages"][0]
        assert m["content_json"] is not None
        assert "0123456789" not in m["content_json"]
        assert "23" in m["content_json"]

    def test_reverse_offset_rejected(self, ten):
        with pytest.raises(Forbidden):
            binding.bind("jiaming", "mem-fx", "c-off", "off-1", "off-1",
                         start_char_offset=8, end_char_offset=2)

    def test_non_integer_offset_rejected(self, ten):
        for bad in (1.9, "3", True):
            with pytest.raises(Forbidden):
                binding.bind("jiaming", "mem-fx", "c-off", "off-1", "off-1",
                             start_char_offset=bad, end_char_offset=4)

    def test_emoji_offsets_are_codepoints(self, actors, tmp_path):
        data = [conv("c-emoji", [msg("e-1", text="😀ab😀cd")])]
        imp(write(tmp_path, "e.json", data))
        with db.formal() as c:
            c.execute("INSERT INTO memories(memory_id, current_version_no,"
                      " created_at, updated_at) VALUES('mem-emoji', 1,"
                      " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
        binding.bind("jiaming", "mem-emoji", "c-emoji", "e-1", "e-1",
                     start_char_offset=1, end_char_offset=3)
        opened = binding.open_for_memory("mem-emoji")
        # Python code point 口径：😀=1 个 code point → [1,3) = "ab"
        assert opened["ranges"][0]["messages"][0]["text"] == "ab"

    def test_multi_message_range_slices_ends(self, ten):
        ro = query.open_range("c-off", "off-1", "off-2",
                              start_char_offset=2, end_char_offset=2)
        texts = [m["text"] for m in ro["messages"]]
        assert texts == ["23456789", "第二"]  # 首条[2:]，尾条[:2]


# ================= SL-06 分支路径验证 =================

class TestSiblingRanges:
    @pytest.fixture()
    def tree(self, actors, tmp_path):
        data = [conv("c-tree", [
            msg("t-u0", text="U0"),
            msg("t-a1", sender="assistant", text="A1", parent="t-u0"),
            msg("t-a2", sender="assistant", text="A2", parent="t-u0"),
            msg("t-u3", text="U3", parent="t-a2"),
        ])]
        imp(write(tmp_path, "tree.json", data))
        return None

    def test_sibling_range_rejected(self, tree):
        with pytest.raises(Forbidden) as ei:
            query.open_range("c-tree", "t-a1", "t-u3")
        assert ei.value.code == "SOURCE_RANGE_NOT_PATH"

    def test_valid_path_excludes_sibling(self, tree):
        ro = query.open_range("c-tree", "t-u0", "t-u3")
        ids = [m["provider_message_id"] for m in ro["messages"]]
        assert ids == ["t-u0", "t-a2", "t-u3"]
        sib = [m for m in ro.get("off_path_messages", [])]
        assert {m["provider_message_id"] for m in sib} == {"t-a1"}
        assert all(m.get("sibling_branch") for m in sib)

    def test_binding_rejects_sibling(self, tree):
        with db.formal() as c:
            c.execute("INSERT INTO memories(memory_id, current_version_no,"
                      " created_at, updated_at) VALUES('mem-tree', 1,"
                      " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
        with pytest.raises(Forbidden):
            binding.bind("jiaming", "mem-tree", "c-tree", "t-a1", "t-u3")


# ================= SL-07 消息版本与快照 =================

class TestMessageVersions:
    def test_same_uuid_new_content_keeps_versions(self, actors, tmp_path):
        imp(write(tmp_path, "v1.json",
                  [conv("c-ver", [msg("ver-1", text="0123456789")])]))
        r2 = imp(write(tmp_path, "v2.json",
                       [conv("c-ver", [msg("ver-1", text="UPDATED ORIGINAL")])]))
        assert r2["status"] == "completed"
        assert r2["stats"]["version_conflicts"] == 1
        # 当前行不被覆盖；两个观察版本都保留
        with db.formal() as c:
            assert c.execute("SELECT text FROM source_messages"
                             ).fetchone()["text"] == "0123456789"
            n = c.execute("SELECT COUNT(*) n FROM source_message_versions"
                          " WHERE provider_message_id='ver-1'"
                          ).fetchone()["n"]
        assert n == 2

    def test_same_uuid_same_content_idempotent(self, actors, tmp_path):
        imp(write(tmp_path, "i1.json",
                  [conv("c-idem", [msg("idem-1", text="同内容")])]))
        r2 = imp(write(tmp_path, "i2.json",
                       [conv("c-idem", [msg("idem-1", text="同内容")])]))
        assert r2["stats"]["version_conflicts"] == 0
        with db.formal() as c:
            assert c.execute("SELECT COUNT(*) n FROM source_message_versions"
                             ).fetchone()["n"] == 1

    def test_reordered_export_sequences_in_snapshots(self, actors, tmp_path):
        imp(write(tmp_path, "o1.json", [conv("c-seq", [
            msg("o-0", text="首条"),
            msg("o-1", text="第二条", parent="o-0"),
        ])]))
        # 新导出在中间插入了 o-2：数组序与旧快照冲突
        r2 = imp(write(tmp_path, "o2.json", [conv("c-seq", [
            msg("o-0", text="首条"),
            msg("o-2", text="插入消息", parent="o-0"),
            msg("o-1", text="第二条", parent="o-0"),
        ])]))
        assert r2["status"] == "completed"
        assert r2["stats"]["sequence_conflicts"] >= 1
        # 两个快照各自记录自己的成员与序号；父关系保留旧值不静默改写
        with db.formal() as c:
            snaps = c.execute(
                "SELECT snapshot_id FROM source_conversation_snapshots"
                " WHERE conversation_id=(SELECT id FROM source_conversations"
                " WHERE provider_conversation_id='c-seq')"
            ).fetchall()
            assert len(snaps) == 2
            parent = c.execute(
                "SELECT parent_provider_message_id FROM source_messages"
                " WHERE provider_message_id='o-1'").fetchone()
            assert parent["parent_provider_message_id"] == "o-0"
            members = c.execute(
                "SELECT provider_message_id, sequence FROM"
                " source_snapshot_members m JOIN source_conversation_snapshots"
                " s ON s.snapshot_id=m.snapshot_id WHERE s.batch_id=?"
                " ORDER BY m.sequence",
                (r2["batch_id"],)).fetchall()
        assert [(m["provider_message_id"], m["sequence"]) for m in members] == \
            [("o-0", 0), ("o-2", 1), ("o-1", 2)]

    def test_conversation_metadata_observed_per_snapshot(self, actors,
                                                         tmp_path):
        imp(write(tmp_path, "m1.json", [conv("c-meta", [msg("mm-1")],
                                             name="旧标题")]))
        r2 = imp(write(tmp_path, "m2.json", [conv("c-meta", [msg("mm-1")],
                                                  name="新标题")]))
        with db.formal() as c:
            titles = [r["title"] for r in c.execute(
                "SELECT s.title FROM source_conversation_snapshots s"
                " JOIN source_conversations c2 ON c2.id=s.conversation_id"
                " WHERE c2.provider_conversation_id='c-meta'"
                " ORDER BY s.created_at, s.snapshot_id")]
        assert titles == ["旧标题", "新标题"]
        assert r2["stats"]["version_conflicts"] == 0  # 元数据观察不算内容冲突


# ================= SL-08 检索过滤与分页 =================

class TestFilteredSearch:
    @pytest.fixture()
    def noisy(self, actors, tmp_path):
        convs = [conv(f"c-noise-{i}", [msg(f"n-{i}", text="needle 干扰")])
                 for i in range(90)]
        convs.append(conv("c-target", [msg("tgt-1", text="auditneedle 目标")]))
        imp(write(tmp_path, "noise.json", convs))
        return None

    def test_many_noise_does_not_mask_target(self, noisy):
        res = query.search("auditneedle", conversation_id="c-target")
        assert len(res["hits"]) == 1
        assert res["hits"][0]["provider_conversation_id"] == "c-target"

    def test_sender_and_date_filters_without_keyword(self, actors, tmp_path):
        imp(write(tmp_path, "d.json", [
            conv("c-d1", [msg("d-1", text="白天话",
                              created="2026-07-01T02:00:00.000Z")]),
            conv("c-d2", [msg("d-2", sender="assistant", text="夜里话",
                              created="2026-07-02T18:00:00.000Z")]),
        ]))
        # 02:00Z→上海 10:00 = 7/1；18:00Z→上海 7/3 凌晨 2 点
        res = query.search(None, date_from="2026-07-03", date_to="2026-07-03",
                           senders=["assistant"])
        assert [h["provider_message_id"] for h in res["hits"]] == ["d-2"]

    def test_pagination_covers_all_filtered(self, actors, tmp_path):
        convs = [conv(f"c-p{i}", [msg(f"p-{i}", text=f"分页命中{i}号")])
                 for i in range(30)]
        imp(write(tmp_path, "p.json", convs))
        seen: list[str] = []
        offset = 0
        while True:
            res = query.search("分页命中", limit=10, offset=offset)
            seen += [h["provider_message_id"] for h in res["hits"]]
            if not res["has_more"]:
                break
            offset += 10
        assert len(seen) == 30 and len(set(seen)) == 30

    def test_like_escape_literals(self, actors, tmp_path):
        data = [conv("c-lk", [
            msg("lk-1", text="正文"),
            msg("lk-2", sender="unknown", text="",
                content=[{"type": "text", "text": "字面 100% 百分号"}]),
            msg("lk-3", sender="unknown", text="",
                content=[{"type": "text", "text": "字面 a_b 下划线"}]),
            msg("lk-4", sender="unknown", text="",
                content=[{"type": "text", "text": "字面反斜杠\\x"}]),
        ])]
        imp(write(tmp_path, "lk.json", data))
        ev = {"senders": ["system", "tool", "unknown"]}
        assert len(query.search("100%", **ev)["hits"]) == 1
        assert len(query.search("a_b", **ev)["hits"]) == 1
        assert len(query.search("\\x", **ev)["hits"]) == 1
        assert query.search("1000%", **ev)["hits"] == []  # % 不是通配


# ================= 资源硬限额 =================

class TestResourceLimits:
    def test_oversized_element_rejected(self, actors, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SOURCE_MAX_ELEMENT_BYTES", 1024)
        data = [conv("c-big", [msg("big-1", text="x" * 4096)])]
        must_fail_import(write(tmp_path, "big.json", data),
                         "SOURCE_ELEMENT_TOO_LARGE")

    def test_oversized_message_text_rejected(self, actors, tmp_path,
                                             monkeypatch):
        monkeypatch.setattr(config, "SOURCE_MAX_MESSAGE_TEXT_BYTES", 64)
        data = [conv("c-mt", [msg("mt-1", text="y" * 128)])]
        r = imp(write(tmp_path, "mt.json", data))
        assert r["status"] == "failed" and r["stats"]["parse_failures"] >= 1

    def test_range_message_cap(self, actors, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SOURCE_RANGE_MAX_MESSAGES", 3)
        data = [conv("c-cap", [msg(f"cap-{i}", text=f"上限{i}",
                                   parent=f"cap-{i-1}" if i else None)
                               for i in range(5)])]
        imp(write(tmp_path, "cap.json", data))
        with pytest.raises(Forbidden) as ei:
            query.open_range("c-cap", "cap-0", "cap-4")
        assert ei.value.code == "SOURCE_RANGE_TOO_LARGE"

    def test_zip_member_limits(self, actors, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SOURCE_MAX_ZIP_MEMBERS", 3)
        z = tmp_path / "many.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for i in range(5):
                zf.writestr(f"f{i}.txt", "x")
            zf.writestr("conversations.json",
                        json.dumps([conv("c-z", [msg("z-1")])]))
        must_fail_import(str(z), "SOURCE_ARCHIVE_LIMIT")


# ================= 并发认领与租约 =================

class TestImportClaim:
    def test_fresh_running_batch_conflicts(self, actors, tmp_path):
        p = write(tmp_path, "cc.json", [conv("c-cc", [msg("cc-1")])])
        r1 = imp(p)
        # 人为把批次拨回 running（模拟另一进程正在处理同文件）
        with db.formal() as c:
            c.execute("UPDATE source_import_batches SET status='running',"
                      " import_started_at=datetime('now')"
                      " WHERE batch_id=?", (r1["batch_id"],))
        with pytest.raises(MariposaError) as ei:
            imp(write(tmp_path, "cc2.json",
                      [conv("c-cc", [msg("cc-1")])]) )
        # cc2 同内容同 sha → 认领冲突
        assert ei.value.code == "SOURCE_IMPORT_IN_PROGRESS"

    def test_stale_running_batch_taken_over(self, actors, tmp_path):
        p = write(tmp_path, "st.json", [conv("c-st", [msg("st-1")])])
        r1 = imp(p)
        with db.formal() as c:
            c.execute("UPDATE source_import_batches SET status='running',"
                      " import_started_at=datetime('now', '-3 hours')"
                      " WHERE batch_id=?", (r1["batch_id"],))
        r2 = imp(write(tmp_path, "st2.json", [conv("c-st", [msg("st-1")])]))
        assert r2["status"] == "completed"
        assert r2["batch_id"] == r1["batch_id"]


# ================= 绑定版本固定 =================

class TestBindingVersionPinned:
    def test_binding_records_content_hash(self, actors, tmp_path):
        imp(write(tmp_path, "bp.json",
                  [conv("c-bp", [msg("bp-1", text="版本正文")])]))
        with db.formal() as c:
            c.execute("INSERT INTO memories(memory_id, current_version_no,"
                      " created_at, updated_at) VALUES('mem-bp', 1,"
                      " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
        b = binding.bind("jiaming", "mem-bp", "c-bp", "bp-1", "bp-1")
        assert b["start_content_hash"] and b["end_content_hash"]
        # 后续导入新版本不改变绑定读取（当前版本行不更新）
        imp(write(tmp_path, "bp2.json",
                  [conv("c-bp", [msg("bp-1", text="改后的版本正文")])]))
        opened = binding.open_for_memory("mem-bp")
        assert opened["ranges"][0]["messages"][0]["text"] == "版本正文"
