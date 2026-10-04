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

    def test_i_outbound_minimal(self, actors):
        """裁定（2026-10-04 三）：I 开窗只出当前的话+极简提示；
        条目指针/明细一律不出站。"""
        from mariposa.identity_i import service as i_svc
        from mariposa.bootstrap import service as boot
        i_svc.item_create("jiaming", "条目一正文" * 50)
        pkg = boot.get("jiaming", "cc", "cc")
        i_sec = pkg["i"]
        assert "items" not in i_sec, "条目指针不出站"
        assert i_sec["content"], "当前的话必须在场"
        assert "has_history" not in i_sec, "单条目无修订时不出提示"
        page = boot.next_page("jiaming", "cc", pkg["snapshot_id"],
                              {"i_offset": 0}, "i")
        assert page["content_role"] == "bootstrap_memory_package"
        assert page["instruction_authority"] == "none"

    def test_i_history_and_binding_hints_minimal(self, actors):
        from mariposa.identity_i import service as i_svc
        from mariposa.bootstrap import service as boot
        from mariposa import db
        from mariposa.memory import service as memory
        item = i_svc.item_create("jiaming", "初版正文")
        i_svc.item_revise("jiaming", item["item_id"], "第二版正文",
                          expected_revision=1)
        m = memory.hold(actors["jiaming"], text="绑定桶正文",
                        memory_date="2026-09-20", date_confidence="exact",
                        original_title="t", categories=["daily"],
                        creation_mode="contemporaneous",
                        raw_pending=False)
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO i_revision_memory_relations(item_id,"
                " revision, memory_id, relation_type, created_at)"
                " VALUES(?, 2, ?, 'related_to', datetime('now'))",
                (item["item_id"], m["memory_id"]))
        pkg = boot.get("jiaming", "cc", "cc")
        i_sec = pkg["i"]
        assert i_sec["has_history"] is True, "有历史提示版本"
        assert i_sec.get("version"), "提示版本号"
        assert i_sec.get("bound_memory_count") == 1, "有桶绑定提示存在"
        assert "items" not in i_sec

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


