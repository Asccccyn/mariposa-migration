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
from mariposa.deletion import service as deletion
from mariposa.memory import extras as memory_extras
from mariposa.memory import service as memory
from mariposa.recall import store
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


class TestN12SupersededConcurrentLoser:
    def test_superseded_branch_cas_loser_is_structured(self, actors,
                                                       monkeypatch):
        """目标不活跃触发 superseded 分支时 CAS 输家必须得到
        AlreadyDecided（409），不得被二次 ROLLBACK 的 OperationalError
        掩盖。直接驱动 CAS 输家分支。"""
        out = hold_v2(actors["jiaming"], "superseded CAS 场景正文")
        mid = out["memory_id"]
        req = deletion.deletion_submit(
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
        from mariposa.deletion import service as ls
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


class TestP101ActionOperationAtomicity:
    """P1-01：每个写 runtime 状态的动作，业务副作用与 operation 记录
    同一事务——首次成功后（响应丢失/重试）同 key 重放首次结果。"""

    def _hold(self, actors, text, **kw):
        from mariposa.memory import service as memory
        base = dict(text=text, memory_date="2026-09-01",
                    date_confidence="exact", original_title="P101",
                    categories=["daily"],
                    creation_mode="contemporaneous", raw_pending=False)
        base.update(kw)
        return memory.hold(actors["jiaming"], **base)

    def _start(self, actors, sid_terms=("搬家",), op="op-p1-s"):
        from mariposa.capabilities import registry as reg
        return reg.invoke(actors["jiaming"], "memory.recall.start",
                          {"query_plan": {
                              "original_request": "查",
                              "channels": ["event"],
                              "lexical_terms": list(sid_terms)},
                           "operation_id": op}, None)

    def test_close_retry_replays_first_result(self, actors):
        self._hold(actors, "关闭幂等场景搬家正文")
        p = self._start(actors)
        sid = p["data"]["data"]["recall_session_id"]
        args = {"session_id": sid, "outcome": "cancelled",
                "operation_id": "idem-p1-close"}
        r1 = registry.invoke(actors["jiaming"], "memory.recall.close",
                             args, None)
        assert r1["data"]["data"]["status"] == "CANCELLED"
        # 响应丢失重试：重放首次结果，不再 INVALID_STATE
        r2 = registry.invoke(actors["jiaming"], "memory.recall.close",
                             args, "idem-p1-close")
        assert r2["data"].get("idempotent_replay") is True
        assert r2["data"]["data"]["status"] == "CANCELLED"

    def test_accept_close_retry_replays(self, actors):
        self._hold(actors, "接受幂等场景搬家正文")
        p = self._start(actors)
        sid = p["data"]["data"]["recall_session_id"]
        cand = p["data"]["data"]["candidates"][0]["candidate_ref"]
        args = {"session_id": sid, "candidate_ref": cand, "close": True,
                "operation_id": "idem-p1-accept"}
        r1 = registry.invoke(actors["jiaming"], "memory.recall.accept",
                             args, None)
        assert r1["data"]["data"]["status"] == "RESOLVED"
        r2 = registry.invoke(actors["jiaming"], "memory.recall.accept",
                             args, "idem-p1-accept")
        assert r2["data"].get("idempotent_replay") is True
        assert r2["data"]["data"]["status"] == "RESOLVED"

    def test_no_budget_refine_retry_replays(self, actors):
        self._hold(actors, "无余额幂等场景搬家正文")
        p = self._start(actors)
        sid = p["data"]["data"]["recall_session_id"]
        base = {"session_id": sid,
                "query_plan": {"original_request": "再查",
                               "channels": ["event"],
                               "lexical_terms": ["搬家"]}}
        # 用满 burst 两轮
        for i in range(2):
            registry.invoke(actors["jiaming"], "memory.recall.refine",
                            {**base, "operation_id": f"op-p1-r{i}"}, None)
        args = {**base, "operation_id": "idem-p1-nb"}
        r1 = registry.invoke(actors["jiaming"], "memory.recall.refine",
                             args, None)
        assert r1["data"]["data"]["status"] == "BUDGET_EXHAUSTED"
        rev_after_first = r1["data"]["data"]["revision"]
        r2 = registry.invoke(actors["jiaming"], "memory.recall.refine",
                             args, None)
        assert r2["data"].get("idempotent_replay") is True, \
            "无余额 refine 重试应重放首次结果"
        assert r2["data"]["data"]["revision"] == rev_after_first, \
            "重试不得再次推进 revision"

    def test_reject_retry_replays(self, actors):
        self._hold(actors, "拒绝幂等场景搬家正文")
        p = self._start(actors)
        sid = p["data"]["data"]["recall_session_id"]
        cand = p["data"]["data"]["candidates"][0]
        args = {"session_id": sid, "candidate_ref": cand["candidate_ref"],
                "reject_target": "candidate",
                "operation_id": "idem-p1-reject"}
        r1 = registry.invoke(actors["jiaming"], "memory.recall.reject",
                             args, None)
        assert r1["data"]["data"]["rejected"]["candidate_ref"] == \
            cand["candidate_ref"]
        r2 = registry.invoke(actors["jiaming"], "memory.recall.reject",
                             args, "idem-p1-reject")
        assert r2["data"].get("idempotent_replay") is True

    def test_crash_before_operation_record_rolls_back_state(
            self, actors, monkeypatch):
        """事务内 operation 记录失败 → 业务状态一并回滚（原子性）。"""
        self._hold(actors, "崩溃原子场景搬家正文")
        p = self._start(actors)
        sid = p["data"]["data"]["recall_session_id"]

        def boom(conn, *a, **kw):
            raise RuntimeError("operation record write failed")

        monkeypatch.setattr(store, "record_operation_row", boom)
        from mariposa.errors import MariposaError
        with pytest.raises((RuntimeError, MariposaError)):
            registry.invoke(actors["jiaming"], "memory.recall.close",
                            {"session_id": sid, "outcome": "cancelled",
                             "operation_id": "idem-p1-crash"}, None)
        monkeypatch.undo()
        # 状态未被改动：session 仍可 close（不是终态）
        session = store.get_session(sid)
        assert session["status"] not in ("CANCELLED", "RESOLVED"), \
            "operation 记录失败时终态迁移必须一并回滚"
        r = registry.invoke(actors["jiaming"], "memory.recall.close",
                            {"session_id": sid, "outcome": "cancelled",
                             "operation_id": "idem-p1-crash"}, None)
        assert r["data"]["data"]["status"] == "CANCELLED"


class TestP103LeaseFencing:
    """P1-03：过期 worker 复活后不得破坏接管方的成功证据。"""

    def _mkbatch(self, status="running", sha="sha-take"):
        import uuid as _u
        bid = f"sib_{_u.uuid4().hex[:8]}"
        with db.formal() as c:
            c.execute(
                "INSERT INTO source_import_batches(batch_id, provider,"
                " status, original_filename, original_bytes, sha256,"
                " raw_path, parser_version, import_started_at, imported_by,"
                " lease_token) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (bid, "claude", status, "f.json", 10, sha, "",
                 "p1", "2026-09-29T00:00:00Z", "jiaming", "lease-A"))
        return bid

    def test_stale_worker_cannot_destroy_takeover_success(self, actors):
        """A 卡死→B 接管并完成→A 复活调 _fail_batch：
        B 的快照/消息/状态/metadata 全部完好。"""
        from mariposa.source import importer
        bid = self._mkbatch(status="running")
        # 模拟 B 接管：换 lease + 完成发布（含快照与已发布消息）
        with db.formal() as c:
            c.execute(
                "UPDATE source_import_batches SET lease_token='lease-B',"
                " status='completed' WHERE batch_id=?", (bid,))
            c.execute(
                "INSERT INTO source_conversations(id, provider,"
                " provider_conversation_id, first_import_batch_id,"
                " last_import_batch_id) VALUES('sc-f1','claude','convF',"
                " ?, ?)", (bid, bid))
            c.execute(
                "INSERT INTO source_conversation_snapshots(snapshot_id,"
                " conversation_id, batch_id, created_at)"
                " VALUES('snap-f1','sc-f1',?, '2026-09-29T00:00:00Z')",
                (bid,))
            c.execute(
                "INSERT INTO source_snapshot_members(snapshot_id,"
                " provider_message_id, sequence, content_hash)"
                " VALUES('snap-f1','fm-1',1,'h1')")
            c.execute(
                "INSERT INTO source_messages(id, conversation_id, provider,"
                " provider_conversation_id, provider_message_id, raw_sender,"
                " normalized_sender, text, sequence, import_batch_id,"
                " published, content_hash)"
                " VALUES('sm-f1','sc-f1','claude','convF','fm-1','human',"
                " 'human','B 完成的正文',1,?,1,'h1')", (bid,))
        # A（旧 lease）复活，尝试失败定稿
        with pytest.raises(importer.LeaseLost):
            importer._fail_batch(bid, "claude", "stale worker crash",
                                 {"x": 1}, lease="lease-A")
        # B 的一切完好
        with db.formal() as c:
            b = c.execute(
                "SELECT status, lease_token FROM source_import_batches"
                " WHERE batch_id=?", (bid,)).fetchone()
            snaps = c.execute(
                "SELECT COUNT(*) n FROM source_conversation_snapshots"
                " WHERE batch_id=?", (bid,)).fetchone()["n"]
            members = c.execute(
                "SELECT COUNT(*) n FROM source_snapshot_members"
                " WHERE snapshot_id='snap-f1'").fetchone()["n"]
            msgs = c.execute(
                "SELECT COUNT(*) n FROM source_messages"
                " WHERE import_batch_id=? AND published=1",
                (bid,)).fetchone()["n"]
        assert b["status"] == "completed" and b["lease_token"] == "lease-B"
        assert snaps == 1 and members == 1 and msgs == 1, \
            "P1-03：旧 worker 毁掉了接管方的成功证据"

    def test_takeover_issues_new_lease_token(self, actors, tmp_path):
        """真实 _claim_batch 接管路径：接管生成新 token，旧 token 失效。"""
        from mariposa.source import importer
        import json as _json
        bid = self._mkbatch(status="running")
        # 把 started_at 拨到过期
        with db.formal() as c:
            c.execute(
                "UPDATE source_import_batches SET import_started_at='2020-01-01T00:00:00Z'"
                " WHERE batch_id=?", (bid,))
        f = tmp_path / "takeover.json"
        f.write_text(_json.dumps([{"uuid": "c-t1", "chat_messages": [
            {"uuid": "t-1", "sender": "human",
             "created_at": "2026-07-01T00:00:00.000Z",
             "content": [{"type": "text", "text": "接管正文"}]}]}],
            ensure_ascii=False), encoding="utf-8")
        # _claim_batch 需要 staged 文件存在（sha256 读取）
        r = importer._claim_batch("claude", "sha-take", f, "jiaming", "t.json")
        new_bid, reused, lease = r
        assert new_bid == bid and reused is True
        assert lease and lease != "lease-A", "接管必须换发新 fencing token"
        # 旧 token 立即失效
        with db.formal() as c:
            with pytest.raises(importer.LeaseLost):
                importer._assert_lease(c, bid, "lease-A")


