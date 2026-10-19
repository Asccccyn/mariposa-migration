"""Audit regressions using only synthetic data and disabled model providers."""
import hashlib
import json

import pytest

from mariposa import db
from mariposa.bootstrap import service as bootstrap
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, StaleOperation
from mariposa.identity import Principal
from mariposa.identity_i import service as identity_i
from mariposa.memory import service as memory, our_words
from mariposa.plans import service as plans
from mariposa.recall import judge_policy, paging, replay
from mariposa.retrieval import search, words
from mariposa.source import binding, importer, ingest


def hold(actor, **kw):
    args = dict(text="普通事件", original_title="普通标题", categories=["daily"],
                creation_mode="contemporaneous")
    args.update(kw)
    return memory.hold(actor, **args)["memory_id"]


def start(actor, term, channel="event"):
    judge_policy.update_policy("qiaosheng", expected_revision=judge_policy.get_policy()["revision"],
                               enabled=False, idempotency_key="off")
    return registry.invoke(actor, "memory.recall.start", {"query_plan": {
        "original_request": term, "lexical_terms": [term], "channels": [channel],
        "request_ref": "repair-" + channel}}, None)["data"]


def next_page(actor, packet):
    return paging.serve_page(actor, {
        "session_id": packet["recall_session_id"],
        "result_set_id": packet["pagination"]["result_set_id"],
        "cursor": packet["pagination"]["next_cursor"]})


def import_source(tmp_path, messages):
    path = tmp_path / "source.json"
    path.write_text(json.dumps([{"uuid": "room", "chat_messages": messages}]))
    importer.import_file("qiaosheng", str(path))
    with db.formal() as conn:
        rows = conn.execute("SELECT * FROM source_messages").fetchall()
    return {r["provider_message_id"]: dict(r) for r in rows}


def message(mid, parent=None):
    out = {"uuid": mid, "sender": "human", "created_at": "2026-10-01T00:00:00Z",
           "content": [{"type": "text", "text": "南瓜灯原话"}]}
    if parent:
        out["parent_message_uuid"] = parent
    return out


@pytest.mark.parametrize("mutation", ["source", "replace", "reject"])
def test_frozen_word_page_rechecks_mutations(actors, tmp_path, mutation):
    sources = import_source(tmp_path, [message("a"), message("b", "a")])
    source = sources["a"]
    ref = "source_msg:" + source["id"]
    hold(actors["jiaming"], our_words=[{
        "speaker": "qiaosheng", "text": f"南瓜灯原话{i:02d}",
        "expression_kind": "verbatim", "source_ref": ref} for i in range(11)])
    packet = start(actors["jiaming"], "南瓜灯", "words")
    frozen = paging.load_set(packet["pagination"]["result_set_id"])
    target = frozen["candidates"][-1]["resource_ref"]
    assert packet["pagination"]["next_cursor"]
    assert target not in {c["resource_ref"] for c in packet["candidates"]}
    if mutation == "source":
        our_words.correct_source("jiaming", target.split(":", 1)[1], ref,
                                 "remove_wrong_binding", expected_source_version=0)
        assert words.get_word(target.split(":", 1)[1])["evidence"][0]["evidence_kind"] == "word_unverified"
    elif mutation == "replace":
        our_words.correct_source("jiaming", target.split(":", 1)[1], ref,
                                 "replace_wrong_binding", expected_source_version=0,
                                 replacement={"source_ref": "source_msg:" + sources["b"]["id"]})
        assert words.get_word(target.split(":", 1)[1])["evidence"][0]["evidence_kind"] == "word_verbatim"
    else:
        registry.invoke(actors["jiaming"], "memory.recall.reject", {
            "session_id": packet["recall_session_id"], "resource_ref": target,
            "reject_target": "candidate", "operation_id": "reject"}, None)
    page = next_page(actors["jiaming"], packet)
    assert target not in {c["resource_ref"] for c in page["candidates"]}
    assert target in {c["resource_ref"] for c in page["invalidated"]}
    assert page["pagination"]["candidate_total"] == 11
    saved = {**packet, "candidates": frozen["candidates"]}
    assert target not in {c["resource_ref"] for c in
                          replay.revalidate_replayed("start", saved)["candidates"]}


def test_frozen_page_and_replay_recheck_plan_phase(actors):
    pid = plans.create("jiaming", "测试计划", state="active")["plan_id"]
    for _ in range(11):
        hold(actors["jiaming"], original_title="鸢尾花计划", categories=["plan"], plan_ids=[pid])
    packet = start(actors["jiaming"], "鸢尾花")
    plans.update("jiaming", pid, 1, state="done")
    with db.formal() as conn:
        assert search.search(conn, "鸢尾花")["hits"] == []
    page = next_page(actors["jiaming"], packet)
    assert page["candidates"] == []
    assert len(page["invalidated"]) == 11
    assert not page["pagination"]["has_more"]
    assert replay.revalidate_replayed("start", packet)["candidates"] == []


