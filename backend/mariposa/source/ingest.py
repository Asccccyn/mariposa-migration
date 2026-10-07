"""在线原文 live ingest（estómago 生命周期 WP1；MARIPOSA_LIFECYCLE_v1.0 §4）。

与导出快照导入（importer.py）的关系：复用 Source 存储、检索投影与
绑定合同；本模块是**有限追加事务**（live_delta），不重放整房间快照。
每条消息修订 = 新的不可变 source_messages 行（不覆盖旧行）；谱系钉在
source_live_revisions；(stream, origin 消息, revision) 唯一。

原子性（契约 §4.3）：请求 JSON 先按 Raw Archive 规约落母本
（publish_bytes），再开短正式库事务写来源行/谱系/投影/审计与完成
回执（经 relations.corrections.atomic_write，op:<operation_id>）。
磁盘与 SQLite 不是同一事务：DB 失败只留待核对孤立资产，不存在
"ACK 成功而母本未落盘"的路径。

命名空间编码（契约 §4.2）：物理 provider_conversation_id /
provider_message_id 均为规范化结构 hash，不裸拼字符串——实例、房间、
逻辑消息、修订四层无歧义；业务 room 不因换窗/跨日改名。

content_hash 共享契约（与 estómago messages/service.ts 冻结一致）：
sha256(正文 UTF-8 字节) 的 hex。服务端核验不符即 SOURCE_HASH_MISMATCH。
"""
from __future__ import annotations

import hashlib
import json
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import audit, config, db
from ..errors import Forbidden, MariposaError, NotFound
from ..identity import Principal
from ..retrieval import projection
from ..relations.corrections import atomic_write
from . import archive

PROVIDER = "estomago"
PARSER_VERSION = "source_live_v1"
#: 允许的发布类型（首版收口：普通正式消息；控制事件/草稿不入本投影）
# D5（她批 2026-10-06 扩展归档合同）：sticker/voice 消息按各自
# published_kind 入库（转写文本与资产分存由 estómago 侧提供；发布门禁/
# 检索投影/绑定链对全部 kind 一致生效）
PUBLISHED_KINDS = ("chat_message", "sticker_message", "voice_message")

_SENDER_MAP = {"user": ("human", "qiaosheng"),
               "assistant": ("assistant", "jiaming")}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_hash(*parts) -> str:
    return hashlib.sha256(json.dumps(
        parts, ensure_ascii=False, sort_keys=True, default=str)
        .encode("utf-8")).hexdigest()


def provider_conversation_id_of(origin_instance: str,
                                origin_conversation_id: str) -> str:
    return f"esc-{_canonical_hash(origin_instance, origin_conversation_id)[:24]}"


def provider_message_id_of(origin_instance: str,
                           origin_conversation_id: str,
                           origin_message_id: str, revision: int) -> str:
    return f"esm-{_canonical_hash(origin_instance, origin_conversation_id, origin_message_id, int(revision))[:24]}"


# ---------- stream 授权（服务端钉死；客户端参数只定位不授权） ----------

def _active_grant(conn, binding_id: str, stream_id: str):
    g = conn.execute(
        "SELECT * FROM source_stream_grants WHERE stream_id=?",
        (stream_id,)).fetchone()
    if g is None:
        raise Forbidden("unknown stream", code="FORBIDDEN",
                        stream_id=stream_id)
    if g["revoked_at"] is not None:
        raise Forbidden("stream grant revoked", code="FORBIDDEN",
                        stream_id=stream_id)
    # 归档凭据与 stream 一一对应：别的 binding 拿到 token 也顶不了名
    if g["binding_id"] != binding_id:
        raise Forbidden("stream not bound to this credential",
                        code="FORBIDDEN", stream_id=stream_id)
    return g


def _check_origin(g, origin_instance: str, origin_conversation_id: str,
                  senders: set[str]) -> None:
    if origin_instance != g["origin_instance"] or \
            origin_conversation_id != g["origin_conversation_id"]:
        raise Forbidden(
            "origin instance/conversation 与 stream 授权不符",
            code="FORBIDDEN", stream_id=g["stream_id"])
    allowed = set(json.loads(g["allowed_senders"]))
    bad = sorted(senders - allowed)
    if bad:
        raise Forbidden(
            f"sender 不在 stream 授权内：{bad}", code="FORBIDDEN",
            stream_id=g["stream_id"], senders=bad)


