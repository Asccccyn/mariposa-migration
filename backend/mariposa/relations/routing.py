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
            # CB-032 + RA-021（2026-10-02 复审 P2）：Source 端反查消费
            # 完整端点身份——conversation 必须一致，消息区间按
            # validate_range 解析两端，同消息时按字符区间重叠匹配
            #（此前只拿 start_message_id，丢 conversation/end/offset）
            if "source_binding" in domains and _wants(direction, "in"):
                from ..source import binding as src_binding
                anchor = a.get("resource", {}) if isinstance(
                    a.get("resource"), dict) else {}
                conv_id = str(anchor.get("conversation_id", ident))
                s_mid = str(anchor.get("start_message_id", ""))
                e_mid = str(anchor.get("end_message_id", s_mid))
                s_off = anchor.get("start_char_offset")
                e_off = anchor.get("end_char_offset")
                rows = conn.execute(
                    "SELECT b.*, cs.sequence AS cseq, ce.sequence AS eseq"
                    " FROM memory_source_bindings b"
                    " JOIN source_conversations c ON"
                    " c.id=b.conversation_id OR"
                    " c.provider_conversation_id=b.conversation_id"
                    " JOIN source_messages cs ON cs.id=b.start_message_id"
                    " JOIN source_messages ce ON ce.id=b.end_message_id"
                    " WHERE c.id=? OR c.provider_conversation_id=?",
                    (conv_id, conv_id)).fetchall()

                # F08（2026-10-03 审计 P2）：区间重叠按半开语义统一判定
                # ——"a 整体在 b 前"= a 的末端不晚于 b 的首端（同消息时
                # 按字符偏移，跨消息按消息序，偏移缺失视为消息粒度）。
                # 旧实现 <= 把 [0,4) 与 [4,8) 判为重叠；且跨消息查询的
                # 起止字符偏移被整个丢弃。
                def _entirely_before(e_seq, e_off, b_seq, b_off):
                    if e_seq is None or b_seq is None:
                        return False
                    if e_seq < b_seq:
                        return True
                    return (e_seq == b_seq
                            and e_off is not None and b_off is not None
                            and e_off <= b_off)

                def _ov(a0, a1, b0, b1):
                    return (a0 is None or b1 is None or a0 < b1) and \
                           (b0 is None or a1 is None or b0 < a1)
                for r in rows:
                    if not (r["cseq"] is not None and r["eseq"] is not None):
                        continue
                    # 消息区间重叠（同消息时叠加字符半开区间重叠）
                    msg_overlap = True
                    if s_mid and e_mid:
                        srow = conn.execute(
                            "SELECT id, provider_message_id, sequence,"
                            " conversation_id FROM"
                            " source_messages WHERE id=? OR"
                            " provider_message_id=?",
                            (s_mid, s_mid)).fetchone()
                        erow = conn.execute(
                            "SELECT id, provider_message_id, sequence,"
                            " conversation_id FROM"
                            " source_messages WHERE id=? OR"
                            " provider_message_id=?",
                            (e_mid, e_mid)).fetchone()
                        if srow is None or erow is None:
                            continue
                        # F08：锚定消息必须属于绑定所在会话——不同会话
                        # 撞 sequence 不得互相匹配
                        _bconv = r["conversation_id"]
                        if (srow["conversation_id"] != _bconv
                                or erow["conversation_id"] != _bconv):
                            continue
                        # SRC-07：覆盖以共享范围解析器判定的实际
                        # parent 路径成员为准（含锚定消息端点）；
                        # 锚定消息不在绑定路径上 → 不算命中。解析
                        # 失败保守跳过
                        from ..source import query as _sq
                        _pids = _sq.covered_path_ids(
                            _bconv, r["start_message_id"],
                            r["end_message_id"])
                        _sid = srow["id"]
                        _eid = erow["id"]
                        if _pids is None or not (
                                _sid in _pids and _eid in _pids):
                            continue
                        q_s, q_e = srow["sequence"], erow["sequence"]
                        msg_overlap = not _entirely_before(
                            q_e, e_off, r["cseq"],
                            r["start_char_offset"]) and \
                            not _entirely_before(
                                r["eseq"], r["end_char_offset"],
                                q_s, s_off)
                        if msg_overlap and q_s == q_e \
                                and r["cseq"] == r["eseq"] and (
                                    s_off is not None or e_off is not None):
                            msg_overlap = _ov(
                                s_off, e_off,
                                r["start_char_offset"],
                                r["end_char_offset"])
                    if msg_overlap:
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