def test_frozen_page_rejects_old_query_after_refine(actors):
    for _ in range(11):
        hold(actors["jiaming"], text="南瓜灯")
    packet = start(actors["jiaming"], "南瓜灯")
    registry.invoke(actors["jiaming"], "memory.recall.refine", {
        "session_id": packet["recall_session_id"], "expected_revision": 1,
        "operation_id": "repair-refine", "query_plan": {
            "original_request": "新的查询", "lexical_terms": ["鸢尾花"],
            "channels": ["event"]}}, None)
    with pytest.raises(StaleOperation):
        next_page(actors["jiaming"], packet)
    with pytest.raises(StaleOperation):
        replay.revalidate_replayed("start", packet)


def assert_budget(packet):
    assert len(json.dumps({"ok": True, "data": packet}, ensure_ascii=False).encode()) <= 24576


def test_bootstrap_combined_sections_paginate_without_loss(actors):
    plan_content = "计" * 2500
    expected_plans = {plans.create("jiaming", str(i), plan_content, state="active")["plan_id"]
                      for i in range(7)}
    i_content = "\x01" * 4500
    identity_i.item_create("jiaming", i_content)
    mood = "长心情" * 5000
    expected_memories = {hold(actors["jiaming"], text="正文" * 2000,
                              mood={"tags": ["爱"], "text": mood}) for _ in range(5)}
    packet = bootstrap.get("jiaming", "estomago", "estomago")
    assert_budget(packet)
    chunks = [packet["i"]["content"]]
    cursor = packet["i"]["next_cursor"]
    while cursor:
        page = bootstrap.next_page("jiaming", "estomago", packet["snapshot_id"], cursor, "i")
        assert_budget(page)
        chunks.append(page["content"])
        cursor = page["next_cursor"]
    assert "".join(chunks) == i_content
    for section, key, expected in [("plans", "plan_id", expected_plans),
                                    ("memory_days", "memory_id", expected_memories)]:
        items = list(packet[section]["items"])
        cursor = packet[section]["next_cursor"]
        seen = set()
        while cursor and not (section == "plans" and cursor.get("plans_offset") is None):
            marker = json.dumps(cursor, sort_keys=True)
            assert marker not in seen
            seen.add(marker)
            page = bootstrap.next_page("jiaming", "estomago", packet["snapshot_id"], cursor, section)
            assert_budget(page)
            items.extend(page["items"])
            cursor = page["next_cursor"]
        assert len(items) == len(expected)
        assert {item[key] for item in items} == expected
        if section == "plans":
            for item in items:
                chunks = [item["content"]]
                cursor = item["content_next_cursor"]
                while cursor:
                    page = bootstrap.next_page("jiaming", "estomago", packet["snapshot_id"],
                                               cursor, "plan_content")
                    assert_budget(page)
                    chunks.append(page["content"])
                    cursor = page["next_cursor"]
                assert "".join(chunks) == plan_content
        if section == "memory_days":
            for item in items:
                chunks = [item["mood_text"]]
                cursor = item["mood_text_next"]
                while cursor:
                    page = bootstrap.next_page("jiaming", "estomago", packet["snapshot_id"],
                                               cursor, "memory_item")
                    assert_budget(page)
                    chunks.append(page["content"])
                    cursor = page["next_cursor"]
                assert "".join(chunks) == mood


def test_bootstrap_oversize_metadata_fails_without_empty_loop(actors):
    plans.create("jiaming", "巨" * 30000, state="active")
    packet = bootstrap.get("jiaming", "estomago", "estomago")
    assert_budget(packet)
    assert packet["plans"]["next_cursor"] == {"plans_offset": 0}
    with pytest.raises(Forbidden) as exc:
        bootstrap.next_page("jiaming", "estomago", packet["snapshot_id"],
                            packet["plans"]["next_cursor"], "plans")
    assert exc.value.code == "BOOTSTRAP_ITEM_TOO_LARGE"