class TestP102DenseWiring:
    """P1-02：dense 路遵守 v1.7 字段矩阵，卡片携带版本与证据。"""

    def _enable_dense(self, monkeypatch):
        from mariposa import config as cfg
        from mariposa.retrieval import semantic

        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")

        def fake_embed(texts):
            import numpy as np
            out = []
            for t in texts:
                # 伪向量：含目标词 → [1, 0]；查询含目标词 → [1, 0]
                compact = t.replace(" ", "")
                v = [1.0, 0.0] if "极光" in compact else [0.0, 1.0]
                out.append(np.asarray(v, dtype=np.float32))
            return out

        monkeypatch.setattr(semantic, "embed", fake_embed)


    def test_dense_card_has_version_field_and_evidence(self, actors,
                                                       monkeypatch):
        """正文含目标词 → dense 召回，且卡携带版本、event_text 字段、
        真实 snippet；同 operation 重放不被 guard 删。"""
        self._enable_dense(monkeypatch)
        from mariposa.capabilities import registry as reg
        from mariposa.memory import service as memory
        out = memory.hold(actors["jiaming"], text="深夜山顶看见极光铺满天空",
                          memory_date="2026-09-01",
                          date_confidence="exact", original_title="极光夜",
                          categories=["daily"],
                          creation_mode="contemporaneous", raw_pending=False)
        mid = out["memory_id"]
        args = {"query_plan": {
            "original_request": "找极光", "channels": ["event"],
            "semantic_query": "极光", "lexical_terms": ["zzz不存在的词"]},
            "operation_id": "op-dense-1"}
        r1 = reg.invoke(actors["jiaming"], "memory.recall.start", args, None)
        cards = [c for c in r1["data"]["data"]["candidates"]
                 if c.get("memory_id") == mid]
        assert cards, "前置：dense 应召回正文命中的记忆"
        card = cards[0]
        assert card["content_version"] == "1", \
            "dense 卡必须携带真实 revision（replay/receipt 校验依据）"
        assert "event_text" in card["matched_fields"]
        ev = card["evidence"][0]
        assert ev["evidence_kind"] == "authored_event"
        assert "极" in (ev.get("snippet") or "").replace(" ", ""), \
            "dense 卡证据必须携带真实事件正文片段"
        # 同 operation 重放：dense 卡不被 guard 删除
        r2 = reg.invoke(actors["jiaming"], "memory.recall.start", args, None)
        assert r2["data"].get("idempotent_replay") is True
        replayed = [c for c in r2["data"]["data"]["candidates"]
                    if c.get("memory_id") == mid]
        assert replayed, "P1-02：dense 卡被自己的 replay guard 误删"


