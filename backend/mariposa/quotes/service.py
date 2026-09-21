"""她的话（§10.1）：语义忠实、允许复述；独立资源，不随记忆压缩。

semantic_status 字段保留后台校对分类；受控校对管线为 reserved
（QUOTE_SEMANTIC_AUTO_APPLY 配置存在但管线未接真实模型前不产生写动作）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound
from ..retrieval import projection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def keep(principal_id: str, text: str, said_at: str | None = None,
         said_at_confidence: str = "unknown", raw_ref: str | None = None) -> dict:
    """周家明选取与保留；复述允许，不要求逐字。"""
    if principal_id != "jiaming":
        raise Forbidden("only jiaming keeps her words", principal=principal_id)
    if not text or not str(text).strip():
        raise Forbidden("quote text required")
    qid = f"qt_{uuid.uuid4().hex[:10]}"
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO quotes(id, current_version_no, said_at, said_at_confidence,"
                " kept_by, created_at, updated_at) VALUES(?,?,?,? ,?,?,?)",
                (qid, 1, said_at, said_at_confidence, principal_id, now, now),
            )
            conn.execute(
                "INSERT INTO quote_versions(quote_id, version_no, text, semantic_status,"
                " raw_ref, created_by, created_at) VALUES(?,1,?, 'no_source', ?,?,?)",
                (qid, str(text), raw_ref, principal_id, now),
            )
            audit.record(conn, "quote.kept", principal_id, resource_id=qid,
                         resource_version=1, payload={"said_at": said_at})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"quote_id": qid, "version": 1}


def list_quotes(include_withdrawn: bool = False, limit: int = 100) -> list[dict]:
    q = ("SELECT q.id, q.current_version_no, q.said_at, q.said_at_confidence, q.kept_by,"
         " q.withdrawn, v.text, v.semantic_status, v.raw_ref FROM quotes q"
         " JOIN quote_versions v ON v.quote_id = q.id"
         " AND v.version_no = q.current_version_no")
    if not include_withdrawn:
        q += " WHERE q.withdrawn = 0"
    q += " ORDER BY q.updated_at DESC LIMIT ?"
    with db.formal() as conn:
        rows = conn.execute(q, (limit,)).fetchall()
    return [dict(r) for r in rows]


def search(query: str, limit: int = 20) -> dict:
    """独立 quotes 检索：命中标 source=quotes，不得反向算作记忆搜索。"""
    toks = [t.lower() for t in projection.tokenize(query)]
    if not toks:
        return {"hits": [], "source": "quotes"}
    joined = " ".join(toks)
    hits = []
    for r in list_quotes():
        if joined in projection.normalize_search_text(r["text"]):
            hits.append({
                "quote_id": r["id"], "text": r["text"], "said_at": r["said_at"],
                "semantic_status": r["semantic_status"],
                "matched_by": "quote_keyword", "source": "quotes",
            })
            if len(hits) >= limit:
                break
    return {"hits": hits, "source": "quotes"}


def withdraw(principal_id: str, quote_id: str) -> dict:
    """撤下后任何校对不得重新浮现（§10.1）。"""
    with db.formal() as conn:
        row = conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if row is None:
            raise NotFound("quote not found", quote_id=quote_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE quotes SET withdrawn=1, updated_at=? WHERE id=?",
                         (_now(), quote_id))
            audit.record(conn, "quote.withdrawn", principal_id, resource_id=quote_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"quote_id": quote_id, "withdrawn": True}
