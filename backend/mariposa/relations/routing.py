"""跨域关系只读路由（规格 v2.0 §6.2）。

不建图数据库/镜像表/全局 canonical ID——按资源身份路由到五域
原生表，返回可原样作为下一次查询起点的两端定位对象（§6.2）。
方向语义不颠倒：反向只改读法不改事实（§Q04）。
"""
from __future__ import annotations

from .. import db

_DOMAINS = ("memory_relation", "i_revision_relation", "source_binding",
            "plan_link", "word_source")


def _endpoint(res: dict) -> tuple[str, str]:
    """(domain, 具体身份)——支持 memory_id / (item_id, revision) /
    plan_id / word_id 单独或 {type: ..., ...} 带类型对象。"""
    if isinstance(res, str):
        if res.startswith("mem_") or res.startswith("memory:"):
            return "memory", res.split(":")[-1]
        if res.startswith("plan_"):
            return "plan", res
        if res.startswith("ow_"):
            return "word", res
        return "memory", res.split(":")[-1]
    t = res.get("type")
    if t == "memory":
        return "memory", res["memory_id"]
    if t == "i_revision":
        return "i_revision", f"{res['item_id']}@{res['revision']}"
    if t == "plan":
        return "plan", res["plan_id"]
    if t == "word":
        return "word", res["word_id"]
    raise ValueError("resource must carry type/memory_id/item_id+revision/"
                     "plan_id/word_id")


def list_relations(a: dict) -> dict:
    """有效关系正/反查（direction in/out/both；游标分页）。"""
    res = a.get("resource") or a.get("memory_id") or a.get("plan_id") \
        or a.get("word_id")
    if res is None and a.get("item_id") is not None:
        res = {"type": "i_revision", "item_id": a["item_id"],
               "revision": int(a.get("revision", 0))}
    from ..errors import Forbidden as _F
    if res is None:
        raise _F("resource 必填", code="SCHEMA_VIOLATION")
    kind, ident = _endpoint(res)
    direction = a.get("direction", "both")
    domains = a.get("domains") or _DOMAINS
    limit = max(1, min(int(a.get("limit", 50)), 200))
    offset = max(0, int(a.get("offset", 0)))
    out: list[dict] = []
    with db.formal() as conn:
        if kind == "memory":
            mid = ident
            if "memory_relation" in domains:
                if direction in ("out", "both"):
                    for r in conn.execute(
                            "SELECT * FROM memory_relations WHERE"
                            " from_memory=?", (mid,)):
                        out.append({
                            "domain": "memory_relation",
                            "relation_id": r["relation_id"],
                            "direction": "out",
                            "other": {"type": "memory",
                                      "memory_id": r["to_memory"]},
                            "relation_type": r["relation_type"],
                            "custom_label": r["custom_label"],
                            "reverse_label": r["reverse_label"]})
                if direction in ("in", "both"):
                    for r in conn.execute(
                            "SELECT * FROM memory_relations WHERE"
                            " to_memory=?", (mid,)):
                        out.append({
                            "domain": "memory_relation",
                            "relation_id": r["relation_id"],
                            "direction": "in",
                            "other": {"type": "memory",
                                      "memory_id": r["from_memory"]},
                            "relation_type": r["relation_type"],
                            "custom_label": r["custom_label"],
                            "reverse_label": r["reverse_label"],
                            "reversed": True})
            if "i_revision_relation" in domains:
                for r in conn.execute(
                        "SELECT * FROM i_revision_memory_relations WHERE"
                        " memory_id=?", (mid,)):
                    out.append({
                        "domain": "i_revision_relation",
                        "relation_id": r["relation_id"],
                        "direction": "in",
                        "other": {"type": "i_revision",
                                  "item_id": r["item_id"],
                                  "revision": r["revision"]},
                        "relation_type": r["relation_type"],
                        "reversed": True})
            if "source_binding" in domains:
                for r in conn.execute(
                        "SELECT * FROM memory_source_bindings WHERE"
                        " memory_id=?", (mid,)):
                    out.append({
                        "domain": "source_binding",
                        "relation_id": r["binding_id"],
                        "direction": "out",
                        "other": {"type": "source_range",
                                  "conversation_id": r["conversation_id"],
                                  "start_message_id": r["start_message_id"],
                                  "end_message_id": r["end_message_id"]},
                        "confidence": r["bind_confidence"]})
            if "plan_link" in domains:
                for r in conn.execute(
                        "SELECT * FROM plan_memory_links WHERE memory_id=?",
                        (mid,)):
                    out.append({
                        "domain": "plan_link", "relation_id": r["link_id"],
                        "direction": "out",
                        "other": {"type": "plan", "plan_id": r["plan_id"]}})
            if "word_source" in domains:
                for r in conn.execute(
                        "SELECT word_id FROM memory_our_words WHERE"
                        " memory_id=? AND source_ref IS NOT NULL"
                        " AND source_ref<>''", (mid,)):
                    out.append({
                        "domain": "word_source",
                        "relation_id": f"word:{r['word_id']}",
                        "direction": "out",
                        "other": {"type": "word",
                                  "word_id": r["word_id"]}})
        elif kind == "plan":
            for r in conn.execute(
                    "SELECT * FROM plan_memory_links WHERE plan_id=?",
                    (ident,)):
                out.append({
                    "domain": "plan_link", "relation_id": r["link_id"],
                    "direction": "out",
                    "other": {"type": "memory",
                              "memory_id": r["memory_id"]}})
        elif kind == "word":
            from ..memory.our_words import source_of
            ref = source_of(ident)
            if ref:
                out.append({
                    "domain": "word_source",
                    "relation_id": f"word:{ident}",
                    "direction": "out", "other": {"type": "source_ref",
                                                  "ref": ref}})
        elif kind == "i_revision":
            item_id, rev = ident.split("@")
            for r in conn.execute(
                    "SELECT * FROM i_revision_memory_relations WHERE"
                    " item_id=? AND revision=?", (item_id, int(rev))):
                out.append({
                    "domain": "i_revision_relation",
                    "relation_id": r["relation_id"], "direction": "out",
                    "other": {"type": "memory", "memory_id": r["memory_id"]},
                    "relation_type": r["relation_type"]})
    total = len(out)
    return {"relations": out[offset:offset + limit], "total": total,
            "limit": limit, "offset": offset,
            "next_offset": (offset + limit) if total > offset + limit
            else None}


def trace_relations(a: dict) -> dict:
    """沿指定域继续追链（§6.3：默认仅 memory_relation 的
    continuation_of，多分支全达、有环有界、披露截断）。"""
    res = a.get("resource") or a.get("memory_id")
    if res is None:
        raise _F("resource 必填", code="SCHEMA_VIOLATION")
    _, ident = _endpoint(res)
    from ..memory import relations as rel
    depth = max(1, min(int(a.get("max_depth", 5)), 20))
    return rel.trace(ident, max_depth=depth)