# ---------- 请求归一化与静态校验 ----------

def _normalize_messages(raw: list) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise Forbidden("messages 必须是非空数组", code="INVALID_ARGUMENT")
    if len(raw) > config.SOURCE_LIVE_MAX_MESSAGES:
        raise Forbidden(
            f"单次请求消息数超限（{len(raw)} > "
            f"{config.SOURCE_LIVE_MAX_MESSAGES}）；由宿主 outbox 分批",
            code="SOURCE_SELECTION_TOO_LARGE")
    out = []
    seen: set[tuple[str, int]] = set()
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise Forbidden(f"messages[{i}] 不是对象", code="INVALID_ARGUMENT")
        omid = m.get("origin_message_id")
        rev = m.get("revision")
        if not isinstance(omid, str) or not omid:
            raise Forbidden(f"messages[{i}].origin_message_id 必填",
                            code="INVALID_ARGUMENT")
        if isinstance(rev, bool) or not isinstance(rev, int) or rev < 1:
            raise Forbidden(
                f"messages[{i}].revision 必须是 ≥1 整数",
                code="INVALID_ARGUMENT")
        if (omid, rev) in seen:
            raise Forbidden(
                f"messages[{i}] 同 (origin, revision) 重复出现：{omid} r{rev}",
                code="INVALID_ARGUMENT")
        seen.add((omid, rev))
        prev = m.get("previous_revision")
        if prev is not None and (isinstance(prev, bool)
                                 or not isinstance(prev, int) or prev < 1):
            raise Forbidden(
                f"messages[{i}].previous_revision 必须是 ≥1 整数或 null",
                code="INVALID_ARGUMENT")
        pred = m.get("predecessor")
        if pred is not None:
            if not isinstance(pred, dict):
                raise Forbidden(
                    f"messages[{i}].predecessor 必须是对象或 null",
                    code="INVALID_ARGUMENT")
            pm, pr = pred.get("origin_message_id"), pred.get("revision")
            if not isinstance(pm, str) or not pm or isinstance(pr, bool) \
                    or not isinstance(pr, int) or pr < 1:
                raise Forbidden(
                    f"messages[{i}].predecessor 形状不合法",
                    code="INVALID_ARGUMENT")
        sender = m.get("sender")
        if sender not in _SENDER_MAP:
            raise Forbidden(
                f"messages[{i}].sender 必须是 user/assistant",
                code="INVALID_ARGUMENT")
        kind = m.get("published_kind")
        if kind not in PUBLISHED_KINDS:
            raise Forbidden(
                f"messages[{i}].published_kind 不在允许集合"
                f"{PUBLISHED_KINDS}（草稿/thinking/工具/控制事件不入本投影）",
                code="INVALID_ARGUMENT")
        text = m.get("text")
        if not isinstance(text, str):
            raise Forbidden(f"messages[{i}].text 必须是字符串",
                            code="INVALID_ARGUMENT")
        chash = m.get("content_hash")
        if not isinstance(chash, str) or not chash:
            raise Forbidden(f"messages[{i}].content_hash 必填",
                            code="INVALID_ARGUMENT")
        # 共享契约（冻结）：sha256(正文 UTF-8)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != chash:
            raise Forbidden(
                f"messages[{i}] content_hash 与正文不符（共享规范化契约："
                "sha256(text UTF-8)）",
                code="SOURCE_HASH_MISMATCH", origin_message_id=omid)
        assets = m.get("assets")
        if assets is None:
            assets = []
        if not isinstance(assets, list) or \
                any(not isinstance(a, str) for a in assets):
            raise Forbidden(
                f"messages[{i}].assets 必须是字符串数组（授权引用）",
                code="INVALID_ARGUMENT")
        seq = m.get("conversation_sequence")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise Forbidden(
                f"messages[{i}].conversation_sequence 必须是 ≥0 整数"
                "（宿主 authoritative sequence）", code="INVALID_ARGUMENT")
        times = {}
        for k in ("occurred_at", "received_at", "published_at"):
            v = m.get(k)
            if v is not None and not isinstance(v, str):
                raise Forbidden(f"messages[{i}].{k} 必须是字符串或 null",
                                code="INVALID_ARGUMENT")
            times[k] = v
        # 缺可靠时间留 unknown：显示时间取 occurred→published→received
        out.append({
            "origin_message_id": omid, "revision": rev,
            "previous_revision": prev, "predecessor": pred,
            "conversation_sequence": seq, "sender": sender,
            "published_kind": kind, "text": text, "assets": assets,
            "content_hash": chash, **times})
    return out


