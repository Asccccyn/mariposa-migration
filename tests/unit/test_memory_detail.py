"""memory.detail.read：按桶全量详情快照、分页、只读副作用合同（docs/05）。"""
from __future__ import annotations

import json

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.source import importer
from mariposa.surfacing import store as surface_store
from tests.conftest import reset_all

#: 长正文：含 CJK、emoji、Unicode 增补面字符（𝄞 U+1D11E，4 字节 UTF-8）
LONG_TEXT = ("7月9日我们把记忆库建成了。" + "那天的细节很多，值得慢慢写下来。" * 1500
             + "还有一个音乐符号𝄞和表情🌃收尾。")


@pytest.fixture()
def actors(tmp_path):
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "宿主用户", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "char", "agent",
                                      "claude_chat", "bj"),
    }


def _seed(tmp_path, actors):
    jm = actors["jiaming"]
    held = memory.hold(jm, text=LONG_TEXT, memory_date="2026-07-09",
                       date_confidence="exact", original_title="7.9 记忆库",
                       categories=["sweet"], creation_mode="contemporaneous",
                       raw_pending=False)
    other_held = memory.hold(jm, text="另一件事", memory_date="2026-08-19",
                             date_confidence="exact", original_title="8.19",
                             categories=["sweet"],
                             creation_mode="contemporaneous",
                             raw_pending=False)
    mem_id = held["memory_id"]
    other = other_held["memory_id"]
    from mariposa.memory import our_words
    our_words.append("jiaming", mem_id, [
        {"speaker": "jiaming", "text": "我们做到了"},
        {"speaker": "qiaosheng", "text": "嗯，辛苦了"}])
    with db.formal() as conn:
        conn.execute(
            "INSERT INTO memory_recollections(recollection_id, memory_id,"
            " author, text, view_receipt, supersedes, version, written_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("rc_1", mem_id, "jiaming", "后来想起这天", None, None, 1,
             "2026-07-10T00:00:00Z"))
        conn.execute(
            "INSERT INTO memory_keeps(mark_id, memory_id, owner,"
            " recollection_id, recollection_version, created_at)"
            " VALUES(?,?,?,?,?,?)",
            ("kp_1", mem_id, "jiaming", "rc_1", 1, "2026-07-11T00:00:00Z"))
        conn.execute(
            "INSERT INTO memory_relations(relation_id, from_memory,"
            " to_memory, relation_type, custom_label, reverse_label,"
            " created_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("rel_test_1", mem_id, other, "related_to", None, None,
             "jiaming", "2026-07-12T00:00:00Z"))
    # 原文层 + 绑定
    path = tmp_path / "detail-source.json"
    msgs = [{"uuid": f"dm{i}", "sender": "human",
             "created_at": "2026-07-09T12:00:00Z",
             "parent_message_uuid": f"dm{i-1}" if i else None,
             "content": [{"type": "text", "text": f"原文消息{i}"}]}
            for i in range(4)]
    path.write_text(json.dumps(
        [{"uuid": "dconv", "name": "Detail source", "chat_messages": msgs}]),
        encoding="utf-8")
    assert importer.import_file("jiaming", str(path))["status"] == "completed"
    registry.invoke(jm, "source.binding.bind", {
        "memory_id": mem_id, "conversation_id": "dconv",
        "start_message_id": "dm0", "end_message_id": "dm3"}, None)
    return mem_id, other


def _receipt_with_detail(mem_id, text, version="1"):
    import hashlib
    item = {"event_id": "ev_1", "memory_id": mem_id,
            "memory_version": version, "domain_versions": {},
            "memory_date": "2026-07-09", "date_confidence": "exact",
            "event_text": text,
            "body_hash": hashlib.sha256(
                text.encode()).hexdigest(),
            "detail_ref": "dref_test"}
    surface_store.save_validation_receipt(
        "run_d", "room-1", "tm", 1, "w1", "h", [item], 1, "issued", 3600)
    return item


def _read(jm, ref="dref_test", cursor=None):
    args = {"detail_ref": ref}
    if cursor:
        args["cursor"] = cursor
    return registry.invoke(jm, "memory.detail.read", args, None)["data"]