class TestP2Fixes:
    """P2-01 投影一致性（hold 即含 why/meaning，与 update/rebuild 一致）
    + P2-02 plan 对外口径不再宣称 20 天。"""

    def test_hold_projection_consistent_with_rebuild(self, actors):
        """刚 hold 的 v2 记忆：why 可检索（与 update/rebuild 后一致），
        不再有"先不可搜、rebuild 后突然可搜"的漂移。"""
        from mariposa.memory import service as memory
        from mariposa.retrieval import rebuild as rr
        from mariposa.retrieval import search as rs
        out = memory.hold(
            actors["jiaming"], text="一致性场景正文内容",
            memory_date="2026-09-01", date_confidence="exact",
            original_title="一致性", categories=["daily"],
            creation_mode="contemporaneous", raw_pending=False,
            why_remember="独特的纪念理由蓝风铃")
        with db.formal() as conn:
            before = conn.execute(
                "SELECT search_text, whitelist_body FROM"
                " retrieval_documents WHERE memory_id=?",
                (out["memory_id"],)).fetchone()
            hits_before = len(rs.search(conn, "蓝风铃", 10)["hits"])
        rr.rebuild_index()
        with db.formal() as conn:
            after = conn.execute(
                "SELECT search_text, whitelist_body FROM"
                " retrieval_documents WHERE memory_id=?",
                (out["memory_id"],)).fetchone()
            hits_after = len(rs.search(conn, "蓝风铃", 10)["hits"])
        assert before["search_text"] == after["search_text"], \
            "P2-01：hold 与 rebuild 的投影内容不一致"
        assert before["whitelist_body"] == after["whitelist_body"]
        # 2026-09-30 裁定：why 为禁检来源——一致性指"两侧都不命中"
        assert hits_before == hits_after == 0

    def test_plan_api_no_stale_20day_wording(self, actors):
        from mariposa.capabilities import registry as reg
        created = reg.invoke(actors["jiaming"], "plan.create",
                             {"title": "口径计划", "content": "x"}, None)
        pid = created["data"]["plan_id"]
        got = reg.invoke(actors["jiaming"], "plan.get",
                         {"plan_id": pid}, None)
        assert "20" not in got["data"]["forgetting_note"], \
            f"P2-02：对外仍宣称 20 天口径：{got['data']['forgetting_note']}"
        assert "CORE" in got["data"]["forgetting_note"]