def _display_time(m: dict) -> str | None:
    return m.get("occurred_at") or m.get("published_at") or \
        m.get("received_at")


def _occurred_date(m: dict) -> str | None:
    t = _display_time(m)
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(
        ZoneInfo(config.RELATIONSHIP_TIMEZONE)).date().isoformat()


# ---------- 主入口 ----------

def ingest(principal: Principal, a: dict) -> dict:
    op = a.get("operation_id")
    stream_id = a.get("stream_id")
    origin_instance = a.get("origin_instance")
    origin_conv = a.get("origin_conversation_id")
    if not isinstance(op, str) or not op:
        raise Forbidden("operation_id 必填（宿主固定 op，重试沿用）",
                        code="INVALID_ARGUMENT")
    if not isinstance(stream_id, str) or not stream_id:
        raise Forbidden("stream_id 必填", code="INVALID_ARGUMENT")
    if not isinstance(origin_instance, str) or not isinstance(origin_conv, str):
        raise Forbidden("origin_instance/origin_conversation_id 必填",
                        code="INVALID_ARGUMENT")
    messages = _normalize_messages(a.get("messages"))

    # 静态授权（事务内还会按当前撤销态复查——方案 §8）
    with db.formal() as conn:
        g = _active_grant(conn, principal.binding_id, stream_id)
        _check_origin(g, origin_instance, origin_conv,
                      {m["sender"] for m in messages})

    payload = {"operation_id": op, "stream_id": stream_id,
               "origin_instance": origin_instance,
               "origin_conversation_id": origin_conv,
               "messages": messages}
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      default=str).encode("utf-8")
    if len(body) > config.SOURCE_LIVE_MAX_JSON_BYTES:
        raise Forbidden(
            f"规范化 JSON 超 {config.SOURCE_LIVE_MAX_JSON_BYTES} 字节；"
            "由宿主 outbox 分批（50 条/1MiB 工程初值）",
            code="SOURCE_SELECTION_TOO_LARGE")
    content_sha = hashlib.sha256(body).hexdigest()

    # ---- 母本载荷先落盘（不可变、内容寻址）；manifest 在事务成功后
    # 写（契约 §4.3 顺序 + 自审①：被幂等冲突拒绝的尝试不得覆盖成功
    # 批次的 manifest）----
    batch_id = "live-" + hashlib.sha256(op.encode()).hexdigest()[:20]
    # batches.sha256 唯一键口径：(op, 内容) 复合——不同 op 同内容不撞
    # UNIQUE(provider, sha256)，同 op 重放由 op 回执先行短路
    batch_sha = hashlib.sha256(
        (op + ":" + content_sha).encode()).hexdigest()
    payload_path = archive.ensure_payload_bytes(
        PROVIDER, batch_id, body, content_sha)

    def _tx(conn) -> dict:
        # 事务内复查：grant 撤销 / binding 撤销在途变化（fail-closed）
        g = _active_grant(conn, principal.binding_id, stream_id)
        _check_origin(g, origin_instance, origin_conv,
                      {m["sender"] for m in messages})
        b = conn.execute(
            "SELECT revoked FROM client_bindings WHERE binding_id=?",
            (principal.binding_id,)).fetchone()
        if b is None or b["revoked"]:
            raise Forbidden("credential revoked", code="FORBIDDEN")

        conn.execute(
            "INSERT INTO source_import_batches(batch_id, provider,"
            " status, original_filename, original_bytes, sha256,"
            " raw_path, parser_version, import_started_at,"
            " import_finished_at, stats, imported_by, kind)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, PROVIDER, "completed",
             f"live:{op}", len(body), batch_sha, str(payload_path),
             PARSER_VERSION, _now(), _now(),
             json.dumps({"messages": len(messages)}, ensure_ascii=False),
             principal.principal_id, "live_delta"))

        conv_pid = provider_conversation_id_of(origin_instance, origin_conv)
        conv = conn.execute(
            "SELECT id FROM source_conversations WHERE provider=? AND"
            " provider_conversation_id=?", (PROVIDER, conv_pid)).fetchone()
        if conv is None:
            conv_id = f"sc_{_uuid.uuid4().hex[:16]}"
            conn.execute(
                "INSERT INTO source_conversations(id, provider,"
                " provider_conversation_id, title, created_at, updated_at,"
                " message_count, first_import_batch_id,"
                " last_import_batch_id) VALUES(?,?,?,?,?,?,0,?,?)",
                (conv_id, PROVIDER, conv_pid, None, _now(), _now(),
                 batch_id, batch_id))
        else:
            conv_id = conv["id"]

        ack_msgs = []
        for m in messages:
            ack_msgs.append(_ingest_one(
                conn, principal, g, batch_id, conv_id, conv_pid,
                origin_instance, origin_conv, op, m))
        _refresh_aggregates(conn, conv_id)
        audit.record(
            conn, "source.ingest.committed", principal.principal_id,
            resource_id=batch_id,
            payload={"operation_id": op, "stream_id": stream_id,
                     "messages": len(messages)})
        return {"operation_id": op, "committed_at": _now(),
                "messages": ack_msgs, "batch_ref": batch_id,
                "integrity": "verified"}

    ack = atomic_write(principal.principal_id, "source.ingest", op,
                       payload, _tx)
    # manifest 只在成功（含幂等重放）后写——描述最终提交内容
    archive.write_live_manifest(
        PROVIDER, batch_id, body, content_sha,
        {"kind": "live_delta", "operation_id": op, "stream_id": stream_id,
         "imported_by": principal.principal_id})
    return ack


