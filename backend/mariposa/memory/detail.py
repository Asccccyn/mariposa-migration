"""按桶全量详情（docs/05）：引用直读原桶，不再次 recall。

"全量" = 该桶当前获授权的数据及其完整关系边（五域正反方向），不是
整个数据库/邻居桶全文/已物理删除历史。分页在同一 detail snapshot 上
推进（Unicode 码点半开区间，一次性 JSON 字节预算含封装与转义）；中途
桶变更返回 DETAIL_SNAPSHOT_STALE，可对同一 ref 重新开始，仍无需 recall。

一致性（docs/05）：聚合构建前后各取一次跨域版本向量复核，变化则整体
重取一次，仍变化 → DETAIL_SNAPSHOT_UNSTABLE——不把新旧数据拼成一份。

只读副作用合同：不写 memories 业务字段、不建 memory_view_receipts、
不改 last_explicit_open_at/keep/hold/绑定/关系/回忆（原 memory_open
的显式打开链不合并进来）；运行快照写 surface 运行库。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import config, db
from ..errors import NotFound
from ..memory import service as mem_service
from ..relations import routing as relations_routing
from ..source import binding as src_binding
from ..source import query as src_query
from ..surfacing import store as surface_store
from ..surfacing.candidates import domain_version_vector
from ..surfacing.models import SurfaceBlocked

#: 段固定顺序（返回序按合同确定，不随调用打乱）
SECTION_ORDER = ("memory", "categories", "mood", "our_words", "source",
                 "recollections", "keeps", "relations")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


def _chunk_text(text: str, byte_limit: int) -> list[dict]:
    """Unicode 码点分片（含封装字节实测；二分逼近上限，不丢增补字符）。

    每片实测 {"start_cp","end_cp","text"} 序列化后 ≤ byte_limit——与包
    reference.chunk_text 同规则（reference 是规则锁定，不是生产代码）。
    """
    if not text:
        return []
    out: list[dict] = []
    start = 0
    while start < len(text):
        lo, hi, best = start + 1, len(text), start
        while lo <= hi:
            end = (lo + hi) // 2
            item = {"start_cp": start, "end_cp": end,
                    "text": text[start:end]}
            if _json_bytes(item) <= byte_limit:
                best, lo = end, end + 1
            else:
                hi = end - 1
        if best == start:
            raise SurfaceBlocked("DETAIL_PARTIAL_UNAVAILABLE",
                                 detail="page_too_small_for_codepoint")
        out.append({"start_cp": start, "end_cp": best,
                    "text": text[start:best]})
        start = best
    return out


# --------------------------------------------------------------- 构建快照

def build_sections(memory_id: str, conn) -> tuple[dict, dict, dict]:
    """取全量段。段级读取失败 → 该段 unavailable（合法空=empty，读取
    失败/权限阻断=unavailable，不都置空数组——docs/05）。"""
    from ..memory import our_words, recollections, keep as keep_mod

    sections: dict = {}
    status: dict = {s: "empty" for s in SECTION_ORDER}

    full = mem_service.get(conn, memory_id)   # 不存在 → NotFound 上抛

    sections["memory"] = {k: full.get(k) for k in (
        "memory_id", "version", "original_title", "memory_date",
        "date_confidence", "occurred_start", "occurred_end", "held_at",
        "representation", "creation_mode", "visibility", "text")}
    status["memory"] = "complete"
    sections["categories"] = full.get("categories") or []
    status["categories"] = "complete" if sections["categories"] else "empty"

    mood = full.get("mood")
    if mood and (mood.get("text") or mood.get("tags")):
        sections["mood"] = mood
        status["mood"] = "complete"
    else:
        sections["mood"] = {}
        status["mood"] = "empty"

    def _safe(fn, section):
        try:
            value = fn()
        except Exception:
            return None, "unavailable"
        return value, ("complete" if value else "empty")

    words, status["our_words"] = _safe(
        lambda: our_words.list_for(memory_id), "our_words")
    sections["our_words"] = words

    def _source():
        out = []
        for r in src_binding.ranges_of(memory_id):
            entry = {k: r.get(k) for k in (
                "binding_id", "conversation_id", "start_message_id",
                "end_message_id", "start_char_offset", "end_char_offset",
                "version", "created_at") if k in r}
            try:
                opened = src_query.open_range(
                    r["conversation_id"], r["start_message_id"],
                    r["end_message_id"],
                    r.get("start_char_offset"), r.get("end_char_offset"),
                    include_content=True)
                entry["messages"] = opened.get("messages")
                entry["state"] = "complete"
            except Exception:
                # 未绑定原文与读取失败是不同状态：失败标 unavailable
                entry["messages"] = None
                entry["state"] = "unavailable"
            out.append(entry)
        return out

    src, status["source"] = _safe(_source, "source")
    sections["source"] = src
    if src is not None and any(e.get("state") == "unavailable"
                               for e in src):
        status["source"] = "partial"

    recl, status["recollections"] = _safe(
        lambda: recollections.list_for(memory_id, include_history=True),
        "recollections")
    sections["recollections"] = recl

    def _keeps():
        marks = keep_mod.marks_of(memory_id)
        return {"marks": marks,
                "active_keepers": keep_mod.active_keepers(memory_id)}

    keeps, status["keeps"] = _safe(_keeps, "keeps")
    sections["keeps"] = keeps
    if keeps is not None and not keeps["marks"]:
        status["keeps"] = "empty"

    def _relations():
        # 五域正反边全页（memory.cards.batch 只带 memory_relation 边，
        # 不能当五域齐全）
        edges: list[dict] = []
        offset = 0
        while True:
            page = relations_routing.list_relations({
                "memory_id": memory_id, "direction": "both",
                "limit": 200, "offset": offset})
            batch = (page.get("relations") or page.get("items")
                     or page.get("edges") or [])
            edges.extend(batch)
            if len(batch) < 200:
                break
            offset += 200
        return edges

    rel, status["relations"] = _safe(_relations, "relations")
    sections["relations"] = rel

    domain_versions = domain_version_vector(conn, memory_id)
    return sections, status, domain_versions


# --------------------------------------------------------------- 分页

def _structured_cost(name: str, payload) -> int:
    return _json_bytes({name: payload}) + len(name) + 8


def _longest_text_field(value) -> str | None:
    if not isinstance(value, dict):
        return None
    best_key, best_len = None, 0
    for k, v in value.items():
        if isinstance(v, str) and len(v) > best_len:
            best_key, best_len = k, len(v)
    return best_key if best_len > 0 else None


def _put_structured(name, payload, st, budget, put):
    limit = budget - 192  # 与 memory 正文分片同额的信封余量
    if not (isinstance(payload, list) and payload):
        if _structured_cost(name, payload) <= limit:
            put(name, payload, st)
            return
        _put_fragmented(name, payload, st, None, limit, put)
        return
    batch: list = []
    for index, value in enumerate(payload):
        if _structured_cost(name, [value]) > limit:
            if batch:
                put(name, batch, "partial")
                batch = []
            _put_fragmented(name, value, st, index, limit, put)
            continue
        if batch and _structured_cost(name, batch + [value]) > limit:
            put(name, batch, "partial")
            batch = []
        batch.append(value)
    if batch:
        put(name, batch, st)


def _put_fragmented(name, value, st, index, limit, put):
    field = _longest_text_field(value)
    if field is None:
        raise SurfaceBlocked(
            "DETAIL_PARTIAL_UNAVAILABLE",
            detail="structured_item_exceeds_page_budget", section=name)
    text = value[field]
    base = {k: v for k, v in value.items() if k != field}
    chunks = _chunk_text(text, limit - _structured_cost(
        name, [{**base, field: "", "partial_item": True,
                "item_index": index, "text_range": [0, 0]}]))
    if not chunks:
        raise SurfaceBlocked("DETAIL_PARTIAL_UNAVAILABLE", section=name)
    for i, ch in enumerate(chunks):
        frag = {**base, field: ch["text"], "partial_item": True,
                "item_index": index,
                "text_range": [ch["start_cp"], ch["end_cp"]]}
        put(name, [frag], st if i == len(chunks) - 1 else "partial")


def _paginate(sections: dict, status: dict, page_bytes: int) -> list[dict]:
    """字节有界页：长文本码点分片；每页 sections 只含本页承载的段。

    section_status 语义：本页内该段 partial=仍有后续分片；末页由
    read_page 换成全快照状态。返回序固定 = SECTION_ORDER。
    """
    budget = max(1024, int(page_bytes))
    pages: list[dict] = []
    cur: dict = {"sections": {}, "status": {}}
    used = 128  # 信封余量（snapshot_id/游标/版本字段）

    def flush():
        nonlocal cur, used
        if cur["sections"]:
            pages.append(cur)
            cur = {"sections": {}, "status": {}}
            used = 128

    def put(section: str, payload, st: str):
        nonlocal used
        cost = _json_bytes({section: payload}) + len(section) + 8
        # A final short fragment must not be overwritten by the next batch of
        # the same section, even when both fit in the remaining byte budget.
        if section in cur["sections"]:
            flush()
        if used + cost > budget and cur["sections"]:
            flush()
        cur["sections"][section] = payload
        cur["status"][section] = st
        used += cost

    for name in SECTION_ORDER:
        if name not in sections:
            continue
        payload = sections.get(name)
        st = status.get(name, "unavailable")
        if st == "unavailable":
            put(name, None, "unavailable")
            continue
        if name == "memory" and isinstance(payload, dict):
            text = payload.get("text") or ""
            meta = {k: v for k, v in payload.items() if k != "text"}
            chunks = (_chunk_text(text, budget - 192) if text else [])
            if not chunks:
                put(name, {**meta, "text": ""}, st)
            else:
                for i, ch in enumerate(chunks):
                    put(name, {**meta, "text": ch["text"],
                               "text_range": [ch["start_cp"],
                                               ch["end_cp"]]},
                        "partial" if i < len(chunks) - 1 else st)
        else:
            # R08（她 1021 指令收口：审计执行包移植）——超大单条结构项给
            # 稳定续页片段合同：最长字符串字段按码点分片（partial_item/
            # item_index/text_range，同 index 按序拼接精确还原），不再
            # 无上界整段交付；真正不可分割（无文本字段）明确拒绝，绝不
            # 静默截断或超门输出。普通条目路径零变化。
            _put_structured(name, payload, st, budget, put)
    flush()
    if not pages:
        pages = [{"sections": {}, "status": {}}]
    return pages


# --------------------------------------------------------------- 读页

def snapshot_key(detail_ref, section=None):
    return f"{detail_ref}/{section}" if section else detail_ref


def read_page(principal, a: dict) -> dict:
    detail_ref = str(a["detail_ref"])
    cursor = a.get("cursor")
    section = a.get("section")
    key = snapshot_key(detail_ref, section)

    def read_version(conn, memory_id):
        if section:
            row = conn.execute("SELECT current_version_no FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None: raise NotFound("memory not found", memory_id=memory_id)
            return str(row["current_version_no"])
        return str(mem_service.get(conn, memory_id).get("version") or "")

    def read_vector(conn, memory_id):
        vector = domain_version_vector(conn, memory_id)
        if section in ("source", "relations"):
            from .detail_sections import fingerprint
            vector["_detail_section"] = fingerprint(conn, memory_id, section)
        return vector

    item = _find_surfaced_item(detail_ref)
    if item is None:
        raise SurfaceBlocked("DETAIL_REF_NOT_SEEN")
    memory_id = item["memory_id"]

    with db.formal() as conn:
        try:
            current_version = read_version(conn, memory_id)
        except NotFound:
            raise SurfaceBlocked("DETAIL_NOT_FOUND", memory_id=memory_id)
        current_vector = read_vector(conn, memory_id)

    if cursor:
        snap = surface_store.load_detail_snapshot_by_ref(key)
        if snap is None:
            raise SurfaceBlocked("DETAIL_REF_NOT_SEEN")
        if (snap["memory_version"] != current_version
                or snap["domain_versions"] != current_vector):
            # 分页中途桶变更：不把新旧数据拼成一份全量（docs/05）
            raise SurfaceBlocked("DETAIL_SNAPSHOT_STALE")
        try:
            if section:
                prefix = f"{section}:p"
                if not str(cursor).startswith(prefix): raise ValueError("wrong section cursor")
                index = int(str(cursor)[len(prefix):])
            else:
                index = int(str(cursor).lstrip("p"))
        except ValueError:
            raise SurfaceBlocked("DETAIL_REF_NOT_SEEN", detail="bad_cursor")
        if not 0 <= index < len(snap["pages"]):
            raise SurfaceBlocked("DETAIL_REF_NOT_SEEN", detail="bad_cursor")
        snap["_section"] = section
        return _page_view(snap, index)

    # 首读=当前最新 + 明示与浮现版本差异（不偷偷给旧正文冒充当前）。
    # 一致性：构建前后版本向量复核，变化整体重取一次，仍变化 UNSTABLE。
    attempts = 0
    while True:
        with db.formal() as conn:
            if section:
                from .detail_sections import build
                sections, status = build(memory_id, section, conn)
                vector = read_vector(conn, memory_id)
            else:
                sections, status, vector = build_sections(memory_id, conn)
        if vector == current_vector:
            break
        attempts += 1
        if attempts > int(config.SURFACE_MAX_SNAPSHOT_RETRIES):
            raise SurfaceBlocked("DETAIL_SNAPSHOT_UNSTABLE")
        with db.formal() as conn:
            current_version = read_version(conn, memory_id)
            current_vector = read_vector(conn, memory_id)

    memory_version = str((sections.get("memory") or {}).get("version")
                         or current_version)
    changed = (memory_version != str(item.get("memory_version") or "")
               or {k: v for k, v in vector.items() if k != "_detail_section"} != (item.get("domain_versions") or {}))
    pages = _paginate(sections, status, config.SURFACE_DETAIL_PAGE_BYTES)
    snapshot_id = surface_store.save_detail_snapshot(
        key, memory_id, memory_version, vector,
        item.get("conversation_scope") or "", principal.principal_id,
        str(item.get("memory_version") or ""), changed, pages,
        status, len(pages) == 1)
    snap = surface_store.load_detail_snapshot_by_ref(key) or {}
    snap["snapshot_id"] = snapshot_id
    snap["_section"] = section
    return _page_view(snap, 0)


def _page_view(snap: dict, index: int):
    pages = snap.get("pages") or [{}]
    page = pages[index]
    last = index == len(pages) - 1
    sections = page.get("sections") or {}
    # 末页 section_status = 全快照状态（complete/empty/unavailable）；
    # 中间页 = 分片状态（partial=仍有后续）
    page_status = dict(page.get("status") or {})
    if last:
        full = snap.get("section_status")
        if isinstance(full, dict) and full:
            page_status = dict(full)
    return {
        "snapshot_id": snap.get("snapshot_id"),
        "memory_id": snap["memory_id"],
        "memory_version": snap["memory_version"],
        "domain_version_vector": snap["domain_versions"],
        "page_index": index,
        "sections": sections,
        "complete": bool(last),
        "next_cursor": None if last else (f"{snap['_section']}:p{index + 1}" if snap.get("_section") else f"p{index + 1}"),
        "section_status": page_status,
        "changed_since_surface": bool(snap.get("changed_since_surface")),
    }


def _find_surfaced_item(detail_ref: str) -> dict | None:
    """已见 ref 门：detail_ref 必须来自未过期验证回执的 items（服务器
    解析为真实 memory_id 与授权定位票据——模型不传 actor/scope/query，
    docs/05）。"""
    now = _now_iso()
    with db.recall_runtime() as conn:
        rows = conn.execute(
            "SELECT conversation_scope, items_json FROM"
            " surface_validation_receipts WHERE status='issued'"
            " AND expires_at >= ? LIMIT 500", (now,)).fetchall()
    for r in rows:
        try:
            items = json.loads(r["items_json"])
        except (ValueError, TypeError):
            continue
        for it in items:
            if isinstance(it, dict) and it.get("detail_ref") == detail_ref:
                out = dict(it)
                out["conversation_scope"] = r["conversation_scope"]
                return out
    return None
