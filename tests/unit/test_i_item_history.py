import json

import pytest

from mariposa.errors import Forbidden, VersionConflict
from mariposa.capabilities import registry
from mariposa.identity_i import service as i_svc
from mariposa.memory import service as memory_svc
from mariposa.bootstrap import service as bootstrap


def test_current_i_only_exposes_history_pointer(actors):
    created = i_svc.item_create("jiaming", "我会先把事实弄清楚。")
    item = i_svc.item_get(created["item_id"])
    assert item["has_history"] is False
    i_svc.item_revise(
        "jiaming", created["item_id"], "我会先把事实和彼此感受都弄清楚。",
        expected_revision=1, change_reason="理解更完整了")

    current = i_svc.item_get(created["item_id"])
    assert current["revision"] == 2
    assert current["has_history"] is True
    assert current["history_count"] == 1
    assert current["content"] == "我会先把事实和彼此感受都弄清楚。"
    assert "我会先把事实弄清楚。" not in current.values()


def test_restore_creates_new_revision_instead_of_rewinding(actors):
    created = i_svc.item_create("jiaming", "A")
    item_id = created["item_id"]
    i_svc.item_revise("jiaming", item_id, "B", expected_revision=1)
    i_svc.item_revise("jiaming", item_id, "C", expected_revision=2)

    restored = i_svc.item_restore(
        "jiaming", item_id, restore_revision=1, expected_revision=3,
        change_reason="重新看过后仍认同最初判断")

    assert restored["revision"] == 4
    assert i_svc.item_get(item_id)["content"] == "A"
    history = i_svc.item_history(item_id)["revisions"]
    assert [x["content"] for x in history] == ["A", "B", "C", "A"]
    assert history[-1]["change_type"] == "restore"
    assert history[-1]["based_on_revision"] == 3
    assert history[-1]["restored_from_revision"] == 1


def test_revise_can_be_informed_by_old_revision(actors):
    created = i_svc.item_create("jiaming", "A")
    item_id = created["item_id"]
    i_svc.item_revise("jiaming", item_id, "B", expected_revision=1)
    out = i_svc.item_revise(
        "jiaming", item_id, "A-prime", expected_revision=2,
        informed_by_revision=1,
        change_reason="旧版方向仍对，但现在重新表述")
    assert out["revision"] == 3
    last = i_svc.item_history(item_id)["revisions"][-1]
    assert last["change_type"] == "revise"
    assert last["informed_by_revision"] == 1


def test_i_revision_can_bind_memory_relation_without_entering_recall(actors):
    mem = memory_svc.hold(
        actors["jiaming"], "一次让我重新理解自己的经历",
        categories=["milestone"], creation_mode="contemporaneous")
    created = i_svc.item_create("jiaming", "A")
    item_id = created["item_id"]
    i_svc.item_revise(
        "jiaming", item_id, "B", expected_revision=1,
        relations=[{"memory_id": mem["memory_id"],
                    "relation_type": "changed_because_of"}])

    history = i_svc.item_history(item_id)["revisions"]
    assert history[-1]["relations"][0]["memory_id"] == mem["memory_id"]
    assert history[-1]["relations"][0]["relation_type"] == "changed_because_of"


def test_only_jiaming_can_change_i_and_stale_revision_is_rejected(actors):
    with pytest.raises(Forbidden):
        i_svc.item_create("qiaosheng", "不能直接写")
    created = i_svc.item_create("jiaming", "A")
    item_id = created["item_id"]
    i_svc.item_revise("jiaming", item_id, "B", expected_revision=1)
    with pytest.raises(VersionConflict):
        i_svc.item_revise("jiaming", item_id, "C", expected_revision=1)


def test_legacy_whole_document_write_stops_after_item_mode(actors):
    assert i_svc.write("jiaming", "旧兼容主条目")["version"] == 1
    i_svc.item_create("jiaming", "新增条目")
    with pytest.raises(Forbidden) as exc:
        i_svc.write("jiaming", "不能再整篇覆盖")
    assert exc.value.code == "I_ITEM_MODE_REQUIRED"


def test_item_capabilities_expose_explicit_history_path(actors):
    created = registry.invoke(
        actors["jiaming"], "i.item.create", {"content": "A"}, None
    )["data"]
    revised = registry.invoke(
        actors["jiaming"], "i.item.revise",
        {"item_id": created["item_id"], "content": "B",
         "expected_revision": 1, "change_reason": "改得更准确"}, None
    )["data"]
    assert revised["revision"] == 2
    current = registry.invoke(
        actors["jiaming"], "i.item.get", {"item_id": created["item_id"]}, None
    )["data"]
    assert current["has_history"] is True
    history = registry.invoke(
        actors["jiaming"], "i.item.history",
        {"item_id": created["item_id"]}, None
    )["data"]
    assert [r["content"] for r in history["revisions"]] == ["A", "B"]


def test_bootstrap_injects_current_i_but_not_old_i_body(actors):
    created = i_svc.item_create("jiaming", "OLD-I-BODY-SENTINEL")
    i_svc.item_revise(
        "jiaming", created["item_id"], "CURRENT-I-BODY",
        expected_revision=1)
    section = bootstrap.get("jiaming", "cc", "cc")["i"]
    assert section["items"][0]["has_history"] is True
    # MEM-03：items 只留指针元数据；当前正文在分节 content 顶层
    assert "content" not in section["items"][0]
    assert section["content"] == "CURRENT-I-BODY"
    assert "OLD-I-BODY-SENTINEL" not in json.dumps(section, ensure_ascii=False)