def test_source_drift_is_scoped_to_conversation(actors):
    with db.formal() as conn:
        conn.execute("INSERT INTO client_bindings(binding_id,token_hash,principal_id,entry_source,created_at)"
                     " VALUES('archive',?,'worker','estomago_archive',datetime('now'))", ("x" * 64,))
    actor = Principal("worker", "worker", "agent", "estomago_archive", "archive")
    def write(room, revision):
        text = room + str(revision)
        return ingest.ingest(actor, {"operation_id": room + str(revision), "stream_id": room,
            "origin_instance": "estomago", "origin_conversation_id": room,
            "messages": [{"origin_message_id": "m1", "revision": revision,
                          "previous_revision": revision - 1 if revision > 1 else None,
                          "predecessor": None, "conversation_sequence": 1,
                          "sender": "user", "published_kind": "chat_message", "text": text,
                          "content_hash": hashlib.sha256(text.encode()).hexdigest()}]})["messages"][0]
    for room in ("a", "b"):
        ingest.create_grant("archive", room, "estomago", room, ["user"], "private", "qiaosheng")
    a = write("a", 1)
    b = write("b", 1)
    write("b", 2)
    def selected(msg):
        return binding.open_selection({"conversation_id": msg["source_conversation_id"],
            "members": [{"source_message_id": msg["source_message_id"], "content_hash": msg["content_hash"]}]})
    assert selected(a)["drifted_members"] == []
    assert selected(b)["drifted_members"] == [b["source_message_id"]]


def test_batch_references_use_members_and_parent_path(actors, tmp_path, monkeypatch):
    msgs = import_source(tmp_path, [message("a"), message("b", "a"),
                                    message("c", "b"), message("sibling", "a")])
    mid = hold(actors["jiaming"])
    cid = msgs["a"]["conversation_id"]
    pinned = binding.bind("jiaming", mid, cid, msgs["a"]["id"], msgs["c"]["id"],
                          members=[{"source_message_id": msgs[k]["id"],
                                    "content_hash": msgs[k]["content_hash"]} for k in ("a", "c")])
    regular = binding.bind("jiaming", mid, cid, msgs["a"]["id"], msgs["c"]["id"])
    with db.formal() as conn:
        # The page caller supplies its snapshot; inverse lookup must not open
        # a separate connection per message or per parent-path binding.
        with monkeypatch.context() as patch:
            def no_new_connection():
                raise AssertionError("batch inverse lookup opened another connection")
            patch.setattr(db, "formal", no_new_connection)
            refs = binding.references_for_messages([m["id"] for m in msgs.values()], conn=conn)
    assert {r["binding_id"] for r in refs[msgs["a"]["id"]]} == {pinned["binding_id"], regular["binding_id"]}
    assert {r["binding_id"] for r in refs[msgs["b"]["id"]]} == {regular["binding_id"]}
    assert refs[msgs["sibling"]["id"]] == []
    assert binding.memories_referencing("b") == refs[msgs["b"]["id"]]


def test_words_search_does_not_rebuild_unused_fts(actors, monkeypatch):
    hold(actors["jiaming"], our_words=[{"speaker": "qiaosheng", "text": "南瓜灯"}])
    def must_not_rebuild(*args):
        raise AssertionError("query attempted legacy index rebuild")
    monkeypatch.setattr(words, "ensure_index_current", must_not_rebuild)
    with db.formal() as conn:
        assert words.words_search(conn, {"lexical_terms": ["南瓜灯"]})["hits"]


@pytest.mark.parametrize("single", [True, False])
def test_selection_offsets_slice_text_excerpt_and_content(actors, tmp_path, single):
    source = [message("a"), message("b", "a"), message("c", "b")]
    for item, text in zip(source, ["起😀选中尾", "中间整条", "甲😀尾"]):
        item["content"] = [{"type": "text", "text": text},
                           {"type": "thinking", "thinking": "未选隐藏内容"}]
    msgs = import_source(tmp_path, source)
    keys = ["a"] if single else ["a", "b", "c"]
    selection = {"conversation_id": msgs["a"]["conversation_id"],
        "members": [{"source_message_id": msgs[k]["id"], "content_hash": msgs[k]["content_hash"]}
                    for k in keys], "start_char_offset": 2,
        "end_char_offset": 4 if single else 2}
    out = binding.open_selection(selection, include_content=True)
    expected = ["选中"] if single else ["选中尾", "中间整条", "甲😀"]
    assert [m["text"] for m in out["members"]] == expected
    for index in {0, len(out["members"]) - 1}:
        member = out["members"][index]
        assert member["excerpt"] == expected[index]
        content = json.loads(member["content_json"])
        assert "".join(block.get("text", "") for block in content) == expected[index]
        assert "未选隐藏内容" not in member["content_json"]
    assert out["members"][0]["char_offset_start"] == 2
    assert out["members"][-1]["char_offset_end"] == selection["end_char_offset"]
    with pytest.raises(Forbidden) as exc:
        binding.open_selection({**selection, "start_char_offset": 100}, include_content=True)
    assert exc.value.code == "SOURCE_RANGE_OFFSET"