class TestDetailRead:
    def test_full_snapshot_all_sections_and_binding_text(self, actors,
                                                         tmp_path):
        mem_id, _ = _seed(tmp_path, actors)
        _receipt_with_detail(mem_id, LONG_TEXT)
        page = _read(actors["jiaming"])
        assert page["page_index"] == 0
        assert page["memory_id"] == mem_id
        assert page["changed_since_surface"] is True  # 浮现版本 1≠当前
        # 段齐全：跨页收集，末页给出全段终态
        pages, cursor = [page], page["next_cursor"]
        while cursor:
            p2 = _read(actors["jiaming"], cursor=cursor)
            pages.append(p2)
            cursor = p2["next_cursor"]
        st = pages[-1]["section_status"]
        for s in ("memory", "categories", "mood", "our_words", "source",
                  "recollections", "keeps", "relations"):
            assert s in st, s
        assert st["memory"] == "complete"

    def test_pagination_reassembles_text_lossless(self, actors, tmp_path):
        mem_id, _ = _seed(tmp_path, actors)
        _receipt_with_detail(mem_id, LONG_TEXT)
        pages = []
        cursor = None
        for _ in range(50):
            page = _read(actors["jiaming"], cursor=cursor)
            pages.append(page)
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        assert pages[-1]["complete"] is True
        # 正文跨页原样拼回（码点半开区间，无丢重，含增补面字符）
        text = ""
        for p in pages:
            mem = p["sections"].get("memory")
            if mem and "text" in mem:
                text += mem["text"]
        assert text == LONG_TEXT

    def test_no_view_receipt_and_no_explicit_open(self, actors, tmp_path):
        mem_id, _ = _seed(tmp_path, actors)
        _receipt_with_detail(mem_id, LONG_TEXT)
        before = _explicit_open(mem_id)
        _read(actors["jiaming"])
        _read(actors["jiaming"], cursor="p1")
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) n FROM memory_view_receipts").fetchone()["n"]
            after = conn.execute(
                "SELECT last_explicit_open_at FROM memories WHERE"
                " memory_id=?", (mem_id,)).fetchone()[0]
        assert n == 0                    # 不建 view_receipt（不借 open 链）
        assert after == before           # 不确认明确打开/续期

    def test_snapshot_stale_when_memory_changes_mid_pagination(
            self, actors, tmp_path):
        mem_id, _ = _seed(tmp_path, actors)
        _receipt_with_detail(mem_id, LONG_TEXT)
        first = _read(actors["jiaming"])
        assert first["next_cursor"]
        cur = registry.invoke(actors["jiaming"], "memory.get",
                              {"memory_id": mem_id}, None)["data"]
        registry.invoke(actors["jiaming"], "memory.update", {
            "memory_id": mem_id, "text": LONG_TEXT + "（改）",
            "expected_version": cur["version"]}, None)
        with pytest.raises(Exception) as ei:
            _read(actors["jiaming"], cursor=first["next_cursor"])
        assert "DETAIL_SNAPSHOT_STALE" in str(ei.value)

    def test_detail_not_found_after_delete(self, actors, tmp_path):
        mem_id, _ = _seed(tmp_path, actors)
        item = _receipt_with_detail(mem_id, LONG_TEXT)
        with db.formal() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            for t in ("memory_our_words", "memory_recollections",
                      "memory_keeps", "memory_source_bindings",
                      "memory_categories", "memory_versions",
                      "retrieval_documents", "field_search_docs",
                      "memories"):
                conn.execute(f"DELETE FROM {t} WHERE memory_id=?",
                             (mem_id,))
            conn.execute(
                "DELETE FROM memory_relations WHERE from_memory=?"
                " OR to_memory=?", (mem_id, mem_id))
            conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(Exception) as ei:
            _read(actors["jiaming"])
        assert "DETAIL_NOT_FOUND" in str(ei.value)

    def test_detail_never_recalls_or_judges(self, actors, tmp_path, monkeypatch):
        """docs/05 接线合同：detail 路径 recall/judge/向量/BM25 全零（spy）。"""
        mem_id, _ = _seed(tmp_path, actors)
        _receipt_with_detail(mem_id, LONG_TEXT)
        calls = {"n": 0}

        def _boom(*a, **kw):
            calls["n"] += 1
            raise AssertionError("detail 路径不得触发检索/判断")

        import mariposa.recall.service as recall_svc
        import mariposa.recall.round2 as round2_mod
        import mariposa.retrieval.semantic as sem_mod
        import mariposa.recall.pipeline as pipe_mod
        import mariposa.recall.judge_policy as jp_mod
        for fn in ("start", "refine", "navigate", "accept", "reject"):
            monkeypatch.setattr(recall_svc, fn, _boom, raising=True)
        monkeypatch.setattr(round2_mod, "round2", _boom, raising=True)
        monkeypatch.setattr(sem_mod, "semantic_search", _boom, raising=True)
        monkeypatch.setattr(pipe_mod, "round1_lexical_hits", _boom, raising=True)
        # judge 政策读可以（只读），但 judge provider 构造/调用必须零
        import mariposa.surfacing.judge as sj
        monkeypatch.setattr(sj, "set_transport_for_tests",
                            lambda fn: _boom(), raising=True)
        page = _read(actors["jiaming"])
        assert page["memory_id"] == mem_id
        assert calls["n"] == 0

    def test_unseen_ref_rejected(self, actors, tmp_path):
        _seed(tmp_path, actors)
        with pytest.raises(Exception) as ei:
            _read(actors["jiaming"], ref="dref_never_issued")
        assert "DETAIL_REF_NOT_SEEN" in str(ei.value)


def _explicit_open(mem_id):
    with db.formal() as conn:
        return conn.execute(
            "SELECT last_explicit_open_at FROM memories WHERE memory_id=?",
            (mem_id,)).fetchone()[0]
