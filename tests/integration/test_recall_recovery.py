"""运行库恢复与失效（SESSION-04 / RUNTIME-03/04 / EVID-03 服务端链）。"""
from __future__ import annotations

import pytest

from mariposa import db, schema
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service
from mariposa.recall import store
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": identity.Principal("jiaming", "周家明", "agent",
                                          "claude_chat", "bj")}


def hold(actors, text, date):
    return memory.hold(actors["jiaming"], text=text, memory_date=date,
                       date_confidence="exact", original_title="t",
                       categories=["daily"],
                       creation_mode="contemporaneous", raw_pending=False)


def start(actors):
    return recall_service.start(actors["jiaming"], {"query_plan": {
        "original_request": "找搬家", "channels": ["event"],
        "lexical_terms": ["搬家"]}})


class TestRecovery:
    def test_session04_survives_process_restart(self, actors):
        """SESSION-04：重启后未过期 active session 从宿主 runtime 恢复。

        store 每次操作新开连接（无进程内缓存），等价于重启进程后读取；
        再显式跑一遍 runtime 迁移确认幂等。
        """
        hold(actors, "搬家事件甲", "2026-08-10")
        p = start(actors)
        sid = p["recall_session_id"]
        # 模拟容器/进程重启：重跑迁移（生产启动路径）后再读
        schema.migrate_runtime()
        st = recall_service.status(actors["jiaming"], {"session_id": sid})
        assert st["session"]["session_id"] == sid
        assert st["session"]["status"] in ("ACTIVE", "AMBIGUOUS")
        # 同 session 还能继续纠正（恢复的不只是可读，是可续）
        p2 = recall_service.refine(actors["jiaming"], {
            "session_id": sid, "query_plan": {
                "original_request": "再找", "channels": ["event"],
                "lexical_terms": ["搬家"]}})
        assert p2["revision"] == 2

    def test_runtime03_attempts_survive_restart(self, actors):
        """RUNTIME-03：操作/预算记录跨重启保留（attempt 落宿主 runtime）。"""
        hold(actors, "搬家事件甲", "2026-08-10")
        p = start(actors)
        sid = p["recall_session_id"]
        with db.recall_runtime() as conn:
            rows = conn.execute(
                "SELECT operation_id, status FROM recall_attempts"
                " WHERE session_id=?", (sid,)).fetchall()
        assert rows and all(r["status"] == "completed" for r in rows)


class TestInvalidation:
    def test_runtime04_formal_change_invalidates_receipts(self, actors):
        """RUNTIME-04/EVID-03 链：正式修订后 status 重校验发现失效，
        不从 formal 缓存重放旧正文，session 转 STALE_RETRY_REQUIRED。"""
        m = hold(actors, "搬家事件甲", "2026-08-10")
        p = start(actors)
        sid = p["recall_session_id"]
        # 正式内容修订（版本前进）
        from mariposa.memory import extras
        extras.update_text(actors["jiaming"].principal_id, m["memory_id"],
                           1, "搬家事件甲修订版", None, None)
        st = recall_service.status(actors["jiaming"], {"session_id": sid})
        assert st["receipts_revalidated"]["invalid_refs"]
        assert st["session"]["status"] == "STALE_RETRY_REQUIRED"
        # STALE 后仍可 status/close，但不能直接 refine 继续检索
        from mariposa.errors import Forbidden
        with pytest.raises(Forbidden):
            recall_service.refine(actors["jiaming"], {
                "session_id": sid, "query_plan": {
                    "original_request": "再", "channels": ["event"],
                    "lexical_terms": ["搬家"]}})
        closed = recall_service.close(actors["jiaming"], {
            "session_id": sid, "outcome": "cancelled"})
        assert closed["status"] == "CANCELLED"

    def test_forgetting_invalidates_word_receipts(self, actors):
        """遗忘生效使 our_word 回执失效（读取时重校验兜底）。"""
        m = hold(actors, "搬家事件甲", "2026-08-10")
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO memory_our_words(word_id, memory_id, ordinal,"
                " speaker, text, expression_kind, source_ref, created_by,"
                " created_at) VALUES('ow_t1',?,1,'qiaosheng','搬家话语',"
                "'verbatim',NULL,'jiaming','2026-08-10')",
                (m["memory_id"],))
        p = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "找话", "channels": ["words"],
            "lexical_terms": ["搬家"]}})
        sid = p["recall_session_id"]
        # 遗忘生效（表示变化）
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET compression_state="
                "'forgotten_summary' WHERE memory_id=?", (m["memory_id"],))
        st = recall_service.status(actors["jiaming"], {"session_id": sid})
        assert "our_word:ow_t1" in st["receipts_revalidated"]["invalid_refs"]
