"""2026-10-05 第三方全量审计修复回归（P1/P2 关键行为）。

对应审计编号见各 docstring；只测行为契约，不测实现细节。
基线：01edb28 之上的修复批。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.memory import our_words
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent",
                                     "gpt_chat", "bw"),
    }


def _hold(actors, text="审计回归桶", date="2026-06-01"):
    return memory.hold(actors["jiaming"], text=text, memory_date=date,
                       categories=["daily"], original_title="测试标题")


# ---------------------------------------------------------------- P1-01

class TestSourceImportConfinement:
    def test_jiaming_host_path_forbidden(self, actors, tmp_path):
        """P1-01：宿主任意路径对非 qiaosheng owner 关闭。"""
        from mariposa.capabilities.registry import _source_import
        with pytest.raises(Forbidden) as ei:
            _source_import(actors["jiaming"], {"path": "/etc/hosts"})
        assert ei.value.code == "SOURCE_PATH_FORBIDDEN"

    def test_jiaming_incoming_path_allowed(self, actors):
        """P1-01：上传暂存目录内的路径对所有 owner 开放（空数组=合法完成）。"""
        from mariposa import config
        from mariposa.capabilities.registry import _source_import
        config.SOURCE_INCOMING_DIR.mkdir(parents=True, exist_ok=True)
        f = config.SOURCE_INCOMING_DIR / "audit_re.json"
        f.write_text("[]", encoding="utf-8")
        try:
            out = _source_import(actors["jiaming"], {"path": str(f)})
            assert out["status"] == "completed"
        finally:
            f.unlink(missing_ok=True)

    def test_qiaosheng_host_path_passes_gate(self, actors, tmp_path):
        """P1-01：qiaosheng 本人保留宿主直读（过权限门后才是 NotFound）。"""
        from mariposa.capabilities.registry import _source_import
        with pytest.raises(NotFound):
            _source_import(actors["qiaosheng"],
                           {"path": str(tmp_path / "no-such-file.md")})

    def test_oversize_rejected_before_copy(self, actors, monkeypatch, tmp_path):
        """P1-01：直读路径与上传通道同一字节上限（复制前 stat 预检）。"""
        from mariposa import config
        from mariposa.capabilities.registry import _source_import
        big = tmp_path / "big.md"
        big.write_text("x" * 64, encoding="utf-8")
        monkeypatch.setattr(config, "SOURCE_UPLOAD_MAX_BYTES", 16)
        from mariposa.errors import MariposaError
        with pytest.raises(MariposaError) as ei:
            _source_import(actors["qiaosheng"], {"path": str(big)})
        assert ei.value.code == "SOURCE_IMPORT_TOO_LARGE"


# ---------------------------------------------------------------- P1-02

class TestWordsIndexConcurrency:
    def test_concurrent_rebuild_no_integrity_error(self, actors):
        """P1-02：指纹失效后并发 words_search 不再撞主键 500。"""
        from mariposa.retrieval import words as wm
        from mariposa.recall import pipeline as pl
        h = _hold(actors, "并发重建：今晚一起看了老电影")
        our_words.append("jiaming", h["memory_id"], [
            {"text": "散场时她说了句台词", "speaker": "jiaming"}])
        # 制造过期指纹（删除 meta 行 → 全部并发读都判定需重建）
        with db.formal() as conn:
            conn.execute("DELETE FROM mariposa_db_meta WHERE key=?",
                         (wm.WORDS_INDEX_META_KEY,))
        plan = {"original_request": "台词", "channels": ["words"],
                "lexical_terms": ["台词"]}

        def _search(_):
            with db.formal() as conn:
                return wm.words_search(conn, plan)

        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(_search, range(12)))
        # map 会重抛工作线程异常——能走到断言即证明无 IntegrityError
        assert all(r["coverage"] in ("complete_within_scope",
                                     "partial_topk_window")
                   for r in results)
        # 二次确认：重建后指纹已一致（index_rebuilt 至少一个 True）
        with db.formal() as conn:
            assert wm._index_fresh(conn)


# ---------------------------------------------------------------- P1-03

class TestCoverageHonestSignature:
    def test_v1_datagap_bucket_scanned_as_core(self, actors):
        """P1-03：held_at 缺失的 v1 桶按最小允许集纳入扫描且不虚签完整。"""
        from mariposa.recall import pipeline as pl
        h = _hold(actors, "旧桶：那年夏天的萤火虫")
        with db.formal() as conn:
            conn.execute("UPDATE memories SET held_at=NULL"
                         " WHERE memory_id=?", (h["memory_id"],))
        coverage: dict = {}
        with db.formal() as conn:
            pl.round1_lexical_hits(
                conn, {"original_request": "萤火虫",
                       "lexical_terms": ["萤火虫"]}, set(), coverage)
        stats = coverage["stage_filter"]
        assert stats["gap"] == 0, "DataGap 桶不再落 gap"
        assert stats["CORE"] >= 1, "v1 桶以 CORE 最小允许集参与扫描"
        assert coverage["event"] == "complete_within_scope"


# ---------------------------------------------------------------- P1-04

class TestAuditTrailOnSensitiveActions:
    def _event_types(self):
        with db.formal() as conn:
            return {r["event_type"] for r in conn.execute(
                "SELECT event_type FROM audit_events").fetchall()}

    def test_deletion_lifecycle_audited(self, actors):
        """P1-04：申请/撤回/驳回三个敏感动作同事务留痕。"""
        h = _hold(actors)
        rid = _request(h["memory_id"])
        assert "deletion.requested" in self._event_types()
        from mariposa.deletion import service as ds
        ds.deletion_withdraw("qiaosheng", rid)
        rid2 = _request(h["memory_id"])
        ds.deletion_decide("jiaming", rid2, "reject",
                           rejection_reason="审计回归：驳回")
        types = self._event_types()
        assert {"deletion.withdrawn", "deletion.rejected"} <= types

    def test_revoke_binding_and_handoff_audited(self, actors):
        """P1-04：凭证撤销与交接便签留痕。"""
        identity.revoke_binding("qiaosheng", "binding_worker")
        from mariposa.time_context import service as tctx
        tctx.handoff_write("jiaming", "claude_chat", "审计回归便签")
        types = self._event_types()
        assert {"binding.revoked", "handoff.written"} <= types


def _request(memory_id: str) -> str:
    from mariposa.deletion import service as ds
    return ds.deletion_request("qiaosheng", memory_id,
                               "审计回归：申请")["request_id"]


# ---------------------------------------------------------------- P2-01/P2-02

class TestReconcileFloorAndLimitClamps:
    def _running_record(self, age_s: int, key: str):
        created = (datetime.now(timezone.utc)
                   - timedelta(seconds=age_s)).isoformat()
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO idempotency_records(principal_id, capability,"
                " idempotency_key, payload_hash, status, result_ref,"
                " created_at) VALUES(?,?,?,?,?,?,?)",
                ("qiaosheng", "memory.hold", f"t:{key}", "ph",
                 "running", None, created))

    def test_fresh_record_refused_despite_tiny_stale_seconds(self):
        """P2-01：stale_seconds=1 也被服务端 600s 下限钉住——并发中的
        真实执行不能被强行判死触发双写。"""
        from mariposa.maintenance import service as ms
        self._running_record(age_s=30, key="floor-fresh")
        out = ms.idempotency_reconcile(
            "qiaosheng", "qiaosheng", "memory.hold", "floor-fresh",
            stale_seconds=1)
        assert out["reconciled"] is False

    def test_aged_record_reconciled_with_audit(self):
        from mariposa.maintenance import service as ms
        self._running_record(age_s=700, key="floor-aged")
        out = ms.idempotency_reconcile(
            "qiaosheng", "qiaosheng", "memory.hold", "floor-aged",
            stale_seconds=1)
        assert out["reconciled"] is True
        with db.formal() as conn:
            assert conn.execute(
                "SELECT 1 FROM audit_events WHERE event_type="
                "'maintenance.idempotency.reconciled'").fetchone()

    def test_non_numeric_limit_structured_400(self, actors):
        """P2-02：非数字 limit 是结构化 400，不是裸 500。"""
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["worker"], "maintenance.activity.list",
                            {"limit": "abc"}, None)
        assert ei.value.code == "INVALID_ARGUMENT"

    def test_negative_limit_clamped_not_unlimited(self, actors):
        """P2-02：负 limit 钳制到 1，不再变成 SQLite LIMIT -1 全表倾倒。"""
        out = registry.invoke(actors["worker"], "maintenance.activity.list",
                              {"limit": -5}, None)
        assert len(out["data"]["events"]) <= 1


# ---------------------------------------------------------------- P2-04

class TestOAuthHardening:
    def test_authorize_requires_pkce(self):
        """P2-04：无 code_challenge 的授权请求结构化拒绝。"""
        from fastapi.testclient import TestClient
        from mariposa.app import app
        with TestClient(app, raise_server_exceptions=False) as c:
            reg = c.post("/oauth/register", json={
                "redirect_uris": ["http://127.0.0.1:9/cb"]}).json()
            r = c.get("/oauth/authorize", params={
                "client_id": reg["client_id"],
                "redirect_uri": "http://127.0.0.1:9/cb",
                "state": "s"})
            assert r.status_code == 400
            assert r.json()["error"] == "invalid_request"

    def test_registration_capacity_enforced(self, monkeypatch):
        """P2-04：动态注册有容量上限，触顶结构化 503。"""
        from fastapi.testclient import TestClient
        from mariposa import oauth as _oauth
        from mariposa.app import app
        with TestClient(app, raise_server_exceptions=False) as c:
            with db.formal() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM oauth_clients"
                ).fetchone()["c"]
            monkeypatch.setattr(_oauth, "MAX_REGISTERED_CLIENTS", n)
            r = c.post("/oauth/register", json={
                "redirect_uris": ["http://127.0.0.1:9/cb2"]})
            assert r.status_code == 503
            assert r.json()["error"] == "REGISTRATION_CAPACITY"


# ---------------------------------------------------------------- P2-05

class TestRecallPurgeDeletesChildren:
    def test_stale_session_chain_removed(self):
        """P2-05：超期 session 连同子行物理删除（docstring 承诺兑现）。"""
        from mariposa import config
        from mariposa.recall import store as st
        old = (datetime.now(timezone.utc)
               - timedelta(hours=config.RECALL_SESSION_TTL_HOURS * 3)
               ).isoformat()
        with db.recall_runtime() as conn:
            conn.execute(
                "INSERT INTO recall_sessions(session_id, principal_id,"
                " status, policy_version, created_at, updated_at,"
                " expires_at) VALUES('purge-me','jiaming','RESOLVED',"
                " 'p','" + old + "','" + old + "','" + old + "')")
            conn.execute(
                "INSERT INTO recall_rounds(session_id, round_no, burst_no,"
                " operation_key, created_at)"
                " VALUES('purge-me',1,1,'k','" + old + "')")
            conn.execute(
                "INSERT INTO recall_candidates(session_id, candidate_ref,"
                " resource_ref, channel, representation, state,"
                " first_seen_revision, updated_at)"
                " VALUES('purge-me','c1','memory:x','event','full',"
                "'seen',1,'" + old + "')")
        st.purge_expired()
        with db.recall_runtime() as conn:
            assert conn.execute(
                "SELECT 1 FROM recall_sessions WHERE"
                " session_id='purge-me'").fetchone() is None
            assert conn.execute(
                "SELECT 1 FROM recall_rounds WHERE"
                " session_id='purge-me'").fetchone() is None
            assert conn.execute(
                "SELECT 1 FROM recall_candidates WHERE"
                " session_id='purge-me'").fetchone() is None


# ---------------------------------------------------------------- P2-06

class TestOpenForMemoryToleratesDrift:
    def test_too_large_binding_marked_unresolvable(self, actors, monkeypatch):
        """P2-06：单绑定超限不再毒死同 memory 全部绑定。"""
        from mariposa import config
        from mariposa.source import binding as sb
        h = _hold(actors)
        with db.formal() as conn:
            # drift-c1 三条（压限后超限）；ok-c2 两条（不超限）
            for cid, nmsg in (("drift-c1", 3), ("ok-c2", 2)):
                conn.execute(
                    "INSERT INTO source_conversations(id, provider,"
                    " provider_conversation_id, created_at, first_import"
                    "_batch_id, last_import_batch_id)"
                    " VALUES(?,'claude',?,datetime('now'),'b1','b1')",
                    (cid, cid))
                prev = None
                for i in range(1, nmsg + 1):
                    mid = f"{cid}-m{i}"
                    conn.execute(
                        "INSERT INTO source_messages(id, conversation_id,"
                        " provider, provider_conversation_id,"
                        " provider_message_id, parent_provider_message_id,"
                        " normalized_sender, created_at,"
                        " text, sequence, import_batch_id, published)"
                        " VALUES(?,?, 'claude', ?, ?, ?, 'human',"
                        " '2026-10-01T10:00:00', '正文', ?, 'b1', 1)",
                        (mid, cid, cid, mid, prev, i))
                    prev = mid
        sb.bind("jiaming", h["memory_id"], "drift-c1",
                "drift-c1-m1", "drift-c1-m3")
        sb.bind("jiaming", h["memory_id"], "ok-c2", "ok-c2-m1", "ok-c2-m2")
        # 把条数上限压到 2：三消息区间 → SOURCE_RANGE_TOO_LARGE
        monkeypatch.setattr(config, "SOURCE_RANGE_MAX_MESSAGES", 2)
        out = sb.open_for_memory(h["memory_id"], include_content=False)
        statuses = [r.get("status") for r in out["ranges"]]
        assert "unresolvable" in statuses, "漂移绑定降级为 unresolvable"
        # 另一条绑定照常打开（不被连坐）：成功条目展开 payload 字段
        # （含 conversation_id），失败条目只有 status=unresolvable
        opened_ok = [r for r in out["ranges"]
                     if r.get("status") != "unresolvable"]
        assert opened_ok, "好绑定被连坐"
        assert any("messages" in r for r in opened_ok)


# ---------------------------------------------------------------- P2-07

class TestWorkspaceDeadTablesGone:
    def test_no_recall_tables_in_workspace_db(self):
        """P2-07：workspace 库不再残留引用缺失表的死 recall_* 表。"""
        with db.workspace() as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert not ({"recall_query_revisions", "recall_candidates",
                     "recall_attempts", "recall_receipts",
                     "recall_operation_keys"} & names)


# ---------------------------------------------------------------- P3 抽样

class TestAmbiguousSourceRef:
    def test_cross_provider_collision_rejected(self, actors):
        """P3：跨 provider 同 UUID 撞号不再非确定解析。"""
        h = _hold(actors)
        with db.formal() as conn:
            for p in ("claude", "gemini"):
                conn.execute(
                    "INSERT INTO source_conversations(id, provider,"
                    " provider_conversation_id, created_at, first_import"
                    "_batch_id, last_import_batch_id)"
                    " VALUES(? ,?, ?, datetime('now'),'b1','b1')",
                    (f"amb-{p}", p, f"amb-{p}"))
                conn.execute(
                    "INSERT INTO source_messages(id, conversation_id,"
                    " provider, provider_conversation_id,"
                    " provider_message_id, normalized_sender, created_at,"
                    " text, sequence, import_batch_id, published)"
                    " VALUES(?,?,?,?,?,'human','2026-10-01T10:00:00',"
                    " '撞号正文', 1, 'b1', 1)",
                    (f"amb-{p}-m", f"amb-{p}", p, f"amb-{p}",
                     "shared-uuid"))
        with pytest.raises(Forbidden) as ei:
            our_words.append("jiaming", h["memory_id"], [
                {"text": "撞号来源话语", "speaker": "jiaming",
                 "source_ref": "source_msg:shared-uuid"}])
        assert ei.value.code == "SOURCE_MESSAGE_AMBIGUOUS"


class TestMemoryGetMissingVersion:
    def test_structured_not_found(self, actors):
        """P3：current_version_no 悬空时结构化 NotFound，不是 TypeError。"""
        h = _hold(actors)
        with db.formal() as conn:
            conn.execute("UPDATE memories SET current_version_no=99"
                         " WHERE memory_id=?", (h["memory_id"],))
        with db.formal() as conn:
            with pytest.raises(NotFound):
                memory.get(conn, h["memory_id"])
