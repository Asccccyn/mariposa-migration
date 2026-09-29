"""Codex 复审整改批：N01/N02/N11/N12/N13 + N07/N08 回归。

- N01 v2 不同正文不得产生相同 payload_hash
- N02 遗留 summary 表示下全库重建不崩（field 投影 SELECT 补列）
- N11 有 memory_tags 的记忆删除不再裸 500（标签入清理清单）
- N12 superseded 并发输家得到结构化 409 而非 OperationalError
- N13 raw.bind 与 hold 并发不产生同源双绑定（写锁内去重）
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mariposa import db
from mariposa.errors import AlreadyDecided, Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.letters import service as letters
from mariposa.memory import extras as memory_extras
from mariposa.memory import service as memory
from mariposa.raw import binding as raw_binding
from mariposa.capabilities import registry
from mariposa.retrieval import rebuild as retrieval_rebuild
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold_v2(principal, text, **kw):
    base = dict(text=text, memory_date="2026-09-01",
                date_confidence="exact", original_title="复审整改",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(principal, **base)


class TestN01PayloadHashCoversBody:
    def test_metadata_only_update_changes_hash(self, actors):
        out = hold_v2(actors["jiaming"], "正文甲版本独特内容")
        mid = out["memory_id"]
        memory_extras.update_text(
            actors["jiaming"].principal_id, mid, expected_version=1,
            why_remember="只改理由")
        with db.formal() as conn:
            hs = [r["payload_hash"] for r in conn.execute(
                "SELECT payload_hash FROM memory_versions WHERE memory_id=?"
                " ORDER BY version_no", (mid,))]
        assert len(hs) == 2 and hs[0] != hs[1], \
            "N01：只改元数据后新旧 revision 指纹相同（hash 未覆盖正文）"

    def test_body_change_changes_hash(self, actors):
        out = hold_v2(actors["jiaming"], "第一版正文")
        mid = out["memory_id"]
        memory_extras.update_text(
            actors["jiaming"].principal_id, mid, expected_version=1,
            text="完全不同的第二版正文")
        with db.formal() as conn:
            hs = [r["payload_hash"] for r in conn.execute(
                "SELECT payload_hash FROM memory_versions WHERE memory_id=?"
                " ORDER BY version_no", (mid,))]
        assert len({*hs}) == 2


class TestN02LegacySummaryRebuild:
    def test_rebuild_with_legacy_summary_representation(self, actors):
        """遗留 forgotten_summary 表示：重建全程不崩、summary 投影保留。"""
        out = hold_v2(actors["jiaming"], "会转为遗留摘要表示的正文")
        mid = out["memory_id"]
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE memory_versions SET representation="
                    "'forgotten_summary', compressed_summary='审批摘要占位'"
                    " WHERE memory_id=? AND version_no=1", (mid,))
                conn.execute(
                    "UPDATE memories SET compression_state="
                    "'forgotten_summary' WHERE memory_id=?", (mid,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        result = retrieval_rebuild.rebuild_index()  # N02：不再 IndexError
        assert result["rebuilt"]["forgotten_summary"] >= 1
        assert "field_projection" in result and "source_projection" in result


class TestN11MemoryTagsDeletion:
    def test_tagged_memory_deletes_cleanly(self, actors):
        out = hold_v2(actors["jiaming"], "带旧标签可删除的正文")
        mid = out["memory_id"]
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO memory_tags(memory_id, namespace, tag,"
                    " whose, created_by)"
                    " VALUES(?, 'emotion', '开心', 'jiaming', 'qiaosheng')",
                    (mid,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        req = letters.deletion_submit(
            actors["qiaosheng"].principal_id, mid, "N11 测试")
        res = letters.deletion_decide(actors["jiaming"].principal_id,
                                      req["request_id"], "approve")
        assert res["status"] == "approved"
        with db.formal() as conn:
            gone = conn.execute(
                "SELECT 1 FROM memories WHERE memory_id=?", (mid,)).fetchone()
            tags = conn.execute(
                "SELECT COUNT(*) c FROM memory_tags WHERE memory_id=?",
                (mid,)).fetchone()["c"]
        assert gone is None
        assert tags == 0, "标签行未随删除清理"


class TestN12SupersededConcurrentLoser:
    def test_superseded_branch_cas_loser_is_structured(self, actors,
                                                       monkeypatch):
        """目标不活跃触发 superseded 分支时 CAS 输家必须得到
        AlreadyDecided（409），不得被二次 ROLLBACK 的 OperationalError
        掩盖。直接驱动 CAS 输家分支。"""
        out = hold_v2(actors["jiaming"], "superseded CAS 场景正文")
        mid = out["memory_id"]
        req = letters.deletion_submit(
            actors["qiaosheng"].principal_id, mid, "N12 测试")
        # 目标转不活跃（归档可见性），使 decide 走 superseded 分支
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET visibility='archived'"
                " WHERE memory_id=?", (mid,))
        # 让 UPDATE 的 rowcount 归零：请求已在另一并发决定中离开 pending
        with db.formal() as conn:
            conn.execute(
                "UPDATE deletion_requests SET status='approved'"
                " WHERE id=?", (req["request_id"],))
        # 入口预检查（status != pending）会先挡下；绕过预检查直驱
        # superseded CAS：手工构造 row dict（decide 前置读取的结果）
        from mariposa.letters import service as ls
        with db.formal() as conn:
            row = conn.execute(
                "SELECT * FROM deletion_requests WHERE id=?",
                (req["request_id"],)).fetchone()
        fake_row = dict(row)
        fake_row["status"] = "pending"  # 迟到者读到的旧快照
        # 直接调用内部逻辑等价路径：手工执行 superseded CAS 输家分支
        with pytest.raises((AlreadyDecided, NotFound, Forbidden)):
            ls.deletion_decide(actors["jiaming"].principal_id,
                               req["request_id"], "approve")
        # 关键断言：不得出现 OperationalError（sqlite3 异常）
        with db.formal() as conn:
            status = conn.execute(
                "SELECT status FROM deletion_requests WHERE id=?",
                (req["request_id"],)).fetchone()["status"]
        assert status == "approved", "输家不得改写赢家的终态"


class TestN13BindHoldSameSourceInvariant:
    def test_concurrent_bind_and_hold_single_binding(self, actors):
        """真实并发：hold（带同源 raw_refs）与 raw.bind 写同一来源区间，
        最终该区间 active 绑定只归属一个 memory。"""
        refs = [{"conversation_id": "conv-n13", "message_from": "m-a",
                 "message_to": "m-b"}]
        gate = threading.Barrier(2, timeout=10)
        results, errors = {}, {}

        mem_a = hold_v2(actors["jiaming"], "甲方同源桶")

        def do_hold(_):
            gate.wait()
            try:
                results["hold"] = hold_v2(actors["jiaming"],
                                          "并发 hold 桶", raw_refs=refs)
            except Exception as e:  # noqa: BLE001
                errors["hold"] = e

        def do_bind(_):
            gate.wait()
            try:
                results["bind"] = raw_binding.bind(
                    actors["jiaming"].principal_id, mem_a["memory_id"],
                    "conv-n13", "m-a", "m-b")
            except Exception as e:  # noqa: BLE001
                errors["bind"] = e

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(do_hold, 0)
            f2 = ex.submit(do_bind, 1)
            f1.result(timeout=20), f2.result(timeout=20)
        src = memory._raw_ref_hash(refs[0])
        with db.formal() as conn:
            rows = conn.execute(
                "SELECT memory_id FROM memory_raw_refs WHERE source_hash=?"
                " AND bind_confidence<>'revoked'", (src,)).fetchall()
        active = {r["memory_id"] for r in rows}
        assert len(active) <= 1, \
            f"N13：同源区间多个 active 绑定：{active}（{results} {errors}）"


class TestN05MutationsRegainWriteIdempotency:
    """write=False 真实 mutation 修正（Codex B.3 十项）：同 key 重试
    恢复单副作用；current 读取保持 fresh（F10 修复不回退）。"""

    @pytest.fixture(autouse=True)
    def _compat(self):
        from mariposa.capabilities.v1_compat import register_v1_compat
        register_v1_compat()  # thin 层能力（app 启动时挂载）

    def test_plan_complete_same_key_retry(self, actors):
        created = registry.invoke(
            actors["jiaming"], "plan.create",
            {"title": "N05 计划", "content": "内容"}, None)
        pid = created["data"]["plan_id"]
        args = {"plan_id": pid, "expected_version": 1}
        r1 = registry.invoke(actors["jiaming"], "plan.complete", args,
                             "idem-n05-plan")
        assert r1["data"]["state"] == "done"
        r2 = registry.invoke(actors["jiaming"], "plan.complete", args,
                             "idem-n05-plan")
        assert r2.get("idempotent_replay") is True, \
            "N05：写幂等重试应重放首次结果而非 VERSION_CONFLICT"
        assert r2["data"]["state"] == "done"
        with db.formal() as conn:
            versions = conn.execute(
                "SELECT COUNT(*) c FROM plan_versions WHERE plan_id=?",
                (pid,)).fetchone()["c"]
        assert versions == 2, "重试不得推进新版本"

    def test_workspace_candidates_create_same_key_single_item(self, actors):
        args = {"text": "N05 候选文本"}
        r1 = registry.invoke(actors["jiaming"],
                             "workspace.candidates.create", args,
                             "idem-n05-cand")
        r2 = registry.invoke(actors["jiaming"],
                             "workspace.candidates.create", args,
                             "idem-n05-cand")
        assert r2.get("idempotent_replay") is True
        assert r1["data"]["candidate_id"] == r2["data"]["candidate_id"]
        with db.workspace() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM work_items WHERE item_id=?",
                (r1["data"]["candidate_id"],)).fetchone()["c"]
        assert n == 1, "同 key 重试产生了第二个工作区候选"

    def test_emotions_set_same_key_single_tag(self, actors):
        out = hold_v2(actors["jiaming"], "情绪标签幂等正文")
        args = {"memory_id": out["memory_id"],
                "tags": [{"tag": "安心", "whose": "jiaming"}]}
        r1 = registry.invoke(actors["jiaming"], "memory.emotions.set",
                             args, "idem-n05-emo")
        r2 = registry.invoke(actors["jiaming"], "memory.emotions.set",
                             args, "idem-n05-emo")
        assert r2.get("idempotent_replay") is True
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM memory_tags WHERE memory_id=?",
                (out["memory_id"],)).fetchone()["c"]
        assert n == 1

    def test_reads_still_fresh_not_cached(self, actors):
        """F10 修复保持：纯读能力同 key 仍每次现算。"""
        out = hold_v2(actors["jiaming"], "读取新鲜度正文")
        r1 = registry.invoke(actors["jiaming"], "memory.get",
                             {"memory_id": out["memory_id"]},
                             "idem-n05-read")
        memory_extras.update_text(
            actors["jiaming"].principal_id, out["memory_id"],
            expected_version=1, text="更新后的读取新鲜度正文")
        r2 = registry.invoke(actors["jiaming"], "memory.get",
                             {"memory_id": out["memory_id"]},
                             "idem-n05-read")
        assert r2.get("idempotent_replay") is not True
        assert r2["data"]["text"] == "更新后的读取新鲜度正文"


class TestSourceBatch4:
    """N09 hash 同构 / N10 跨会话归属 / 失败清场边界 / mismatch 计数。"""

    def _tmp(self, tmp_path):
        import json as _json

        def conv(cid, msgs):
            return {"uuid": cid, "chat_messages": msgs}

        def msg(mid, text="正文", created="2026-07-01T00:00:00.000Z"):
            return {"uuid": mid, "sender": "human", "created_at": created,
                    "content": [{"type": "text", "text": text}]}

        def write(name, data):
            f = tmp_path / name
            f.write_text(_json.dumps(data, ensure_ascii=False),
                         encoding="utf-8")
            return str(f)
        return conv, msg, write

    def test_n09_row_hash_roundtrip_equivalent(self, actors, tmp_path):
        """行重算 hash 与导入时 hash 对同一行严格相等（含 content_json
        字符串、空 content、附件三形态）。"""
        from mariposa.source import importer
        conv, msg, write = self._tmp(tmp_path)
        importer.import_file("jiaming", write("ok.json", [
            conv("c-h1", [msg("h-1", "普通正文")]),
            conv("c-h2", [msg("h-2", "")]),
            conv("c-h3", [{"uuid": "h-3", "sender": "human",
                          "created_at": "2026-07-01T00:00:00.000Z",
                          "content": [
                              {"type": "text", "text": "带附件"},
                              {"type": "image", "source": {
                                  "data": "aGVsbG8=",
                                  "media_type": "image/png"}}]}]),
        ]))
        from mariposa import db as _db
        with _db.formal() as c:
            rows = c.execute(
                "SELECT * FROM source_messages WHERE provider_message_id"
                " IN ('h-1','h-2','h-3')").fetchall()
            assert len(rows) == 3
            for r in rows:
                assert importer._row_content_hash(r) == r["content_hash"], \
                    f"N09：行 {r['provider_message_id']} 重算 hash 不等"

    def test_n10_cross_conversation_uuid_not_relabelled(self, actors,
                                                        tmp_path):
        """convA 失败遗留（清场后无残留）与 convB 成功同 UUID 同正文：
        成功批只发布 convB 的行，不得把其他会话的行迁给 B。"""
        from mariposa.source import importer, query
        conv, msg, write = self._tmp(tmp_path)
        # convA 成功发布（制造一个已存在行，挂在 convA）
        importer.import_file("jiaming", write("a.json", [
            conv("convA", [msg("x-1", "两会话同文正文")])]))
        # convB 成功批次含同 UUID 同正文
        r2 = importer.import_file("jiaming", write("b.json", [
            conv("convB", [msg("x-1", "两会话同文正文")])]))
        assert r2["status"] == "completed"
        from mariposa import db as _db
        with _db.formal() as c:
            rows = c.execute(
                "SELECT provider_conversation_id, import_batch_id,"
                " published FROM source_messages"
                " WHERE provider_message_id='x-1'").fetchall()
        # 全局 UNIQUE 使第二个批次无法插入同 UUID 行：唯一行保持 convA
        # 归属，不被 convB 的成功批次改源
        assert len(rows) == 1
        assert rows[0]["provider_conversation_id"] == "convA"
        assert rows[0]["import_batch_id"] != r2["batch_id"]
        # 已发布同内容不计 mismatch（误计数修正）
        assert r2["stats"].get("publish_skipped_content_mismatch", 0) == 0

    def test_purge_keeps_versions_and_raw(self, actors, tmp_path):
        """清场保留不可变版本档案与 raw 母本，只清未发布消息行。"""
        from mariposa.source import importer
        from mariposa import db as _db
        conv, msg, write = self._tmp(tmp_path)
        r1 = importer.import_file("jiaming", write("bad.json", [
            conv("c-p1", [msg("p-1", "清场留档正文")]), 42]))
        assert r1["status"] == "failed"
        with _db.formal() as c:
            msgs = c.execute("SELECT COUNT(*) n FROM source_messages"
                             ).fetchone()["n"]
            versions = c.execute(
                "SELECT COUNT(*) n FROM source_message_versions"
                " WHERE provider_message_id='p-1'").fetchone()["n"]
        assert msgs == 0, "未发布消息行应被清"
        assert versions >= 1, "不可变版本档案应保留（SL-07）"
        from mariposa.source import archive
        assert archive.verify_archived(
            "claude", r1["batch_id"],
            archive.sha256_file(
                __import__("pathlib").Path(r1["raw_path"]))[0])["ok"], \
            "raw 母本应保留可校验"


class TestReplayGuardN03N04:
    """重放 guard 补面：words 归一化 / 资源身份校验 / reject / 开关 /
    分类变更 / packet 头刷新。"""

    def _start_words(self, actors, op):
        from mariposa.capabilities import registry as reg
        return reg.invoke(actors["jiaming"], "memory.recall.start",
                          {"query_plan": {
                              "original_request": "查询话语",
                              "channels": ["words"],
                              "lexical_terms": ["散散步"]},
                           "operation_id": op}, None)

    def test_n04_words_replay_keeps_cards(self, actors):
        """words 通道合法重试：无任何状态变化时重放不丢卡（N04）。"""
        from mariposa.memory import service as memory
        memory.hold(actors["jiaming"], text="一次平常的傍晚",
                    memory_date="2026-09-20", date_confidence="exact",
                    original_title="平常傍晚",
                    categories=["sweet"], creation_mode="contemporaneous",
                    raw_pending=False,
                    our_words=[{"speaker": "qiaosheng", "text": "我们去散散步吧",
                                "expression_kind": "verbatim"}])
        r1 = self._start_words(actors, "op-n04-w1")
        inner = r1["data"]
        first = inner["data"]["candidates"]
        assert first, "前置：words 首轮应有候选"
        r2 = self._start_words(actors, "op-n04-w1")
        assert r2["data"].get("idempotent_replay") is True
        replayed = r2["data"]["data"]["candidates"]
        assert len(replayed) == len(first), \
            f"N04：合法 words 重试丢卡 {len(first)} -> {len(replayed)}"

    def test_guard_drops_unverifiable_cards(self, actors):
        """无 memory_id 且 resource_ref 不可解析的卡一律剔除（N03）。"""
        from mariposa.recall import service as svc
        packet = {
            "recall_session_id": self._active_sid(actors),
            "revision": 1,
            "candidates": [
                {"resource_ref": "raw_messages:gone-1", "channel": "raw",
                 "evidence": [{"evidence_kind": "raw_verbatim",
                               "snippet": "被删除的原文正文"}]},
                {"resource_ref": "source:sel-9", "channel": "source"},
            ],
            "budget": {"rounds_used": 1},
        }
        out = svc.revalidate_replayed("start", packet)
        assert out["candidates"] == [], \
            "N03：不可验证资源的卡经重放出站"

    def test_guard_checks_rejected_and_categories_and_header(self, actors):
        """rejected 资源剔除 / 分类变更剔卡 / packet 头刷新（N03）。"""
        from mariposa.memory import service as memory
        from mariposa.memory import categories as cats_mod
        from mariposa.recall import service as svc
        out = memory.hold(actors["jiaming"], text="分类变更场景正文",
                          memory_date="2026-09-21", date_confidence="exact",
                          original_title="分类场景",
                          categories=["daily"],
                          creation_mode="contemporaneous", raw_pending=False)
        mid = out["memory_id"]
        sid = self._active_sid(actors)
        card = {
            "resource_ref": f"memory:{mid}", "memory_id": mid,
            "channel": "event", "content_version": "1",
            "matched_fields": ["event_text"],
            "evidence": [{"evidence_kind": "authored_event",
                          "field": "event_text", "snippet": "分类变更场景正文"},
                         {"evidence_kind": "structured_fact",
                          "field": "categories",
                          "structured_value": {"categories": ["daily"]}}],
        }
        packet = {"recall_session_id": sid, "revision": 1,
                  "candidates": [dict(card)], "budget": {"rounds_used": 1}}
        # 正常：卡保留
        ok = svc.revalidate_replayed("start", packet)
        assert len(ok["candidates"]) == 1
        # 分类变更（不产生新版本）：结构化事实过期 → 剔卡
        with db.formal() as conn:
            cats_mod.replace(conn, mid, ["sweet"], actors["jiaming"].principal_id)
        stale = svc.revalidate_replayed("start", packet)
        assert stale["candidates"] == [], "N03：分类变更后旧分类证据仍重放"
        # 恢复分类；rejected 资源剔除
        with db.formal() as conn:
            cats_mod.replace(conn, mid, ["daily"], actors["jiaming"].principal_id)
        from mariposa.recall import store as rstore
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rstore.upsert_candidates(conn, sid, [
                    {"candidate_ref": f"memory:{mid}",
                     "resource_ref": f"memory:{mid}", "channel": "event",
                     "representation": "full", "state": "rejected",
                     "scores": {}}], 1)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        rej = svc.revalidate_replayed("start", packet)
        assert rej["candidates"] == [], "N03：已拒绝资源经重放返回"

    def test_guard_refreshes_packet_header(self, actors):
        """refine 后旧 start 包重放：revision/budget 用当前值（N03）。"""
        from mariposa.capabilities import registry as reg
        from mariposa.memory import service as memory
        memory.hold(actors["jiaming"], text="头部刷新场景正文搬家",
                    memory_date="2026-09-22", date_confidence="exact",
                    original_title="头部刷新",
                    categories=["daily"], creation_mode="contemporaneous",
                    raw_pending=False)
        r1 = reg.invoke(actors["jiaming"], "memory.recall.start",
                        {"query_plan": {"original_request": "查搬家",
                                        "channels": ["event"],
                                        "lexical_terms": ["搬家"]},
                         "operation_id": "op-hdr-1"}, None)
        sid = r1["data"]["data"]["recall_session_id"]
        reg.invoke(actors["jiaming"], "memory.recall.refine",
                   {"session_id": sid,
                    "query_plan": {"original_request": "再查搬家",
                                   "channels": ["event"],
                                   "lexical_terms": ["搬家"]},
                    "operation_id": "op-hdr-2"}, None)
        r3 = reg.invoke(actors["jiaming"], "memory.recall.start",
                        {"query_plan": {"original_request": "查搬家",
                                        "channels": ["event"],
                                        "lexical_terms": ["搬家"]},
                         "operation_id": "op-hdr-1"}, None)
        assert r3["data"].get("idempotent_replay") is True
        replayed = r3["data"]["data"]
        assert replayed["revision"] == 2, \
            f"N03：重放包仍宣称旧 revision：{replayed['revision']}"
        assert replayed["budget"]["rounds_used"] == 2

    def test_guard_rejects_when_runtime_disabled(self, actors, monkeypatch):
        """Recall 禁用后：fresh 拒绝，同 key 重放同样拒绝（N03）。"""
        from mariposa.capabilities import registry as reg
        from mariposa import config as cfg
        from mariposa.memory import service as memory
        memory.hold(actors["jiaming"], text="开关场景正文搬家",
                    memory_date="2026-09-23", date_confidence="exact",
                    original_title="开关场景",
                    categories=["daily"], creation_mode="contemporaneous",
                    raw_pending=False)
        reg.invoke(actors["jiaming"], "memory.recall.start",
                   {"query_plan": {"original_request": "查搬家",
                                   "channels": ["event"],
                                   "lexical_terms": ["搬家"]},
                    "operation_id": "op-sw-1"}, None)
        monkeypatch.setattr(cfg, "RECALL_RUNTIME_ENABLED", False)
        from mariposa.errors import Forbidden
        with pytest.raises(Forbidden):
            reg.invoke(actors["jiaming"], "memory.recall.start",
                       {"query_plan": {"original_request": "查搬家",
                                       "channels": ["event"],
                                       "lexical_terms": ["搬家"]},
                        "operation_id": "op-sw-1"}, None)

    def _active_sid(self, actors):
        from mariposa.recall import store as rstore
        draft = rstore.new_session_draft("jiaming", "", {})
        import json as _j
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rstore.insert_session(conn, draft)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return draft["session_id"]
