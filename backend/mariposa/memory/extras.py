"""记忆扩展能力：update（正文/标题/日期修订，§17.3）。

2026-10-13 她裁定退役：pin/protect/anchor 旗标（遗忘机制 v1.7 退役后
无消费方的死开关）整段删除——死代码是多轮审计的方法盲区（只有规格
→代码对照，没有代码→规格反查），本批清理见 registry 同日注释。
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, VersionConflict
from . import service as memory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_UNSET = object()  # 字段缺席哨兵（WR-04：缺席≠显式 null 清空）


def update_text(principal_id: str, memory_id: str, expected_version: int,
                text: str | None = None,
                memory_date=_UNSET, date_confidence=_UNSET,
                original_title=_UNSET) -> dict:
    """修改桶正文/标题：新版本，不就地覆盖（§4.5）。

    审计 F39：expected_version 校验在 BEGIN IMMEDIATE 写锁内执行——
    两个并发编辑同旧 revision，先提交者成功，后者得到结构化
    VERSION_CONFLICT，而不是裸 IntegrityError。
    审计 F04：正文/投影构造统一走 version_body / rebuild_full_projection，
    v2 记忆只改元数据不再让事件正文从检索消失。
    original_title（2026-10-12 她 2026-10-11 裁定的标题修订）：_UNSET
    哨兵与日期字段同语义——缺席保留现值；提供则校验（非空、≤30 字）
    并写入新版本行。text 与 original_title 至少其一，纯看日期/无参
    调用保持既有兼容（元数据-only 版本推进）。
    """
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            m = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                             (memory_id,)).fetchone()
            if m is None:
                raise NotFound("memory not found", memory_id=memory_id)
            if m["current_version_no"] != expected_version:
                raise VersionConflict("memory version moved",
                                      expected=expected_version,
                                      current=m["current_version_no"])
            v = conn.execute(
                "SELECT * FROM memory_versions WHERE memory_id=? AND version_no=?",
                (memory_id, expected_version)).fetchone()
            # hold_text 列延续：v2 版本行（正文在 event_text）保持 NULL；
            # v1 旧形态行继续以 hold_text 承载正文。跨版本形态不漂移。
            if text is not None:
                new_text = None if v["event_text"] is not None else text
            else:
                new_text = None if v["event_text"] is not None else v["hold_text"]
            if m["compression_state"] == "forgotten_summary":
                raise Forbidden(
                    "forgotten memory cannot be edited in place; restore first")
            new_version = expected_version + 1
            now = _now()
            # A01 + F04：未提供新正文时，经统一入口延续当前 revision 正文
            #（event_text 优先，v1 旧数据回退 hold_text）
            new_event = text if text is not None else memory.version_body(v)
            new_title = v["original_title"]
            if original_title is not _UNSET:
                t = str(original_title).strip()
                if not t:
                    raise Forbidden("original_title 不可修订为空",
                                    code="ORIGINAL_TITLE_REQUIRED")
                if len(t) > 30:
                    raise Forbidden(
                        f"标题超长（{len(t)} > 30 字符）",
                        code="ORIGINAL_TITLE_TOO_LONG", length=len(t))
                new_title = t
            old_schema = v["schema_version"] or 1
            # N01：hash 输入与 canonical 正文字段一致——v2 正文在
            # event_text，只 hash hold_text(NULL) 会让不同正文同指纹
            payload = {"representation": "full", "hold_text": new_text,
                       "event_text": new_event, "origin": "update",
                       "original_title": new_title}
            conn.execute(
                "INSERT INTO memory_versions(memory_id, version_no, representation,"
                " hold_text, compressed_summary, authored_by, confirmed_by,"
                " origin_kind, payload_hash, created_at, original_title,"
                " event_text, schema_version)"
                " VALUES(?,?,'full',?,NULL,?,NULL,'initial_hold',?,?,?,?,?)",
                (memory_id, new_version, new_text, principal_id,
                 memory.canonical_hash(payload), now, new_title, new_event,
                 old_schema))
            conn.execute(
                "UPDATE memories SET current_version_no=?, memory_date=?,"
                " date_confidence=?, updated_at=? WHERE memory_id=?",
                (new_version,
                 # WR-04 + RE-WR-02（2026-10-04 复审勘误）：
                 # memory_date 列**实际可空**
                 # （PRAGMA notnull=0，此前注释所称 NOT NULL 不实）——
                 # 显式 null 清空是**未实现的残留接口限制**：null 等同
                 # 未提供（保留现值）。date_confidence 同；其公开 schema
                 # 不收 null（入口即拒，非"保留"语义）。是否支持日期
                 # 清空属产品决策，未定案前如实标注、不冒充已修。
                 m["memory_date"] if (memory_date is _UNSET
                                      or memory_date is None)
                 else memory_date,
                 m["date_confidence"] if (date_confidence is _UNSET
                                          or date_confidence is None)
                 else date_confidence, now, memory_id))
            from ..retrieval import field_projection as _fp
            _fp.build_for_memory(conn, memory_id)
            memory.rebuild_full_projection(conn, memory_id)
            audit.record(conn, "memory.updated", principal_id,
                         resource_id=memory_id, resource_version=new_version,
                         payload={"title_changed": original_title is not _UNSET
                                  and new_title != v["original_title"],
                                  "text_changed": text is not None})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "version": new_version}


def update_mood(principal_id: str, memory_id: str, mood: dict) -> dict:
    """当时心情单独修订（2026-10-12）：不推进正文版本行。

    八栏口径：当时心情=感受+为什么记得+想说没说+标签。合同校验复用
    hold 时的 _validate_mood——仅 jiaming、仅 contemporaneous 桶（
    V2-REC-09/04）。已有心情行覆盖更新（mood 是当前态不是历史栈，
    修订留审计不留版本行）；标签整组重建。
    """
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            m = conn.execute("SELECT creation_mode FROM memories"
                             " WHERE memory_id=?",
                             (memory_id,)).fetchone()
            if m is None:
                raise NotFound("memory not found", memory_id=memory_id)
            from ..identity import Principal as _Principal
            mood_data = memory._validate_mood(
                _Principal(principal_id=principal_id,
                           display_name=principal_id,
                           kind="ai" if principal_id == "jiaming"
                           else "human",
                           entry_source="web", binding_id=""),
                mood if isinstance(mood, dict) else {}, m["creation_mode"])
            now = memory._now()
            exists = conn.execute(
                "SELECT 1 FROM memory_moods WHERE memory_id=?",
                (memory_id,)).fetchone()
            if exists:
                conn.execute(
                    "UPDATE memory_moods SET mood_text=?, captured_at=?"
                    " WHERE memory_id=?",
                    (mood_data["text"], now, memory_id))
            else:
                conn.execute(
                    "INSERT INTO memory_moods(memory_id, mood_text, author,"
                    " captured_session, captured_at, evidence_state)"
                    " VALUES(?,?, 'jiaming', 'web', ?, 'contemporaneous')",
                    (memory_id, mood_data["text"], now))
            conn.execute("DELETE FROM memory_mood_tags WHERE memory_id=?",
                         (memory_id,))
            for tag in mood_data["tags"]:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_mood_tags(memory_id, tag)"
                    " VALUES(?,?)", (memory_id, tag))
            audit.record(conn, "memory.mood_updated", principal_id,
                         resource_id=memory_id,
                         payload={"tags": mood_data["tags"]})
            conn.execute("COMMIT")
            return {"memory_id": memory_id, "mood": mood_data}
        except Exception:
            conn.execute("ROLLBACK")
            raise