def _ingest_one(conn, principal, grant, batch_id: str, conv_id: str,
                conv_pid: str, origin_instance: str, origin_conv: str,
                op: str, m: dict) -> dict:
    omid, rev = m["origin_message_id"], m["revision"]
    stream_id = grant["stream_id"]
    prior = conn.execute(
        "SELECT * FROM source_live_revisions WHERE stream_id=? AND"
        " origin_message_id=? ORDER BY revision DESC LIMIT 1",
        (stream_id, omid)).fetchone()
    if prior is not None:
        if rev <= prior["revision"]:
            # 旧修订重放：同内容=已入库去重；异内容=修订冲突（拒绝整批）
            exact = conn.execute(
                "SELECT * FROM source_live_revisions WHERE stream_id=? AND"
                " origin_message_id=? AND revision=?",
                (stream_id, omid, rev)).fetchone()
            if exact is not None:
                if exact["content_hash"] == m["content_hash"]:
                    return {"origin_message_id": omid, "revision": rev,
                            "source_conversation_id":
                                exact["source_conversation_id"],
                            "source_message_id": exact["source_message_id"],
                            "content_hash": exact["content_hash"],
                            "deduplicated": True}
                raise Forbidden(
                    f"同 (origin, revision) 已存在不同内容：{omid} r{rev}",
                    code="SOURCE_REVISION_CONFLICT",
                    origin_message_id=omid, revision=rev)
            raise Forbidden(
                f"revision {rev} ≤ 已入库最新 {prior['revision']} 且非"
                f"已存在修订：{omid}",
                code="SOURCE_REVISION_CONFLICT", origin_message_id=omid)
        if rev != prior["revision"] + 1:
            raise Forbidden(
                f"修订链断裂：期望 r{prior['revision'] + 1}，收到 r{rev}"
                f"（{omid}）——前版缺失，宿主按序重试",
                code="SOURCE_NOT_READY", origin_message_id=omid,
                expected_revision=prior["revision"] + 1)
        if m["previous_revision"] != prior["revision"]:
            raise Forbidden(
                f"previous_revision 与库内最新修订不符：{omid}"
                f"（期望 {prior['revision']}，收到 "
                f"{m['previous_revision']}）",
                code="SOURCE_REVISION_CONFLICT", origin_message_id=omid)
    elif m["previous_revision"] is not None:
        raise Forbidden(
            f"首见修订带 previous_revision={m['previous_revision']} 但"
            f"库内无前版：{omid}",
            code="SOURCE_NOT_READY", origin_message_id=omid)

    parent_pid = None
    pred = m["predecessor"]
    if pred is not None:
        prow = conn.execute(
            "SELECT source_message_id, content_hash, source_conversation_id"
            " FROM source_live_revisions WHERE stream_id=? AND"
            " origin_message_id=? AND revision=?",
            (stream_id, pred["origin_message_id"],
             pred["revision"])).fetchone()
        if prow is None:
            raise Forbidden(
                f"predecessor 未入库：{pred['origin_message_id']}"
                f" r{pred['revision']}——同流有序等待，不跳过",
                code="SOURCE_NOT_READY",
                predecessor=pred["origin_message_id"])
        parent_pid = _provider_message_id(conn, prow["source_message_id"])

    normalized_sender, speaker = _SENDER_MAP[m["sender"]]
    msg_pid = provider_message_id_of(origin_instance, origin_conv, omid, rev)
    msg_row_id = f"sm_{_uuid.uuid4().hex[:16]}"
    display_t = _display_time(m)
    conn.execute(
        "INSERT INTO source_messages(id, conversation_id, provider,"
        " provider_conversation_id, provider_message_id, id_synthetic,"
        " parent_provider_message_id, raw_sender, normalized_sender,"
        " speaker, created_at, updated_at, occurred_date, text,"
        " content_json, attachments, has_thinking, has_tool_content,"
        " sequence, import_batch_id, published, content_hash)"
        " VALUES(?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,0,0,?,?,1,?)",
        (msg_row_id, conv_id, PROVIDER, conv_pid, msg_pid, 0, parent_pid,
         normalized_sender, speaker, display_t, display_t,
         _occurred_date(m), m["text"],
         json.dumps({"origin_message_id": omid, "revision": rev,
                     "published_kind": m["published_kind"],
                     "occurred_at": m["occurred_at"],
                     "received_at": m["received_at"],
                     "published_at": m["published_at"],
                     "sender": m["sender"]},
                    ensure_ascii=False),
         json.dumps(m["assets"], ensure_ascii=False),
         m["conversation_sequence"], batch_id, m["content_hash"]))
    # 检索投影只保留最新有效修订：新修订落库 → 旧修订 doc 撤下
    if prior is not None:
        conn.execute(
            "DELETE FROM source_search_docs WHERE message_id=?",
            (prior["source_message_id"],))
        conn.execute(
            "DELETE FROM source_fts WHERE message_id=?",
            (prior["source_message_id"],))
        conn.execute(
            "UPDATE source_messages SET live_superseded=1 WHERE id=?",
            (prior["source_message_id"],))
    if m["text"]:
        text_norm = projection.normalize_search_text(m["text"])
        conn.execute(
            "INSERT INTO source_search_docs(message_id,"
            " provider_message_id, text_norm, text_hash,"
            " projection_version, built_at) VALUES(?,?,?,?,?,?)",
            (msg_row_id, msg_pid, text_norm,
             hashlib.sha256(m["text"].encode()).hexdigest(),
             config.SOURCE_PROJECTION_VERSION, _now()))
        conn.execute(
            "INSERT INTO source_fts(message_id, text_norm) VALUES(?,?)",
            (msg_row_id, text_norm))
    conn.execute(
        "INSERT INTO source_live_revisions(stream_id, origin_message_id,"
        " revision, source_conversation_id, source_message_id,"
        " origin_conversation_id, conversation_sequence,"
        " previous_revision, predecessor_message_id, predecessor_revision,"
        " sender, published_kind, occurred_at, received_at, published_at,"
        " content_hash, operation_id, ingested_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (stream_id, omid, rev, conv_id, msg_row_id, origin_conv,
         m["conversation_sequence"], m["previous_revision"],
         pred["origin_message_id"] if pred else None,
         pred["revision"] if pred else None,
         m["sender"], m["published_kind"], m["occurred_at"],
         m["received_at"], m["published_at"], m["content_hash"],
         op, _now()))
    return {"origin_message_id": omid, "revision": rev,
            "source_conversation_id": conv_id,
            "source_message_id": msg_row_id,
            "content_hash": m["content_hash"]}


