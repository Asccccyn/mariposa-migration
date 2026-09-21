"""Home / Self / Diary 独立内容（§10.3–10.5）与情绪标签（§10.2）。

- Home：唯一正本，乔生或周家明改，版本化
- Self：只有周家明写；写了立即是正式 self（pending=隔日待回看）；
  另一个共同当地日才能 review/revise/retire
- Diary：本人作品，全文逐字保留，可独立检索（source=diary），按 covers 区间进日历
- memory_tags：情绪标签 whose 必填；遗忘桶的结构化入口不因压缩消失
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import audit, config, db
from ..errors import Forbidden, NotFound
from ..memory import service as memory
from ..retrieval import projection


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_date_str() -> str:
    return _now().astimezone(ZoneInfo(config.RELATIONSHIP_TIMEZONE)).date().isoformat()


def _hash(payload: dict) -> str:
    return memory.canonical_hash(payload)


# ---------------- Home ----------------

def home_get(conn) -> dict:
    row = conn.execute("SELECT * FROM home WHERE id=1").fetchone()
    if row is None:
        return {"version": 0, "content": None, "note": "尚未建立"}
    v = conn.execute(
        "SELECT * FROM home_versions WHERE home_id=1 AND version_no=?",
        (row["current_version_no"],)).fetchone()
    return {"version": row["current_version_no"], "content": v["content"],
            "updated_at": row["updated_at"]}


def home_update(principal_id: str, content: str, expected_version: int) -> dict:
    if not content or not str(content).strip():
        raise Forbidden("home content required")
    now = _now().isoformat()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM home WHERE id=1").fetchone()
            current = row["current_version_no"] if row else 0
            if current != expected_version:
                raise Forbidden("home version conflict", code="VERSION_CONFLICT",
                                expected=expected_version, current=current)
            if row is None:
                conn.execute(
                    "INSERT INTO home(id, current_version_no, updated_at)"
                    " VALUES(1, 1, ?)", (now,))
            new_version = current + 1
            conn.execute(
                "INSERT INTO home_versions(home_id, version_no, content, edited_by,"
                " payload_hash, created_at) VALUES(1,?,?,?, ?,?)",
                (new_version, str(content), principal_id,
                 _hash({"content": content, "v": new_version}), now))
            conn.execute(
                "UPDATE home SET current_version_no=?, updated_at=? WHERE id=1",
                (new_version, now))
            audit.record(conn, "home.updated", principal_id, resource_id="home",
                         resource_version=new_version)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"version": new_version}


# ---------------- Self ----------------

def self_write(principal_id: str, content: str, aspect: str = "") -> dict:
    """只有周家明写；立即正式，pending=隔日待回看，不是「还不算他」（§10.4）。"""
    if principal_id != "jiaming":
        raise Forbidden("only jiaming writes self entries", principal=principal_id)
    if not content or not str(content).strip():
        raise Forbidden("self content required")
    sid = f"self_{uuid.uuid4().hex[:10]}"
    now = _now()
    local_today = now.astimezone(ZoneInfo(config.RELATIONSHIP_TIMEZONE)).date()
    review_on = (local_today + timedelta(days=1)).isoformat()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO self_entries(id, aspect, current_version_no,"
                " review_state, written_at, review_available_on, created_at, updated_at)"
                " VALUES(?,?,1,'pending',?,?,?,?)",
                (sid, aspect, now.isoformat(), review_on, now.isoformat(), now.isoformat()))
            conn.execute(
                "INSERT INTO self_versions(self_id, version_no, content, written_by,"
                " payload_hash, created_at) VALUES(?,1,?,?,?,?)",
                (sid, str(content), principal_id,
                 _hash({"content": content, "v": 1}), now.isoformat()))
            audit.record(conn, "self.written", principal_id, resource_id=sid,
                         resource_version=1,
                         payload={"review_available_on": review_on})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"self_id": sid, "version": 1, "review_state": "pending",
            "review_available_on": review_on}


def self_list(include_retired: bool = False) -> list[dict]:
    q = ("SELECT s.*, v.content FROM self_entries s JOIN self_versions v"
         " ON v.self_id = s.id AND v.version_no = s.current_version_no")
    if not include_retired:
        q += " WHERE s.review_state != 'retired'"
    q += " ORDER BY s.written_at DESC"
    with db.formal() as conn:
        rows = conn.execute(q).fetchall()
    return [{"self_id": r["id"], "aspect": r["aspect"], "version": r["current_version_no"],
             "review_state": r["review_state"], "written_at": r["written_at"],
             "review_available_on": r["review_available_on"], "content": r["content"]}
            for r in rows]


def _get_self(conn, self_id: str):
    row = conn.execute("SELECT * FROM self_entries WHERE id=?", (self_id,)).fetchone()
    if row is None:
        raise NotFound("self entry not found", self_id=self_id)
    return row


def _require_next_day(conn, row, action: str) -> None:
    today = _local_date_str()
    if today < row["review_available_on"]:
        raise Forbidden(
            f"self entry can only be {action} on/after another local day",
            code="SELF_REVIEW_TOO_EARLY", available_on=row["review_available_on"],
            today=today)


def self_review(principal_id: str, self_id: str) -> dict:
    with db.formal() as conn:
        row = _get_self(conn, self_id)
        _require_next_day(conn, row, "reviewed")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE self_entries SET review_state='reviewed', updated_at=?"
                " WHERE id=?", (_now().isoformat(), self_id))
            audit.record(conn, "self.reviewed", principal_id, resource_id=self_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"self_id": self_id, "review_state": "reviewed"}


def self_revise(principal_id: str, self_id: str, content: str,
                expected_version: int) -> dict:
    if principal_id != "jiaming":
        raise Forbidden("only jiaming revises self entries")
    with db.formal() as conn:
        row = _get_self(conn, self_id)
        _require_next_day(conn, row, "revised")
        if row["current_version_no"] != expected_version:
            raise Forbidden("self version conflict", code="VERSION_CONFLICT",
                            expected=expected_version, current=row["current_version_no"])
        new_version = row["current_version_no"] + 1
        now = _now().isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO self_versions(self_id, version_no, content, written_by,"
                " payload_hash, created_at) VALUES(?,?,?,?,?,?)",
                (self_id, new_version, str(content), principal_id,
                 _hash({"content": content, "v": new_version}), now))
            conn.execute(
                "UPDATE self_entries SET current_version_no=?, review_state='reviewed',"
                " updated_at=? WHERE id=?", (new_version, now, self_id))
            audit.record(conn, "self.revised", principal_id, resource_id=self_id,
                         resource_version=new_version)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"self_id": self_id, "version": new_version}


def self_retire(principal_id: str, self_id: str) -> dict:
    """retire 后不主动浮现；明确历史读取仍可查（不物理删）。"""
    with db.formal() as conn:
        row = _get_self(conn, self_id)
        _require_next_day(conn, row, "retired")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE self_entries SET review_state='retired', updated_at=?"
                " WHERE id=?", (_now().isoformat(), self_id))
            audit.record(conn, "self.retired", principal_id, resource_id=self_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"self_id": self_id, "review_state": "retired"}


# ---------------- Diary ----------------

def diary_write(principal_id: str, title: str, content: str,
                covers_from: str | None, covers_to: str | None) -> dict:
    if not content or not str(content).strip():
        raise Forbidden("diary content required")
    if (covers_from is None) != (covers_to is None):
        raise Forbidden("covers_from and covers_to must be given together")
    did = f"diary_{uuid.uuid4().hex[:10]}"
    now = _now().isoformat()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO diary_entries(id, current_version_no, author, covers_from,"
                " covers_to, created_at, updated_at) VALUES(?,1,?,?,?,?,?)",
                (did, principal_id, covers_from, covers_to, now, now))
            conn.execute(
                "INSERT INTO diary_versions(diary_id, version_no, title, content,"
                " edited_by, payload_hash, created_at) VALUES(?,1,?,?,?,?,?)",
                (did, title or "", str(content), principal_id,
                 _hash({"content": content, "v": 1}), now))
            audit.record(conn, "diary.written", principal_id, resource_id=did,
                         resource_version=1,
                         payload={"covers_from": covers_from, "covers_to": covers_to})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"diary_id": did, "version": 1}


def diary_list(include_hidden: bool = False, author: str | None = None) -> list[dict]:
    q = ("SELECT d.*, v.title, v.content FROM diary_entries d JOIN diary_versions v"
         " ON v.diary_id = d.id AND v.version_no = d.current_version_no")
    conds = []
    if not include_hidden:
        conds.append("d.hidden = 0")
    if author:
        conds.append("d.author = ?")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY d.created_at DESC"
    with db.formal() as conn:
        rows = conn.execute(q, (author,) if author else ()).fetchall()
    return [{"diary_id": r["id"], "author": r["author"], "title": r["title"],
             "version": r["current_version_no"], "covers_from": r["covers_from"],
             "covers_to": r["covers_to"], "hidden": bool(r["hidden"]),
             "content": r["content"]} for r in rows]


def diary_search(query: str, limit: int = 20) -> dict:
    """日记独立语义/关键词检索：source=diary，不得反向算记忆命中（§10.5）。"""
    toks = [t.lower() for t in projection.tokenize(query)]
    if not toks:
        return {"hits": [], "source": "diary"}
    joined = " ".join(toks)
    hits = []
    for d in diary_list():
        if joined in projection.normalize_search_text(
                (d["title"] or "") + "\n" + d["content"]):
            hits.append({"diary_id": d["diary_id"], "title": d["title"],
                         "author": d["author"], "covers_from": d["covers_from"],
                         "matched_by": "diary_keyword", "source": "diary"})
            if len(hits) >= limit:
                break
    return {"hits": hits, "source": "diary"}


def diary_hide(principal_id: str, diary_id: str, hide: bool) -> dict:
    with db.formal() as conn:
        row = conn.execute("SELECT * FROM diary_entries WHERE id=?",
                           (diary_id,)).fetchone()
        if row is None:
            raise NotFound("diary not found", diary_id=diary_id)
        if row["author"] != principal_id:
            raise Forbidden("only the author may hide/show a diary entry")
        conn.execute(
            "UPDATE diary_entries SET hidden=?, updated_at=? WHERE id=?",
            (int(hide), _now().isoformat(), diary_id))
    return {"diary_id": diary_id, "hidden": hide}


# ---------------- 情绪标签 ----------------

def tags_add(principal_id: str, memory_id: str, tags: list[dict]) -> dict:
    """情绪标签 whose 必填（jiaming|qiaosheng）；双方同情绪写两项（§10.2）。"""
    with db.formal() as conn:
        if not conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                            (memory_id,)).fetchone():
            raise NotFound("memory not found", memory_id=memory_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            added = 0
            for t in tags:
                whose = t.get("whose")
                if whose not in ("jiaming", "qiaosheng"):
                    raise Forbidden("emotion tag requires whose=jiaming|qiaosheng",
                                    tag=t)
                conn.execute(
                    "INSERT OR IGNORE INTO memory_tags(memory_id, namespace, tag,"
                    " whose, confidence, created_by) VALUES(?,?,?,?, 'human', ?)",
                    (memory_id, t.get("namespace", "emotion"), str(t.get("tag", "")),
                     whose, principal_id))
                added += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "processed": added}


def by_emotion(tag: str, whose: str) -> dict:
    """结构化入口：遗忘桶仍可按情绪查到（文本依据不回退）。"""
    if whose not in ("jiaming", "qiaosheng"):
        raise Forbidden("whose must be jiaming or qiaosheng")
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT t.memory_id, m.compression_state FROM memory_tags t"
            " JOIN memories m ON m.memory_id = t.memory_id"
            " WHERE t.namespace='emotion' AND t.tag=? AND t.whose=?"
            " AND m.visibility='active'",
            (tag, whose)).fetchall()
    return {"tag": tag, "whose": whose,
            "hits": [{"memory_id": r["memory_id"],
                      "representation": r["compression_state"],
                      "matched_by": "tag"} for r in rows]}
