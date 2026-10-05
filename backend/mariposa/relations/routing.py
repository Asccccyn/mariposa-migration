"""跨域关系只读路由（规格 v2.0 §6.2）。

不建图数据库/镜像表/全局 canonical ID——按资源身份路由到五域
原生表，返回可原样作为下一次查询起点的两端定位对象（§6.2）。
方向语义不颠倒：反向只改读法不改事实（§Q04）。

各域正式关系方向（CB-033：先定方向，再按当前端点标 in/out）：
- memory_relation：from → to
- i_revision_relation：revision → memory
- source_binding：memory → source_range
- plan_link：plan → memory
- word_source：word → source_ref（word 挂在 memory 名下，从 memory
  端查看作该桶话语的出边）
"""
from __future__ import annotations

from .. import db
from ..errors import Forbidden

_DOMAINS = ("memory_relation", "i_revision_relation", "source_binding",
            "plan_link", "word_source")


def _endpoint(res: dict) -> tuple[str, str]:
    """(domain, 具体身份)——支持 memory_id / (item_id, revision) /
    plan_id / word_id 单独或 {type: ..., ...} 带类型对象。

    CB-032：接受 relations.list 自己返回的 source_range / source_ref
    端点对象（Source 端反查入口）。
    """
    if isinstance(res, str):
        if res.startswith("mem_") or res.startswith("memory:"):
            return "memory", res.split(":")[-1]
        if res.startswith("plan_"):
            return "plan", res
        if res.startswith("ow_"):
            return "word", res
        return "memory", res.split(":")[-1]
    # F21（2026-10-03 审计 P2）：端点字段类型校验——id 为数组/缺字段/
    # 未知 type 此前裸 KeyError/ValueError 变 HTTP 500，统一结构化 4xx
    def _req_str(field: str) -> str:
        v = res.get(field)
        if not isinstance(v, str) or not v:
            raise Forbidden(
                f"resource.{field} 必须是非空字符串",
                code="INVALID_ARGUMENT", field=field,
                got_type=type(v).__name__)
        return v

    t = res.get("type")
    if t == "memory":
        return "memory", _req_str("memory_id")
    if t == "i_revision":
        item = _req_str("item_id")
        rev = res.get("revision")
        if not isinstance(rev, int) or isinstance(rev, bool):
            raise Forbidden("resource.revision 必须是整数",
                            code="INVALID_ARGUMENT", field="revision",
                            got_type=type(rev).__name__)
        return "i_revision", f"{item}@{rev}"
    if t == "plan":
        return "plan", _req_str("plan_id")
    if t == "word":
        return "word", _req_str("word_id")
    if t == "source_range":
        return "source_range", _req_str("conversation_id")
    if t == "source_ref":
        return "source_ref", _req_str("ref")
    raise Forbidden("resource.type 必须是 memory/i_revision/plan/word/"
                    "source_range/source_ref 之一",
                    code="INVALID_ARGUMENT",
                    got=t if isinstance(t, str) else type(t).__name__)


def _wants(direction: str, edge_dir: str) -> bool:
    """CB-033：请求方向过滤统一应用——非 both 时只收匹配方向的边。"""
    return direction in ("both", edge_dir)