def _provider_message_id(conn, source_message_id: str) -> str | None:
    row = conn.execute(
        "SELECT provider_message_id FROM source_messages WHERE id=?",
        (source_message_id,)).fetchone()
    return row["provider_message_id"] if row else None


def _refresh_aggregates(conn, conv_id: str) -> None:
    conn.execute(
        "UPDATE source_conversations SET"
        " message_count=(SELECT COUNT(*) FROM source_messages m WHERE"
        " m.conversation_id=source_conversations.id AND m.published=1"
        " AND m.live_superseded=0),"
        " first_message_at=(SELECT MIN(created_at) FROM source_messages m"
        " WHERE m.conversation_id=source_conversations.id"
        " AND m.published=1 AND m.live_superseded=0),"
        " last_message_at=(SELECT MAX(created_at) FROM source_messages m"
        " WHERE m.conversation_id=source_conversations.id"
        " AND m.published=1 AND m.live_superseded=0),"
        " updated_at=? WHERE id=?",
        (_now(), conv_id))


# ---------- status（只返回获授权的操作） ----------

def ingest_status(principal: Principal, a: dict) -> dict:
    op = a.get("operation_id")
    if not isinstance(op, str) or not op:
        raise Forbidden("operation_id 必填", code="INVALID_ARGUMENT")
    with db.formal() as conn:
        row = conn.execute(
            "SELECT principal_id, result_ref FROM idempotency_records"
            " WHERE capability='source.ingest' AND idempotency_key=?",
            (f"op:{op}",)).fetchone()
    if row is None or (row["principal_id"] != principal.principal_id
                       and principal.principal_id != "qiaosheng"):
        # 无记录只说明尚无提交证据，不推断别的事（契约 §memory.hold 同口径）
        return {"operation_id": op, "completed": False, "receipt": None}
    try:
        receipt = json.loads(row["result_ref"])
    except (ValueError, TypeError):
        receipt = {"replay_ref": row["result_ref"]}
    return {"operation_id": op, "completed": True, "receipt": receipt}


