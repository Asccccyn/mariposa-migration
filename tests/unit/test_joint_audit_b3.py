"""联合审计 2026-10-06 B3 批回归（mariposa 侧）。

覆盖 F-J-04（purge/reset 三子表）/ F-J-05（raw_msg 读侧 invalid 非 500）/
F-J-19（by_category/by_emotion NULL 三态游标）/ F-J-20（placeholder 现行大类）/
F-J-23（迁移工具缺分类拒迁）/ F-J-26（删除幂等落穿分支结构化）/ F-J-27
（continuation 升级提示与接续引用合并）。
"""
from __future__ import annotations

import hashlib
import json
import uuid
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mariposa import db
from mariposa.deletion import service as deletion
from mariposa.errors import Forbidden, IdempotencyConflict
from mariposa.identity import service as identity
from mariposa.memory import listing, service as memory
from mariposa.recall import service as recall_service, store
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold(actors, i=0, **kw):
    base = dict(text=f"事件正文{i}", memory_date=f"2026-08-1{i}",
                date_confidence="exact", original_title=f"标题{i}",
                categories=["sweet"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def start(actors, terms=("搬家",), **plan_kw):
    plan = {"original_request": "找搬家的事", "channels": ["event"],
            "lexical_terms": list(terms)}
    plan.update(plan_kw)
    return recall_service.start(actors["jiaming"], {"query_plan": plan})


# —— F-J-05：legacy raw_msg 回执读侧标 invalid，不再查询已退役表 ——
class TestFJ05RawMsgRevalidate:
    def test_status_with_legacy_raw_msg_receipt_marks_invalid(self, actors):
        hold(actors, 0, text="八月搬家事件", memory_date="2026-08-10")
        packet = start(actors)
        sid = packet["recall_session_id"]
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO recall_receipts(session_id, receipt_id,"
                " resource_ref, content_version, representation_version,"
                " permission_version, valid_at, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (sid, f"rc_{uuid.uuid4().hex[:10]}", "raw_msg:legacy-1",
                 "cv", "rv", "pv", now, now))
            conn.execute("COMMIT")
        # 修复前：OperationalError: no such table raw_messages（500）
        out = recall_service.status(actors["jiaming"],
                                    {"session_id": sid})
        rv = out.get("receipts_revalidated") or {}
        assert "raw_msg:legacy-1" in rv.get("invalid_refs", []), \
            "legacy raw_msg 前缀应读侧标 invalid（CURRENT §6 gap 语义）"


# —— F-J-04：超期 session 清理/测试清库覆盖三张无 FK 子表 ——
class TestFJ04PurgeChildren:
    def _seed_session_with_children(self, expired: bool):
        principal_id = "jiaming"
        plan = {"original_request": "x", "channels": ["event"]}
        draft = store.new_session_draft(principal_id, "", plan)
        sid = draft["session_id"]
        now = datetime.now(timezone.utc)
        # 过期须早于 TTL×2 清理阈值（store.purge_expired 的 cutoff）
        expires = (now - timedelta(hours=config_ttl() * 2 + 1)
                   if expired else now + timedelta(hours=config_ttl()))
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO recall_continue_refs(session_id,"
                " continue_request_ref, from_revision, created_at)"
                " VALUES (?,?,?,?)", (sid, "cont_x", 1, now))
            conn.execute(
                "INSERT INTO recall_raw_continuations(session_id,"
                " revision, burst_no, token, next_offset, issued_at)"
                " VALUES (?,?,?,?,?,?)", (sid, 1, 1, "tok", 0, now))
            conn.execute(
                "INSERT INTO recall_raw_leases(session_id, revision,"
                " burst_no, lease_token, created_at)"
                " VALUES (?,?,?,?,?)", (sid, 1, 1, "lk", now))
            # draft 不落库：手种 session 行（status 用合法枚举）
            conn.execute(
                "INSERT INTO recall_sessions(session_id, principal_id,"
                " status, policy_version, created_at, updated_at,"
                " expires_at) VALUES (?,?,?,?,?,?,?)",
                (sid, principal_id, "ACTIVE", "pol",
                 now, now, expires.isoformat()))
            conn.execute("COMMIT")
        return sid

    def test_purge_deletes_three_children_with_session(self, actors):
        sid = self._seed_session_with_children(expired=True)
        live_sid = self._seed_session_with_children(expired=False)
        store.purge_expired()
        with db.recall_runtime() as conn:
            for table in ("recall_continue_refs",
                          "recall_raw_continuations",
                          "recall_raw_leases"):
                n = conn.execute(
                    f"SELECT COUNT(*) c FROM {table} WHERE session_id=?",
                    (sid,)).fetchone()["c"]
                assert n == 0, f"{table} 超期子行应随 session 清理"
            gone = conn.execute(
                "SELECT COUNT(*) c FROM recall_sessions WHERE"
                " session_id=?", (sid,)).fetchone()["c"]
            assert gone == 0
            for table in ("recall_continue_refs",
                          "recall_raw_continuations",
                          "recall_raw_leases"):
                n = conn.execute(
                    f"SELECT COUNT(*) c FROM {table} WHERE"
                    " session_id=?", (live_sid,)).fetchone()["c"]
                assert n == 1, f"{table} 活 session 子行不动"
            alive = conn.execute(
                "SELECT COUNT(*) c FROM recall_sessions WHERE"
                " session_id=?", (live_sid,)).fetchone()["c"]
            assert alive == 1

    def test_reset_for_tests_clears_three_children(self, actors):
        sid = self._seed_session_with_children(expired=False)
        store.reset_for_tests()
        with db.recall_runtime() as conn:
            for table in ("recall_continue_refs",
                          "recall_raw_continuations",
                          "recall_raw_leases"):
                n = conn.execute(
                    f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
                assert n == 0, f"{table} 应被测试清库覆盖"


def config_ttl():
    from mariposa import config
    return config.RECALL_SESSION_TTL_HOURS


# —— F-J-19：by_category/by_emotion NULL 日期三态游标 ——
class TestFJ19NullCursor:
    def _seed(self, actors, dated: list, nulls: int):
        ids = []
        for d in dated:
            out = hold(actors, len(ids), text=f"有日期事件{d}",
                       memory_date=d,
                       original_title=f"有日期{d}")
            ids.append(out["memory_id"])
        for i in range(nulls):
            out = hold(actors, 100 + i, text=f"无日期事件{i}",
                       memory_date=None, date_confidence="unknown",
                       original_title=f"无日期{i}")
            ids.append(out["memory_id"])
        return ids

    def _page_all(self, category="sweet", limit=2):
        seen, cursor, rounds = [], None, 0
        while True:
            cur_kwargs = {}
            if cursor is not None:
                cur_kwargs = {"cursor_date": cursor.get("memory_date"),
                              "cursor_id": cursor.get("memory_id")}
            out = listing.by_category(category, limit=limit, **cur_kwargs)
            seen.extend(x["memory_id"] for x in out["items"])
            rounds += 1
            assert rounds < 50, "翻页不终止（死循环回归）"
            if not out.get("has_more"):
                return seen
            cursor = out["next_cursor"]

    def test_dated_then_null_pages_complete_no_loop(self, actors):
        self._seed(actors, ["2026-08-01", "2026-08-02", "2026-08-03",
                            "2026-08-04"], nulls=3)
        seen = self._page_all()
        assert len(seen) == 7, "有日期+无日期全部可达"
        assert len(set(seen)) == 7, "不重复"

    def test_all_null_pages_complete(self, actors):
        self._seed(actors, [], nulls=5)
        seen = self._page_all()
        assert len(seen) == 5 and len(set(seen)) == 5

    def test_same_date_multiple_buckets(self, actors):
        self._seed(actors, ["2026-08-01"] * 4, nulls=1)
        seen = self._page_all()
        assert len(seen) == 5 and len(set(seen)) == 5, "同日多桶不丢"

    def test_incomplete_cursor_rejected(self, actors):
        self._seed(actors, ["2026-08-01"], nulls=1)
        with pytest.raises(Forbidden) as ei:
            listing.by_category("sweet", cursor_date="2026-08-01",
                                cursor_id=None)
        assert ei.value.detail.get("code") == "INVALID_ARGUMENT"

    def test_by_emotion_null_paging(self, actors):
        self._seed(actors, ["2026-08-01", "2026-08-02"], nulls=2)
        # by_emotion 空标签=全量（同谓词路径）
        seen, cursor, rounds = [], None, 0
        while True:
            kw = {}
            if cursor is not None:
                kw = {"cursor_date": cursor.get("memory_date"),
                      "cursor_id": cursor.get("memory_id")}
            out = listing.by_emotion("", limit=2, **kw)
            seen.extend(x["memory_id"] for x in out["items"])
            rounds += 1
            assert rounds < 50
            if not out.get("has_more"):
                break
            cursor = out["next_cursor"]
        assert len(seen) == 4 and len(set(seen)) == 4


# —— F-J-27：words 升级提示与接续引用合并非覆盖 ——
class TestFJ27ContinuationMerge:
    def test_words_insufficient_hint_survives_with_ref(self, actors):
        packet = start(actors, terms=("不存在的话语",),
                       channels=["words"])
        cont = packet["continuation"]
        assert cont.get("continue_request_ref"), "接续引用在场"
        assert cont.get("for_revision") == 1
        assert cont.get("action") == "round2_raw", \
            "words 证据不足的升级提示不再被整体覆盖丢弃"

    def test_event_channel_no_upgrade_hint(self, actors):
        packet = start(actors)
        cont = packet["continuation"]
        assert cont.get("continue_request_ref")
        assert "action" not in cont, "event 通道无升级提示（不误报）"


# —— F-J-23：离线迁移工具缺分类拒迁 ——
class TestFJ23MigrationCategories:
    def test_apply_missing_categories_goes_to_problems(self, actors,
                                                       tmp_path):
        from mariposa import migration
        body = "迁移正文内容\n"
        f = tmp_path / "legacy1.md"
        f.write_text(f"---\nlegacy_id: legacy1\n---\n{body}",
                     encoding="utf-8")
        import hashlib
        report = {"fixture_dir": str(tmp_path),
                  "entries": [{
                      "legacy_id": "legacy1", "target": "memories",
                      "path": "legacy1.md",
                      "payload_hash": hashlib.sha256(
                          body.strip().encode()).hexdigest(),
                      "mapping": {}}]}
        rp = tmp_path / "report.json"
        rp.write_text(json.dumps(report), encoding="utf-8")
        out = migration.apply_from_report(str(rp))
        assert any(p["issue"] == "categories_missing"
                   for p in out["problems"]), "缺分类应列问题拒迁"
        assert not out["applied"], "不应自动补 daily 落桶"
        with db.formal() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM memories"
                             ).fetchone()["c"]
        assert n == 0


# —— F-J-26：deletion 幂等落穿分支结构化冲突 ——
class TestFJ26DeletionIdempotency:
    def _mk_memory(self, actors, i):
        return hold(actors, 200 + i, text=f"删除目标{i}",
                    original_title=f"删{i}")["memory_id"]

    def test_same_key_same_payload_replay(self, actors):
        mid = self._mk_memory(actors, 0)
        out1 = deletion.deletion_request("qiaosheng", mid, "理由A",
                                         operation_key="op-b26-a")
        out2 = deletion.deletion_request("qiaosheng", mid, "理由A",
                                         operation_key="op-b26-a")
        assert out2.get("idempotent_replay") is True
        assert out2["request_id"] == out1["request_id"]

    def test_legacy_null_hash_different_target_structured(self, actors):
        mid_a = self._mk_memory(actors, 1)
        mid_b = self._mk_memory(actors, 2)
        # 手种"同键同哈希但回执指向另一桶"的异常记录（正常 API 不可能
        # 产生——哈希覆盖 memory_id；模拟陈旧/篡改形态）。修复前：落穿到
        # INSERT 撞同键 UNIQUE → 裸 IntegrityError(500)；修复后结构化冲突
        import hashlib as _hl
        ph = _hl.sha256(json.dumps(
            {"memory_id": mid_b, "reason": "理由B"},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO idempotency_records(principal_id, capability,"
                " idempotency_key, payload_hash, status, result_ref,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                ("qiaosheng", "memory.deletion.request", "op:op-b26-x",
                 ph, "completed",
                 json.dumps({"memory_id": mid_a,
                             "request_id": "dr_ghost"}), now))
            conn.execute("COMMIT")
        with pytest.raises(IdempotencyConflict):
            deletion.deletion_request("qiaosheng", mid_b, "理由B",
                                      operation_key="op-b26-x")

    def test_stale_receipt_missing_request_structured(self, actors):
        mid = self._mk_memory(actors, 3)
        deletion.deletion_request("qiaosheng", mid, "理由",
                                  operation_key="op-b26-stale")
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM deletion_requests WHERE"
                         " memory_id=?", (mid,))
            conn.execute("COMMIT")
        with pytest.raises(IdempotencyConflict):
            deletion.deletion_request("qiaosheng", mid, "理由",
                                      operation_key="op-b26-stale")


# —— F-J-20：网页心情输入框只引导现行大类 ——
class TestFJ20Placeholder:
    def test_placeholder_no_retired_word(self):
        html = (Path(__file__).resolve().parents[2]
                / "backend/mariposa/web/index.html").read_text(
                    encoding="utf-8")
        assert "亲密" not in html.split("browseMood")[1].split("\n")[0], \
            "placeholder 不应引导已废子心情词（输入必被拒）"