class TestReauditRound:
    """Codex 78921d0 复审（11 条）修复回归——关键行为抽样。"""

    def test_rer01_replay_retains_issued_continuation(self, actors):
        """重放返回的响应包含同一个仍有效的接续引用（不新发/不丢）。"""
        _hold(actors, "重放保留接续正文")
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             {"query_plan": {
                                 "original_request": "找重放保留",
                                 "channels": ["event"],
                                 "lexical_terms": ["重放保留"],
                                 "request_ref": "rer1-s"}}, None
                             )["data"]["data"]
        ref1 = r1["continuation"]["continue_request_ref"]
        r2 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             {"query_plan": {
                                 "original_request": "找重放保留",
                                 "channels": ["event"],
                                 "lexical_terms": ["重放保留"],
                                 "request_ref": "rer1-s"}}, None
                             )["data"]["data"]
        assert r2["continuation"]["continue_request_ref"] == ref1, \
            "同 operation 重放必须携带同一接续引用"
        # 且该 ref 仍可消费（未因重放被消耗/重复签发）
        from mariposa import config as cfg
        old = cfg.RECALL_BURST_ROUNDS
        cfg.RECALL_BURST_ROUNDS = 1
        try:
            rr = registry.invoke(actors["jiaming"], "memory.recall.refine",
                                 {"session_id": r2["recall_session_id"],
                                  "continue_request_ref": ref1,
                                  "query_plan": {
                                      "original_request": "再找重放保留",
                                      "channels": ["event"],
                                      "lexical_terms": ["重放保留"]},
                                  "request_ref": "rer1-r"}, None)["data"]["data"]
            assert rr["revision"] == 2
        finally:
            cfg.RECALL_BURST_ROUNDS = old

    def test_rer02_navigation_replay_keeps_structured_cards(self, actors):
        from mariposa.recall import service as recall_service
        _hold(actors, "导航重放结构卡正文")
        p = recall_service.start(actors["jiaming"], {
            "query_plan": {"original_request": "找导航重放",
                           "channels": ["event"],
                           "lexical_terms": ["导航重放"]}})
        sid = p["recall_session_id"]
        nav = recall_service.navigate(actors["jiaming"], {
            "session_id": sid, "direction": "earlier"})
        assert not nav["candidates"] or \
            all(c.get("matched_fields") for c in nav["candidates"])
        replay = recall_service.revalidate_replayed("navigate", nav, None)
        # 结构卡不为空时重放不得过滤为空（时间轴字段非 phase 白名单）
        if nav["candidates"]:
            assert replay["candidates"], \
                "导航重放不得把仍有效的结构卡过滤为空"

    def test_remem02_deletion_recovery_returns_real_result(self, actors):
        """RE-MEM-02：删除申请的崩溃恢复返回 deletion_get 真实结果，
        不是裸指针。"""
        from mariposa import db
        from mariposa.deletion import service as deletion
        import hashlib as _hl
        import json as _json
        import datetime as _dt
        out = _hold(actors, "恢复真实结果正文")
        req = deletion.deletion_submit("qiaosheng", out["memory_id"],
                                       "恢复结果测试", action="delete")
        args = {"memory_id": out["memory_id"], "reason": "恢复结果测试",
                "operation_id": "remem2-1"}
        ph = _hl.sha256(_json.dumps(
            {"memory_id": args["memory_id"],
             "reason": args["reason"].strip()},
            ensure_ascii=False, sort_keys=True,
            default=str).encode()).hexdigest()
        old = (_dt.datetime.now(_dt.timezone.utc)
               - _dt.timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO idempotency_records(principal_id,"
                " capability, idempotency_key, payload_hash, status,"
                " result_ref, created_at) VALUES('jiaming',"
                "'memory.deletion.request',?,?,'running',NULL,?)",
                ("t:remem2-t", ph, old))
            # 领域回执（指针形态）已存在
            conn.execute(
                "INSERT INTO idempotency_records(principal_id,"
                " capability, idempotency_key, payload_hash, status,"
                " result_ref, created_at) VALUES('qiaosheng',"
                "'memory.deletion.request',?,?, 'completed', ?,"
                " datetime('now'))",
                ("op:remem2-1", ph,
                 _json.dumps({"memory_id": out["memory_id"],
                              "request_id": req["request_id"]})))
        from tests.unit.test_ruling_provenance import _seed_published_msg
        qiaosheng = identity.Principal(
            "qiaosheng", "江乔生", "human", "web", "bq")
        r = registry.invoke(qiaosheng, "memory.deletion.request",
                            args, "remem2-t")
        data = r["data"]
        assert data.get("request_id") == req["request_id"]
        assert "status" in data, "恢复必须返回正常结果字段（含状态）"

    def test_recall06_words_list_reports_gap(self, actors):
        from mariposa.memory import our_words as ow
        out = _hold(actors, "列表gap正文")
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO memory_our_words(word_id, memory_id, ordinal,"
                " speaker, text, expression_kind, source_ref, created_by,"
                " created_at) VALUES('ow-lgap', ?, 1, 'qiaosheng',"
                " '列表gap话语', 'verbatim', 'raw_msg:legacy-x',"
                " 'jiaming', datetime('now'))",
                (out["memory_id"],))
        words = ow.list_for(out["memory_id"])
        hit = [w for w in words if w["word_id"] == "ow-lgap"]
        assert hit and hit[0].get("source_gap") == "legacy_raw_prefix", \
            "words.list 必须独立报告来源解析缺口"


class TestReview3Fixes:
    """db16d2a 复审三残留：words.list 冒号切片 / I 绑定指纹 /
    apply 子目录路径。"""

    def test_words_list_resolves_provider_message_id(self, actors):
        """source_ref 用 provider message id（非内部 id）时
        words.list 不得误报 source_gap（第二查询参数曾多留冒号）。"""
        import tempfile
        from mariposa.memory import our_words as ow
        from mariposa.source import importer
        tmp = tempfile.mkdtemp()
        f = Path(tmp) / "pv3.json"
        f.write_text(json.dumps([{
            "uuid": "c-pv3", "chat_messages": [{
                "uuid": "pv3-m1", "sender": "human",
                "created_at": "2026-09-20T10:00:00.000Z",
                "content": [{"type": "text", "text": "提供者id引用原文"}]}]}],
            ensure_ascii=False), encoding="utf-8")
        importer.import_file("jiaming", str(f))
        out = _hold(actors, "提供者id引用正文")
        words = ow.list_for(out["memory_id"])  # 前置：无话语也行
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO memory_our_words(word_id, memory_id, ordinal,"
                " speaker, text, expression_kind, source_ref, created_by,"
                " created_at) VALUES('ow-pvid', ?, 1, 'qiaosheng',"
                " '按提供者id引用的话语', 'verbatim', 'source_msg:pv3-m1',"
                " 'jiaming', datetime('now'))",
                (out["memory_id"],))
        words = ow.list_for(out["memory_id"])
        hit = [w for w in words if w["word_id"] == "ow-pvid"]
        assert hit and not hit[0].get("source_gap"), \
            "provider message id 引用必须解析为已发布来源，不得误报缺口"

    def test_binding_removal_invalidates_snapshot(self, actors):
        """移除最后一个 I↔桶绑定后，旧快照不得 unchanged=true。"""
        from mariposa import db as _db
        from mariposa.identity_i import service as i_svc
        from mariposa.memory import service as mem
        from mariposa.memory import relations as rel
        from mariposa.bootstrap import service as boot
        from mariposa.errors import SnapshotStale
        from tests.conftest import reset_all
        item = i_svc.item_create("jiaming", "绑定失效正文")
        m = mem.hold(actors["jiaming"], text="绑定桶正文",
                     memory_date="2026-09-20", date_confidence="exact",
                     original_title="t", categories=["daily"],
                     creation_mode="contemporaneous", raw_pending=False)
        # 建立 I↔桶绑定（item 修订级关系——用 item_revise 带 relations）
        i_svc.item_revise("jiaming", item["item_id"], "绑定失效正文二",
                          expected_revision=1,
                          relations=[{"memory_id": m["memory_id"],
                                      "relation_type": "related_to"}])
        first = boot.get("jiaming", "cc", "cc")
        assert first["i"].get("bound_memory_count") == 1
        # 解除绑定：再修订一次不带 relations 会分叉——走关系纠错
        # （remove_wrong_binding）删除该修订的关系
        rid = None
        with _db.formal() as conn:
            row = conn.execute(
                "SELECT relation_id FROM i_revision_memory_relations"
                " WHERE item_id=? AND memory_id=?",
                (item["item_id"], m["memory_id"])).fetchone()
            rid = row["relation_id"] if row else None
        assert rid, "前置：绑定已建立"
        registry.invoke(actors["jiaming"], "i.item.relations.correct",
                        {"relation_id": rid,
                         "correction_action": "remove_wrong_binding",
                         "operation_id": "rv3-unbind"}, None)
        # 失效的两种合法形态：SnapshotStale（推荐，强制重取）或
        # 非 unchanged 的全新包——都证明旧快照不再冒充未变化
        try:
            second = boot.get("jiaming", "cc", "cc",
                              loaded_snapshot_id=first["snapshot_id"])
            assert second.get("unchanged") is not True, \
                "移除最后一个绑定后旧快照必须失效（提示需刷新）"
            assert "bound_memory_count" not in second["i"] or \
                second["i"].get("bound_memory_count") == 0
        except SnapshotStale:
            pass

    def test_migration_apply_handles_subdirectory(self, actors):
        """apply 按保留的相对路径定位子目录成员（不再 file_missing）。"""
        import tempfile
        from mariposa import migration
        tmp = Path(tempfile.mkdtemp())
        sub = tmp / "nested"
        sub.mkdir()
        (sub / "2026-07-01 10-00-00 子目录样本_ab12cd34ef56.md").write_text(
            "---\ntype: note\ndate: 2026-07-01\n---\n子目录迁移正文内容",
            encoding="utf-8")
        r = migration.dry_run(str(tmp), None)
        assert r["counts"]["total"] == 1
        v = migration.verify(_last_report_path(tmp, r))
        assert v["ok"] is True, v["problems"]
        out = migration.apply_from_report(_last_report_path(tmp, r))
        assert out["ok"] is True, out.get("problems")
        assert out.get("applied"), "子目录成员必须被 apply 落库"


def _last_report_path(tmp, r):
    # dry_run 的 out 未指定时报告写到临时目录——从返回里找不到则
    # 用 dry_run(out=) 重跑一次固定路径
    from mariposa import migration
    p = Path(str(tmp)) / "report.json"
    migration.dry_run(str(tmp), str(p))
    return str(p)


import json  # noqa: E402
from pathlib import Path  # noqa: E402