def list_relations(a: dict) -> dict:
    """有效关系正/反查（direction in/out/both；游标分页）。"""
    res = a.get("resource") or a.get("memory_id") or a.get("plan_id") \
        or a.get("word_id")
    if res is None and a.get("item_id") is not None:
        res = {"type": "i_revision", "item_id": a["item_id"],
               "revision": int(a.get("revision", 0))}
    if res is None:
        raise Forbidden("resource 必填", code="SCHEMA_VIOLATION")
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
                if _wants(direction, "out"):
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
                if _wants(direction, "in"):
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
            if "i_revision_relation" in domains and _wants(direction, "in"):
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
            if "source_binding" in domains and _wants(direction, "out"):
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
            if "plan_link" in domains and _wants(direction, "in"):
                # plan_link 方向 plan → memory：从 memory 端看是入边
                for r in conn.execute(
                        "SELECT * FROM plan_memory_links WHERE memory_id=?",
                        (mid,)):
                    out.append({
                        "domain": "plan_link", "relation_id": r["link_id"],
                        "direction": "in",
                        "other": {"type": "plan", "plan_id": r["plan_id"]}})
            if "word_source" in domains and _wants(direction, "out"):
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
            if "plan_link" in domains and _wants(direction, "out"):
                for r in conn.execute(
                        "SELECT * FROM plan_memory_links WHERE plan_id=?",
                        (ident,)):
                    out.append({
                        "domain": "plan_link", "relation_id": r["link_id"],
                        "direction": "out",
                        "other": {"type": "memory",
                                  "memory_id": r["memory_id"]}})
        elif kind == "word":
            if "word_source" in domains and _wants(direction, "out"):
                from ..memory.our_words import source_of
                ref = source_of(ident)
                if ref:
                    out.append({
                        "domain": "word_source",
                        "relation_id": f"word:{ident}",
                        "direction": "out", "other": {"type": "source_ref",
                                                      "ref": ref}})
        elif kind == "i_revision":
            if "i_revision_relation" in domains and _wants(direction, "out"):
                item_id, rev = ident.split("@")
                for r in conn.execute(
                        "SELECT * FROM i_revision_memory_relations WHERE"
                        " item_id=? AND revision=?",
                        (item_id, int(rev))):
                    out.append({
                        "domain": "i_revision_relation",
                        "relation_id": r["relation_id"], "direction": "out",
                        "other": {"type": "memory",
                                  "memory_id": r["memory_id"]},
                        "relation_type": r["relation_type"]})
        elif kind == "source_range":
            # RSRC-07（2026-10-04 复审 P2）：反查重叠 = 查询范围与绑定
            # 范围各自沿真实 parent 链解析成有序路径后求**交集**——
            # 旧实现要求查询两端点都落在绑定路径上（A→D 查 B→C 判
            # 空），且用跨快照 sequence 大小做"整体在前"推断（序号
            # 倒置把已在交集中的消息误排除）。字符偏移只在共享消息
            # 上按 code point 半开区间比较（端点偏移缺省视为消息
            # 粒度，即开区间端）；任一共享消息上覆盖重叠即命中。
            if "source_binding" in domains and _wants(direction, "in"):
                from ..source import query as _sq
                anchor = a.get("resource", {}) if isinstance(
                    a.get("resource"), dict) else {}
                conv_id = str(anchor.get("conversation_id", ident))
                s_mid = str(anchor.get("start_message_id", ""))
                e_mid = str(anchor.get("end_message_id", s_mid))
                q_path = _sq.ordered_path_ids(
                    conv_id, s_mid, e_mid,
                    anchor.get("start_char_offset"),
                    anchor.get("end_char_offset"))
                if not q_path:
                    # 查询范围解析失败（消息不存在/断链/sibling）：
                    # 与任何绑定都不构成可判定的重叠，保守返回空
                    pass
                else:
                    q_sid, q_eid = q_path[0], q_path[-1]
                    q_set = set(q_path)

                    def _cover_on(mid, start_id, end_id, s_off, e_off):
                        """区间在消息 mid 上的覆盖（lo, hi）；hi=None
                        表示覆盖到消息末尾（开区间端/内部消息）。
                        ASRC-07：lo>=hi（有界端）是空半开覆盖——
                        返回 None 表示该消息上零覆盖。"""
                        lo = 0
                        hi = None
                        if mid == start_id and s_off is not None:
                            lo = s_off
                        if mid == end_id and e_off is not None:
                            hi = e_off
                        if hi is not None and lo >= hi:
                            return None
                        return lo, hi

                    def _ints_overlap(q, b) -> bool:
                        qs, qe = q
                        bs, be = b
                        if be is not None and qs >= be:
                            return False
                        if qe is not None and bs >= qe:
                            return False
                        return True

                    rows = conn.execute(
                        "SELECT b.* FROM memory_source_bindings b"
                        " JOIN source_conversations c ON"
                        " c.id=b.conversation_id OR"
                        " c.provider_conversation_id=b.conversation_id"
                        " WHERE c.id=? OR c.provider_conversation_id=?",
                        (conv_id, conv_id)).fetchall()
                    for r in rows:
                        # 绑定路径按查询的会话身份解析——绑定的端点
                        # 消息不属于该会话（含他方会话撞序号）时
                        # validate_range 直接 NotFound，保守跳过
                        b_path = _sq.ordered_path_ids(
                            conv_id, r["start_message_id"],
                            r["end_message_id"])
                        if not b_path:
                            continue  # 绑定路径解析失败：保守跳过
                        shared = q_set.intersection(b_path)
                        if not shared:
                            continue
                        b_sid, b_eid = b_path[0], b_path[-1]
                        overlap = False
                        for m_id in shared:
                            qc = _cover_on(
                                m_id, q_sid, q_eid,
                                anchor.get("start_char_offset"),
                                anchor.get("end_char_offset"))
                            bc = _cover_on(
                                m_id, b_sid, b_eid,
                                r["start_char_offset"],
                                r["end_char_offset"])
                            # ASRC-07：任一侧在该消息上零覆盖
                            #（空半开区间）即跳过
                            if qc is None or bc is None:
                                continue
                            if _ints_overlap(qc, bc):
                                overlap = True
                                break
                        if overlap:
                            out.append({
                                "domain": "source_binding",
                                "relation_id": r["binding_id"],
                                "direction": "in",
                                "other": {"type": "memory",
                                          "memory_id": r["memory_id"]},
                                "confidence": r["bind_confidence"]})
        elif kind == "source_ref":
            # CB-032：source_msg:<id> 反查——引用该消息的 words（native
            # 表）与覆盖该消息的绑定区间（按序范围）
            ref = ident
            if "word_source" in domains and _wants(direction, "in"):
                for r in conn.execute(
                        "SELECT word_id, memory_id FROM memory_our_words"
                        " WHERE source_ref=?", (ref,)):
                    out.append({
                        "domain": "word_source",
                        "relation_id": f"word:{r['word_id']}",
                        "direction": "in",
                        "other": {"type": "memory",
                                  "memory_id": r["memory_id"]}})
            if "source_binding" in domains and _wants(direction, "in"):
                from ..source import binding as src_binding
                msg_id = ref.split(":", 1)[-1] if ":" in ref else ref
                for b in src_binding.memories_referencing(str(msg_id)):
                    out.append({
                        "domain": "source_binding",
                        "relation_id": b["binding_id"],
                        "direction": "in",
                        "other": {"type": "memory",
                                  "memory_id": b["memory_id"]},
                        "confidence": b.get("bind_confidence")})
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
        raise Forbidden("resource 必填", code="SCHEMA_VIOLATION")
    _, ident = _endpoint(res)
    from ..memory import relations as rel
    depth = max(1, min(int(a.get("max_depth", 5)), 20))
    return rel.trace(ident, max_depth=depth)