# ---------- stream grant 管理（她的管理入口，不进 registry 白名单面） ----------

def create_grant(binding_id: str, stream_id: str, origin_instance: str,
                 origin_conversation_id: str, allowed_senders: list[str],
                 owner_scope: str, created_by: str) -> dict:
    """登记 stream 授权（管理脚本/维护入口用；不给受限凭据自查）。"""
    bad = [s for s in allowed_senders if s not in _SENDER_MAP]
    if bad or not allowed_senders:
        raise Forbidden(
            f"allowed_senders 必须是 ['user','assistant'] 非空子集",
            code="INVALID_ARGUMENT", senders=bad)
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            b = conn.execute(
                "SELECT 1 FROM client_bindings WHERE binding_id=?",
                (binding_id,)).fetchone()
            if b is None:
                raise NotFound("binding not found", binding_id=binding_id)
            conn.execute(
                "INSERT INTO source_stream_grants(grant_id, stream_id,"
                " origin_instance, origin_conversation_id, allowed_senders,"
                " owner_scope, binding_id, created_by, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (f"ssg_{_uuid.uuid4().hex[:12]}", stream_id,
                 origin_instance, origin_conversation_id,
                 json.dumps(allowed_senders), owner_scope, binding_id,
                 created_by, _now()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"stream_id": stream_id, "binding_id": binding_id,
            "origin_instance": origin_instance,
            "origin_conversation_id": origin_conversation_id,
            "allowed_senders": allowed_senders, "owner_scope": owner_scope}


def revoke_grant(stream_id: str, revoked_by: str) -> dict:
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE source_stream_grants SET revoked_at=? WHERE"
                " stream_id=? AND revoked_at IS NULL", (_now(), stream_id))
            if cur.rowcount == 0:
                raise NotFound("active stream grant not found",
                               stream_id=stream_id)
            audit.record(conn, "source.stream.revoked", revoked_by,
                         resource_id=stream_id, payload={})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"stream_id": stream_id, "revoked": True}
