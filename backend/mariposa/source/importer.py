"""Claude Export 导入管线（复核 v1.1 定点补修版）。

关键不变量（对应复核缺口）：
- SL-02 一次固定快照：输入先复制到受控暂存并原子发布为归档 payload；
  provider 检测与解析只读这份固定字节，绝不重开可变原始路径。
- SL-01 completed 门禁：parse_failures>0、完整性问题、归档 hash 不符都
  阻断 completed（批次 failed，结构化返回，不静默成功）。
- SL-10 失败数据隔离：消息默认 published=0；批次完整校验通过才发布
  （published=1）。检索/列表/绑定默认只见已发布数据。
- SL-07 消息版本：同 UUID 不同内容保留不可变第二版本（版本表），
  当前行不覆盖；sequence 只在会话快照内解释，跨快照序号冲突显式计数。
- 并发认领：running 批次有租约；新鲜租约冲突 409，过期可接管。

内存上界：json_stream 单元素 + 1MB 块；查重集合限单会话预取。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid as _uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import audit, config, db
from ..errors import MariposaError, NotFound
from ..retrieval import projection
from . import adapters, archive, json_stream
from .adapters import claude as claude_adapter
from .adapters.base import NormalizedMessage

PARSER_VERSION = "source_claude_v2"

_INSERT_MSG_SQL = (
    "INSERT INTO source_messages(id, conversation_id, provider,"
    " provider_conversation_id, provider_message_id, id_synthetic,"
    " parent_provider_message_id, raw_sender, normalized_sender, speaker,"
    " created_at, updated_at, occurred_date, text, content_json,"
    " attachments, has_thinking, has_tool_content, sequence,"
    " import_batch_id, published, content_hash)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def open_element_stream(path: Path):
    """打开归档 payload（.json 或导出 .zip）为二进制元素流（带 zip 限额）。"""
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        try:
            infos = zf.infolist()
            if len(infos) > config.SOURCE_MAX_ZIP_MEMBERS:
                raise MariposaError(
                    f"zip member 数超限（{len(infos)} > "
                    f"{config.SOURCE_MAX_ZIP_MEMBERS}）",
                    code="SOURCE_ARCHIVE_LIMIT")
            total = sum(i.file_size for i in infos)
            if total > config.SOURCE_MAX_ZIP_TOTAL_BYTES:
                raise MariposaError("zip 累计解压字节超限",
                                    code="SOURCE_ARCHIVE_LIMIT")
            member, ambiguity = _find_conversations_member(zf)
            if ambiguity:
                raise MariposaError(
                    "zip 中存在多个 conversations.json 候选，无法确定母本",
                    code="SOURCE_AMBIGUOUS_ARCHIVE", members=ambiguity)
            if member is None:
                raise MariposaError(
                    "zip 中未找到 conversations.json（Claude 导出格式不符）",
                    code="SOURCE_FORMAT_UNKNOWN")
            info = zf.getinfo(member)
            if info.file_size > config.SOURCE_MAX_ZIP_MEMBER_BYTES:
                raise MariposaError("zip 单 member 超限",
                                    code="SOURCE_ARCHIVE_LIMIT")
            with zf.open(member) as f:
                yield f
        finally:
            zf.close()
    else:
        with open(path, "rb") as f:
            yield f


def _find_conversations_member(zf: zipfile.ZipFile):
    """精确根路径优先；多个 endswith 候选返回歧义清单。"""
    names = zf.namelist()
    if "conversations.json" in names:
        return "conversations.json", None
    candidates = [n for n in names if n.endswith("conversations.json")]
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, candidates[:10]
    return None, None


def import_file(principal_id: str, path: str,
                filename: str | None = None) -> dict:
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise NotFound("source file not found", path=str(src))

    # ---- 1) 一次固定快照（SL-02）：复制到受控暂存，此后不再读原路径 ----
    staged = archive.stage_input(src)
    try:
        return _import_staged(principal_id, src, staged, filename)
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def _import_staged(principal_id: str, src: Path, staged: Path,
                   filename: str | None) -> dict:
    sha256, size = archive.sha256_file(staged)
    original_name = filename or src.name

    # ---- 2) provider 检测：从固定快照读 ----
    # md 对话转写（裁定 2026-10-04 四）：按源扩展名分流——解析产出
    # 与 Claude JSON 同形的元素契约，后续认领/归档/门禁全链复用
    md_dialect = None
    if src.suffix.lower() in (".md", ".markdown"):
        from . import md_transcript
        try:
            provider, elements = md_transcript.parse(staged, original_name)
            md_dialect = provider
            # 文件名是 md 转写唯一的会话命名来源——元素无标题时补
            for _el in elements:
                if not (_el.get("name") or _el.get("title")):
                    _el["title"] = Path(original_name).stem
            if not elements:
                staged.unlink(missing_ok=True)
                return {"batch_id": None, "provider": provider,
                        "status": "completed", "stats": _new_stats(),
                        "note": "md 转写无可导入会话", "raw_path": None}
        except MariposaError as e:
            _record_failed_import(principal_id, staged, "unknown",
                                  f"bad md: {e}", filename)
            raise
    if md_dialect is None:
        try:
            with open_element_stream(staged) as f:
                first, _ = json_stream.peek_first_element(f)
            if first is json_stream.EMPTY_ARRAY:
                # 空数组：合法输入，0 会话（无数据不建批次）
                staged.unlink(missing_ok=True)
                return {"batch_id": None, "provider": "unknown",
                        "status": "completed", "stats": _new_stats(),
                        "note": "空数组：无可导入会话",
                        "raw_path": None}
            provider = adapters.detect_provider(first)
        except json_stream.JsonStreamError as e:
            _record_failed_import(principal_id, staged, "unknown",
                                  f"bad json: {e}", filename)
            raise MariposaError(f"文件不是合法的顶层 JSON 数组: {e}",
                                code=e.code) from e
        except MariposaError:
            _record_failed_import(principal_id, staged, "unknown",
                                  "unreadable archive", filename)
            raise
        if provider is None:
            _record_failed_import(principal_id, staged, "unknown",
                                  "unrecognized export format", filename)
            raise MariposaError(
                "无法识别导出格式（本轮支持 Claude conversations 导出）",
                code="SOURCE_FORMAT_UNKNOWN")

    # ---- 3) 幂等与并发认领（租约） ----
    batch_id, reused, lease = _claim_batch(provider, sha256, staged,
                                           principal_id, original_name)
    if batch_id is None:  # already_imported
        # CB-028：幂等短路不留完整 staging 副本——重试不得每次多一份
        # 同等大小的 .part（正常路径归档/清理，短路分支同样释放）
        staged.unlink(missing_ok=True)
        return _already_result(provider, sha256)
    if batch_id is False:  # 新鲜 running 冲突
        staged.unlink(missing_ok=True)
        raise MariposaError(
            "同源导入正在进行（running 批次租约内）；请稍后重试或等待"
            "租约过期", code="SOURCE_IMPORT_IN_PROGRESS", sha256=sha256)

    # ---- 4) 原子发布归档；解析/复核只读归档 payload（SL-02） ----
    existing_payload = _existing_payload_if_consistent(provider, batch_id,
                                                       sha256)
    if existing_payload is not None:
        staged.unlink(missing_ok=True)
        archived = existing_payload
    else:
        archived = archive.publish_snapshot(
            provider, batch_id, staged, sha256, size, original_name,
            principal_id)
    _set_raw_path(batch_id, str(archived), original_name)

    stats = _new_stats()
    try:
        # ---- 5) 解析归档 payload（绝不碰原始路径） ----
        try:
            _parse_all(archived, provider, batch_id, stats, lease,
                       md_dialect=md_dialect,
                       original_name=original_name)
        except json_stream.JsonStreamError as e:
            # 检测通过但流中后段不合规（如尾随垃圾）：同源码严格拒绝
            _fail_batch(batch_id, provider, f"bad json: {e}", stats,
                         lease)
            raise MariposaError(f"文件不是合法的顶层 JSON 数组: {e}",
                                code=e.code) from e
        _refresh_conversation_aggregates(batch_id, published_only=False)
        verification = _verify_integrity(provider, batch_id)
        archive_check = archive.verify_archived(provider, batch_id, sha256)
        problems = list(verification["problems"])
        if not archive_check.get("ok"):
            problems.append({"check": "raw_archive",
                             **{k: v for k, v in archive_check.items()
                                if k != "ok"}})
        stats["verify_problems"] = problems

        # ---- 6) completed 门禁（SL-01）：真实校验结果决定状态 ----
        blocking = []
        if stats["parse_failures"] > 0:
            blocking.append(
                {"check": "parse_failures",
                 "count": stats["parse_failures"]})
        if problems:
            blocking += problems
        if blocking:
            stats["blocking"] = blocking
            _fail_batch(batch_id, provider,
                        "导入未通过完整性门禁：" + json.dumps(
                            blocking, ensure_ascii=False)[:1500], stats,
                        lease)
            return {"batch_id": batch_id, "provider": provider,
                    "status": "failed", "stats": stats,
                    "error": "integrity gate", "raw_path": str(archived)}

        # ---- 7) 发布可见性（SL-10 + A09 + 审计 F11）：发布以内容身份为
        # 单位——只有行 content_hash 与本批快照成员 hash 一致（本批 raw
        # 归档确实包含该正文）才发布并前移 import_batch_id。
        # 失败批次遗留的同 UUID 旧正文行若与本批内容不同，保持
        # unpublished 且来源批次不动：不得把旧正文包装成"来自本成功批次"。----
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _assert_lease(conn, batch_id, lease)  # P1-03 fencing
                members = conn.execute(
                    "SELECT m.provider_message_id, m.content_hash,"
                    " c.provider_conversation_id FROM"
                    " source_snapshot_members m"
                    " JOIN source_conversation_snapshots s"
                    " ON s.snapshot_id=m.snapshot_id"
                    " JOIN source_conversations c"
                    " ON c.id=s.conversation_id WHERE s.batch_id=?",
                    (batch_id,)).fetchall()
                published = 0
                publish_mismatch = 0
                for member in members:
                    # N10：发布条件包含会话归属——同 UUID 同正文但挂在
                    # 其他会话下的行不得迁移到不含该会话的成功母本
                    cur = conn.execute(
                        "UPDATE source_messages SET published=1,"
                        " import_batch_id=? WHERE provider=?"
                        " AND provider_message_id=?"
                        " AND provider_conversation_id=?"
                        " AND published=0 AND content_hash=?",
                        (batch_id, provider,
                         member["provider_message_id"],
                         member["provider_conversation_id"],
                         member["content_hash"]))
                    published += cur.rowcount
                    if cur.rowcount == 0:
                        cur_row = conn.execute(
                            "SELECT published FROM source_messages"
                            " WHERE provider=? AND provider_message_id=?",
                            (provider, member["provider_message_id"])
                        ).fetchone()
                        # 已发布同内容（UPDATE 天然不匹配）不是 mismatch
                        if cur_row is not None and cur_row["published"] == 0:
                            publish_mismatch += 1
                if publish_mismatch:
                    stats["publish_skipped_content_mismatch"] = \
                        publish_mismatch
                # CB-027：会话聚合与发布同一事务——聚合只数 published，
                # 此前预刷发生在发布前（全 0），成功发布后不再刷新，
                # conversation.get 显示 0 条而 messages 有 N 条
                conn.execute(
                    "UPDATE source_conversations SET"
                    " message_count=(SELECT COUNT(*) FROM source_messages m"
                    "  WHERE m.conversation_id=source_conversations.id"
                    "  AND m.published=1),"
                    " first_message_at=(SELECT MIN(created_at) FROM"
                    "  source_messages m WHERE"
                    " m.conversation_id=source_conversations.id"
                    " AND m.published=1),"
                    " last_message_at=(SELECT MAX(created_at) FROM"
                    "  source_messages m WHERE"
                    " m.conversation_id=source_conversations.id"
                    " AND m.published=1)"
                    " WHERE last_import_batch_id=?", (batch_id,))
                conn.execute(
                    "UPDATE source_import_batches SET status='completed',"
                    " import_finished_at=?, stats=?, error=NULL"
                    " WHERE batch_id=?",
                    (_now(), json.dumps(stats, ensure_ascii=False), batch_id))
                audit.record(conn, "source.import.completed", principal_id,
                             resource_id=batch_id,
                             payload={"provider": provider,
                                      "conversations":
                                          stats["conversations_total"],
                                      "messages_new": stats["messages_new"],
                                      "published": published,
                                      "publish_skipped_content_mismatch":
                                          publish_mismatch})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        # A12：批次状态已在上方事务内定稿；metadata 只是附属落盘，
        # 失败不回写 failed（否则出现 failed+published=1 的矛盾态）
        try:
            archive.write_metadata(provider, batch_id, {
                "status": "completed", "stats": stats,
                "verification": verification})
        except OSError as meta_err:
            archive.write_metadata(provider, batch_id, {
                "status": "completed", "stats": stats,
                "metadata_note": f"metadata write retried: {meta_err}"})
        return _slim_result({
            "batch_id": batch_id, "provider": provider,
            "status": "completed", "stats": stats,
            "verification": {**verification, "archive": archive_check},
            "raw_path": str(archived)})
    except Exception as e:  # noqa: BLE001 —— 未预期异常必须留失败痕迹
        if isinstance(e, LeaseLost):
            raise  # 本 worker 已被接管：不留痕不清理
        try:
            _fail_batch(batch_id, provider, f"{type(e).__name__}: {e}",
                        stats, lease)
        except LeaseLost:
            pass  # 定稿瞬间被接管：接管方负责后续
        raise


class LeaseLost(Exception):
    """当前 worker 持有的租约已被接管（P1-03）：任何写入——包括失败
    清场与 metadata 定稿——必须放弃，不得破坏接管方的批次状态与
    provenance 快照。"""


def _assert_lease(conn, batch_id: str, lease_token: str) -> None:
    """写事务内校验租约归属；失配/批已 completed → LeaseLost。"""
    row = conn.execute(
        "SELECT lease_token, status FROM source_import_batches"
        " WHERE batch_id=?", (batch_id,)).fetchone()
    if row is None or row["lease_token"] != lease_token:
        raise LeaseLost(f"lease lost for batch {batch_id}")
    if row["status"] == "completed":
        raise LeaseLost(f"batch {batch_id} already completed by "
                        "takeover worker")


def _new_lease_token() -> str:
    return f"lease_{_uuid.uuid4().hex[:16]}"


def _claim_batch(provider: str, sha256: str, staged: Path,
                 principal_id: str, original_name: str):
    """返回 (batch_id, reused, lease_token)；
    batch_id None=已导入幂等返回；False=租约冲突。"""
    now = datetime.now(timezone.utc)
    lease_cutoff = now - timedelta(minutes=config.SOURCE_IMPORT_LEASE_MINUTES)
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM source_import_batches WHERE provider=?"
                " AND sha256=?", (provider, sha256)).fetchone()
            if existing:
                if existing["status"] == "completed":
                    conn.execute("COMMIT")
                    return None, False, None
                started_raw = existing["import_started_at"] or ""
                started_dt = _parse_ts(started_raw)
                if (existing["status"] == "running" and started_dt is not None
                        and started_dt > lease_cutoff):
                    conn.execute("COMMIT")
                    return False, False, None
                batch_id = existing["batch_id"]  # failed / stale running
                lease = _new_lease_token()  # 接管 = 新 fencing token
                conn.execute(
                    "UPDATE source_import_batches SET status='running',"
                    " error=NULL, imported_by=?, import_started_at=?,"
                    " lease_token=? WHERE batch_id=?",
                    (principal_id, now.isoformat(), lease, batch_id))
                conn.execute("COMMIT")
                return batch_id, True, lease
            batch_id = f"sib_{_uuid.uuid4().hex[:12]}"
            lease = _new_lease_token()
            conn.execute(
                "INSERT INTO source_import_batches(batch_id, provider,"
                " status, original_filename, original_bytes, sha256,"
                " raw_path, parser_version, import_started_at, imported_by,"
                " lease_token) VALUES(?,?, 'running', ?,?,?,?,?,?,?,?)",
                (batch_id, provider, original_name, staged.stat().st_size,
                 sha256, "", PARSER_VERSION, now.isoformat(), principal_id,
                 lease))
            conn.execute("COMMIT")
            return batch_id, False, lease
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _parse_ts(raw: str):
    """容忍 ISO 与 SQLite datetime('now') 两种格式；失败返回 None。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _existing_payload_if_consistent(provider: str, batch_id: str,
                                    sha256: str) -> Path | None:
    """接管批次时：既有归档存在且 hash 一致则复用，不一致拒绝。"""
    check = archive.verify_archived(provider, batch_id, sha256)
    if check.get("ok"):
        return archive.batch_dir(provider, batch_id) / check["archived_as"]
    if check.get("issue") in ("sha256_mismatch",):
        raise MariposaError(
            "既有归档与本次输入 hash 不一致；拒绝覆盖，需人工处理",
            code="SOURCE_ARCHIVE_MISMATCH", batch_id=batch_id)
    return None  # 无归档（新批次/上次归档前失败）→ 正常发布


def _slim_result(result: dict) -> dict:
    """导入结果出站瘦身（裁定 2026-10-04 四：省 token）。

    完整 stats/verification 留在库内（批次表 + metadata），出站只给
    必要可读信息；失败时完整 blocking/parse_failures 保留（排障要读）。
    """
    if result.get("status") != "completed":
        return {k: v for k, v in result.items() if k != "raw_path"}
    st = result.get("stats") or {}
    # 计数字段全集保留（个位数 int，成本可忽略）；删的是
    # min/max_created_at、verify/sample/复核类诊断块与 raw_path
    slim_stats = {k: v for k, v in st.items()
                  if isinstance(v, (int, float)) and not k.startswith("_")}
    vf = result.get("verification") or {}
    if vf.get("problems"):
        slim_stats["verify_problems"] = vf["problems"]
    vf = result.get("verification") or {}
    slim = {
        "batch_id": result.get("batch_id"),
        "provider": result.get("provider"),
        "status": "completed",
        "stats": slim_stats,
        "verified": bool(vf.get("ok")),
    }
    return slim


def _already_result(provider: str, sha256: str) -> dict:
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM source_import_batches WHERE provider=?"
            " AND sha256=?", (provider, sha256)).fetchone()
    return _slim_result({
        "batch_id": row["batch_id"], "provider": provider,
        "status": "already_imported",
        "stats": json.loads(row["stats"] or "{}"),
        "raw_path": row["raw_path"]})


def batch_status(batch_id: str) -> dict:
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM source_import_batches WHERE batch_id=?",
            (batch_id,)).fetchone()
    if row is None:
        raise NotFound("source import batch not found", batch_id=batch_id)
    out = dict(row)
    out["stats"] = json.loads(out.get("stats") or "{}")
    return out


def batches_list(limit: int = 50) -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT batch_id, provider, status, original_filename,"
            " import_started_at, import_finished_at, error"
            " FROM source_import_batches"
            " ORDER BY import_started_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),)).fetchall()
    # 出站瘦身 + 分钟精度（裁定 2026-10-04 四）
    from .query import _fmt_min
    out = []
    for r in rows:
        d = dict(r)
        d["import_started_at"] = _fmt_min(d.get("import_started_at"))
        d["import_finished_at"] = _fmt_min(d.get("import_finished_at"))
        if d.get("status") == "completed" and not d.get("error"):
            d.pop("error", None)
        out.append(d)
    return out


# ---------------------------------------------------------------- 解析

def _new_stats() -> dict:
    return {
        "conversations_total": 0, "conversations_new": 0,
        "conversations_existing": 0, "conversations_empty": 0,
        "parse_failures": 0,
        "messages_total": 0, "messages_new": 0,
        "messages_skipped_existing": 0, "messages_duplicate_in_file": 0,
        "messages_missing_uuid": 0,
        "sender_human": 0, "sender_assistant": 0, "sender_system": 0,
        "sender_tool": 0, "sender_unknown": 0,
        "messages_with_text": 0, "messages_with_thinking": 0,
        "messages_with_tool_content": 0, "messages_with_attachments": 0,
        "min_created_at": None, "max_created_at": None,
        "version_conflicts": 0, "sequence_conflicts": 0,
        "verify_problems": [],
    }


def _parse_all(payload: Path, provider: str, batch_id: str,
               stats: dict, lease: str, md_dialect: str | None = None,
               original_name: str | None = None) -> None:
    if md_dialect is not None:
        # md 转写：归档母本为 md 原文，按方言解析出同形元素契约。
        # 会话标题以原始文件名为准（归档名是 payload-<sha>，不可当标题）
        from . import md_transcript
        _, elements = md_transcript.parse(
            payload, original_name or payload.name)
        for _el in elements:
            if not (_el.get("name") or _el.get("title")):
                _el["title"] = Path(original_name or payload.name).stem
        for idx, element in enumerate(elements):
            _consume_element(element, provider, batch_id, idx, stats,
                             lease)
        stats["conversations_total"] = len(elements)
        return
    with open_element_stream(payload) as f:
        first, it = json_stream.peek_first_element(f)
        idx = 0
        if first is not None:
            _consume_element(first, provider, batch_id, idx, stats, lease)
            idx += 1
            for element in it:
                _consume_element(element, provider, batch_id, idx, stats,
                                 lease)
                idx += 1
    stats["conversations_total"] = idx


def _consume_element(element: Any, provider: str, batch_id: str,
                     index: int, stats: dict, lease: str) -> None:
    mod = adapters.module_for(provider)
    # 元素级 schema 校验（SL-09/01）：42/字符串/null/异构 dict 都不许伪装
    try:
        if not mod.detect(element):
            raise ValueError(f"element {index} is not a {provider} conversation")
        draft = mod.normalize_conversation(element)
    except Exception:
        stats["parse_failures"] += 1
        return
    if draft.id_synthetic:
        draft.provider_conversation_id = mod.synthetic_conversation_id(
            batch_id, index)
    if not draft.messages:
        stats["conversations_empty"] += 1

    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _assert_lease(conn, batch_id, lease)  # P1-03 fencing
            conv_row_id = _ensure_conversation(conn, provider, batch_id,
                                               draft, stats, index)
            if conv_row_id is None:
                stats["parse_failures"] += 1
                conn.execute("COMMIT")
                return
            snapshot_id = _ensure_snapshot(conn, batch_id, conv_row_id,
                                           draft)
            prev_seq_map = _latest_published_snapshot_members(
                conn, conv_row_id, exclude_batch=batch_id)
            existing = {
                r["provider_message_id"]: r["content_hash"] for r in conn.execute(
                    "SELECT provider_message_id, content_hash FROM"
                    " source_messages WHERE provider=? AND"
                    " provider_conversation_id=?",
                    (provider, draft.provider_conversation_id))}
            local_members: dict[int, str] = {}
            local_pids: set[str] = set()  # 本会话内已处理（有界：单会话）
            try:
                for msg in mod.normalize_messages(draft, batch_id):
                    stats["messages_total"] += 1
                    stats[f"sender_{msg.normalized_sender}"] += 1
                    if msg.id_synthetic:
                        stats["messages_missing_uuid"] += 1
                    if msg.text:
                        stats["messages_with_text"] += 1
                    if msg.has_thinking:
                        stats["messages_with_thinking"] += 1
                    if msg.has_tool_content:
                        stats["messages_with_tool_content"] += 1
                    if msg.attachments:
                        stats["messages_with_attachments"] += 1
                    _track_time_bounds(msg, stats)

                    chash = _content_hash(msg)
                    pid = msg.provider_message_id
                    # sequence 只在本快照内解释；重复序号映射不同消息、
                    # 或与最近成功快照同序号不同消息 → 显式计冲突（SL-07）
                    if msg.sequence in local_members and \
                            local_members[msg.sequence] != pid:
                        stats["sequence_conflicts"] += 1
                    local_members[msg.sequence] = pid
                    if msg.sequence in prev_seq_map and \
                            prev_seq_map[msg.sequence] != pid:
                        stats["sequence_conflicts"] += 1
                    # 同文件内同 UUID 重复出现：行与快照成员一律首见定格
                    #（F11：成员 hash 必须与实际保留的行内容一致，
                    # 否则发布门禁会把首版正文误判为内容不符）
                    conn.execute(
                        "INSERT INTO source_snapshot_members"
                        "(snapshot_id, provider_message_id, sequence,"
                        " content_hash) VALUES(?,?,?,?)"
                        " ON CONFLICT(snapshot_id, provider_message_id)"
                        " DO NOTHING",
                        (snapshot_id, pid, msg.sequence, chash))

                    if pid in local_pids:
                        # 同一文件内同 UUID 已出现过：保留首个（母本可查）
                        stats["messages_duplicate_in_file"] += 1
                        if existing.get(pid) != chash:
                            stats["version_conflicts"] += 1
                            _record_version(conn, provider, pid, chash,
                                            batch_id)
                        continue
                    if pid in existing:
                        if existing[pid] is None:
                            # A13（F11 修正）：旧数据无内容身份 → 用行自身
                            # 内容重算身份回填；与本批 hash 是否一致交给
                            # 下面的比较判定，不把本批 hash 直接贴给旧行
                            old_row = conn.execute(
                                "SELECT * FROM source_messages WHERE"
                                " provider=? AND provider_message_id=?",
                                (provider, pid)).fetchone()
                            row_hash = _row_content_hash(old_row)
                            conn.execute(
                                "UPDATE source_messages SET content_hash=?"
                                " WHERE provider=? AND provider_message_id=?",
                                (row_hash, provider, pid))
                            existing[pid] = row_hash
                            stats["messages_hash_backfilled"] = \
                                stats.get("messages_hash_backfilled", 0) + 1
                        if existing[pid] == chash:
                            stats["messages_skipped_existing"] += 1
                        else:
                            # SL-07：同 UUID 新内容 → 不可变版本留档，
                            # 当前行不覆盖、不静默跳过
                            stats["version_conflicts"] += 1
                            _record_version(conn, provider, pid, chash,
                                            batch_id)
                        local_pids.add(pid)
                        continue
                    try:
                        _insert_message(conn, provider, conv_row_id,
                                        batch_id, msg, chash)
                    except sqlite3.IntegrityError:
                        stats["messages_duplicate_in_file"] += 1
                        continue
                    existing[pid] = chash
                    local_pids.add(pid)
                    _record_version(conn, provider, pid, chash, batch_id)
                    stats["messages_new"] += 1
            except ValueError as e:
                # 消息级违规（非对象/超限）：计 parse failure，中止本会话
                stats["parse_failures"] += 1
                _note_parse_failure(conn, batch_id, index, str(e)[:200])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _note_parse_failure(conn, batch_id: str, index: int, reason: str) -> None:
    """元素级失败位置留痕（audit 事件；不含正文内容）。"""
    audit.record(conn, "source.parse.failure", "system",
                 resource_id=batch_id,
                 payload={"conversation_index": index,
                          "reason": reason[:200]})


def _ensure_conversation(conn, provider: str, batch_id: str, draft,
                         stats: dict, index: int) -> str | None:
    row = conn.execute(
        "SELECT id FROM source_conversations"
        " WHERE provider=? AND provider_conversation_id=?",
        (provider, draft.provider_conversation_id)).fetchone()
    if row:
        stats["conversations_existing"] += 1
        conn.execute(
            "UPDATE source_conversations SET last_import_batch_id=?"
            " WHERE id=?", (batch_id, row["id"]))
        return row["id"]
    stats["conversations_new"] += 1
    conv_id = f"sc_{_uuid.uuid4().hex[:14]}"
    conn.execute(
        "INSERT INTO source_conversations(id, provider,"
        " provider_conversation_id, title, created_at, updated_at,"
        " first_import_batch_id, last_import_batch_id) VALUES(?,?,?,?,?,?,?,?)",
        (conv_id, provider, draft.provider_conversation_id, draft.title,
         draft.created_at, draft.updated_at, batch_id, batch_id))
    return conv_id


def _ensure_snapshot(conn, batch_id: str, conv_row_id: str,
                     draft) -> str:
    """会话快照：每次导入一个观察（title/时间元数据不再永久停留首见）。"""
    row = conn.execute(
        "SELECT snapshot_id FROM source_conversation_snapshots"
        " WHERE conversation_id=? AND batch_id=?",
        (conv_row_id, batch_id)).fetchone()
    if row:
        return row["snapshot_id"]
    snapshot_id = f"snap_{_uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO source_conversation_snapshots(snapshot_id,"
        " conversation_id, batch_id, title, observed_created_at,"
        " observed_updated_at, message_count, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (snapshot_id, conv_row_id, batch_id, draft.title,
         draft.created_at, draft.updated_at, len(draft.messages), _now()))
    return snapshot_id


def _latest_published_snapshot_members(conn, conv_row_id: str,
                                       exclude_batch: str) -> dict[int, str]:
    """最近一次成功批次的快照成员 {sequence: provider_message_id}。"""
    snap = conn.execute(
        "SELECT s.snapshot_id FROM source_conversation_snapshots s"
        " JOIN source_import_batches b ON b.batch_id=s.batch_id"
        " WHERE s.conversation_id=? AND b.status='completed'"
        " AND s.batch_id<>? ORDER BY s.created_at DESC LIMIT 1",
        (conv_row_id, exclude_batch)).fetchone()
    if snap is None:
        return {}
    return {r["sequence"]: r["provider_message_id"] for r in conn.execute(
        "SELECT provider_message_id, sequence FROM source_snapshot_members"
        " WHERE snapshot_id=?", (snap["snapshot_id"],))}


def _content_hash(msg: NormalizedMessage) -> str:
    """规范化内容身份（SL-07 判定同 UUID 内容是否变化）。"""
    basis = json.dumps({
        "raw_sender": msg.raw_sender,
        "normalized_sender": msg.normalized_sender,
        "created_at": msg.created_at, "updated_at": msg.updated_at,
        "parent": msg.parent_provider_message_id,
        "text": msg.text, "content": msg.content_json,
        "attachments": msg.attachments,
        "has_thinking": msg.has_thinking,
        "has_tool_content": msg.has_tool_content,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _row_content_hash(row) -> str:
    """从 source_messages 行重算与 _content_hash 同构的内容身份。

    审计 F11（A13 回填修正）：旧行缺 content_hash 时，回填的必须是
    行自身内容的身份，而不是本批观察到的 hash——否则文本不同的旧行
    会被贴上"本批已验证"的标签，provenance 造假。
    """
    basis = json.dumps({
        "raw_sender": row["raw_sender"],
        "normalized_sender": row["normalized_sender"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "parent": row["parent_provider_message_id"],
        "text": row["text"],
        # N09：与 _content_hash 严格同构——msg.content_json 本就是字符串
        #（adapters.base.NormalizedMessage.content_json: str|None），不做
        # 对象化往返；attachments 是 list，DB JSON 往返后经 outer
        # sort_keys 序列化等价
        "content": row["content_json"],
        "attachments": json.loads(row["attachments"] or "[]"),
        "has_thinking": bool(row["has_thinking"]),
        "has_tool_content": bool(row["has_tool_content"]),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _record_version(conn, provider: str, pid: str, chash: str,
                    batch_id: str) -> None:
    vid = "smv_" + hashlib.sha256(
        (provider + pid + chash).encode()).hexdigest()[:20]
    conn.execute(
        "INSERT OR IGNORE INTO source_message_versions(version_id, provider,"
        " provider_message_id, content_hash, observed_batch_id, observed_at)"
        " VALUES(?,?,?,?,?,?)",
        (vid, provider, pid, chash, batch_id, _now()))


def _insert_message(conn, provider: str, conv_row_id: str, batch_id: str,
                    msg: NormalizedMessage, chash: str) -> None:
    speaker = claude_adapter.speaker_of(msg.normalized_sender)
    msg_row_id = f"sm_{_uuid.uuid4().hex[:16]}"
    conn.execute(_INSERT_MSG_SQL, (
        msg_row_id, conv_row_id, provider,
        msg.provider_conversation_id, msg.provider_message_id,
        1 if msg.id_synthetic else 0, msg.parent_provider_message_id,
        msg.raw_sender, msg.normalized_sender, speaker,
        msg.created_at, msg.updated_at, msg.occurred_date,
        msg.text, msg.content_json,
        json.dumps(msg.attachments, ensure_ascii=False),
        1 if msg.has_thinking else 0, 1 if msg.has_tool_content else 0,
        msg.sequence, batch_id, chash))
    if msg.text and msg.normalized_sender in ("human", "assistant"):
        text_norm = projection.normalize_search_text(msg.text)
        conn.execute(
            "INSERT INTO source_search_docs(message_id, provider_message_id,"
            " text_norm, text_hash, projection_version, built_at)"
            " VALUES(?,?,?,?,?,?)",
            (msg_row_id, msg.provider_message_id, text_norm,
             hashlib.sha256(msg.text.encode()).hexdigest(),
             config.SOURCE_PROJECTION_VERSION, _now()))
        conn.execute(
            "INSERT INTO source_fts(message_id, text_norm) VALUES(?,?)",
            (msg_row_id, text_norm))


def _track_time_bounds(msg, stats: dict) -> None:
    dt = claude_adapter.parse_created_at(msg.created_at)
    if dt is None:
        return
    iso = dt.isoformat()
    if stats["min_created_at"] is None or iso < stats["min_created_at"]:
        stats["min_created_at"] = iso
    if stats["max_created_at"] is None or iso > stats["max_created_at"]:
        stats["max_created_at"] = iso


def _refresh_conversation_aggregates(batch_id: str,
                                     published_only: bool = True) -> None:
    # F13：聚合口径固定为 published（失败批次未发布消息不计入会话条数）
    with db.formal() as conn:
        conn.execute(
            "UPDATE source_conversations SET"
            " message_count=(SELECT COUNT(*) FROM source_messages m"
            "  WHERE m.conversation_id=source_conversations.id"
            "  AND m.published=1),"
            " first_message_at=(SELECT MIN(created_at) FROM"
            "  source_messages m WHERE"
            " m.conversation_id=source_conversations.id"
            " AND m.published=1),"
            " last_message_at=(SELECT MAX(created_at) FROM"
            "  source_messages m WHERE"
            " m.conversation_id=source_conversations.id"
            " AND m.published=1)"
            " WHERE last_import_batch_id=?", (batch_id,))


# ---------------------------------------------------------------- 校验

def _verify_integrity(provider: str, batch_id: str) -> dict:
    """导入后完整性校验（SL-01/11）：问题真实计算并阻断 completed。"""
    problems: list[dict] = []
    with db.formal() as conn:
        # SL-11：speaker NULL 必须检出（IS NOT 对 NULL 语义正确）
        bad_speaker = conn.execute(
            "SELECT COUNT(*) AS c FROM source_messages WHERE provider=?"
            " AND import_batch_id=? AND ("
            " (normalized_sender='human' AND speaker IS NOT 'qiaosheng') OR"
            " (normalized_sender='assistant' AND speaker IS NOT 'jiaming')"
            " OR (normalized_sender NOT IN ('human','assistant')"
            "  AND speaker IS NOT NULL))", (provider, batch_id)
            ).fetchone()["c"]
        if bad_speaker:
            problems.append({"check": "speaker_mapping",
                             "violations": bad_speaker})

        text_no_sender = conn.execute(
            "SELECT COUNT(*) AS c FROM source_messages WHERE provider=?"
            " AND import_batch_id=? AND"
            " text<>'' AND normalized_sender NOT IN ('human','assistant')",
            (provider, batch_id)).fetchone()["c"]
        if text_no_sender:
            problems.append({"check": "text_only_for_human_assistant",
                             "violations": text_no_sender})

        dup_ids = conn.execute(
            "SELECT COUNT(*) AS c FROM (SELECT provider_message_id"
            " FROM source_messages WHERE provider=? AND import_batch_id=?"
            " GROUP BY provider_message_id HAVING COUNT(*)>1)",
            (provider, batch_id)).fetchone()["c"]
        if dup_ids:
            problems.append({"check": "duplicate_provider_message_id",
                             "violations": dup_ids})

        fts_docs = conn.execute(
            "SELECT COUNT(*) AS c FROM source_search_docs d"
            " JOIN source_messages m ON m.id=d.message_id"
            " WHERE m.provider=? AND m.import_batch_id=? AND m.text<>''",
            (provider, batch_id)).fetchone()["c"]
        texts = conn.execute(
            "SELECT COUNT(*) AS c FROM source_messages"
            " WHERE provider=? AND import_batch_id=? AND text<>''",
            (provider, batch_id)).fetchone()["c"]
        if fts_docs != texts:
            problems.append({"check": "search_docs_coverage",
                             "docs": fts_docs, "texts": texts})

        bad_dates = conn.execute(
            "SELECT COUNT(*) AS c FROM source_messages WHERE provider=?"
            " AND import_batch_id=? AND"
            " occurred_date IS NULL AND created_at IS NOT NULL",
            (provider, batch_id)).fetchone()["c"]
        if bad_dates:
            problems.append({"check": "occurred_date_coverage",
                             "violations": bad_dates})

        # 投影 hash 抽样（真实校验，非写死断言）
        sample = conn.execute(
            "SELECT d.text_hash, m.text FROM source_search_docs d"
            " JOIN source_messages m ON m.id=d.message_id"
            " WHERE m.provider=? AND m.import_batch_id=?"
            " ORDER BY d.built_at DESC LIMIT 50",
            (provider, batch_id)).fetchall()
        bad_hash = sum(
            1 for r in sample
            if hashlib.sha256((r["text"] or "").encode()).hexdigest()
            != r["text_hash"])
        if bad_hash:
            problems.append({"check": "projection_hash",
                             "violations": bad_hash, "sampled": len(sample)})

        # 已发布消息必须属于 completed 批次（当前批次除外：接管中的
        # running 批次本批消息保持 published=1 是合法中间态）
        orphan_published = conn.execute(
            "SELECT COUNT(*) AS c FROM source_messages m LEFT JOIN"
            " source_import_batches b ON b.batch_id=m.import_batch_id"
            " WHERE m.published=1 AND m.import_batch_id<>? AND"
            " (b.batch_id IS NULL OR b.status<>'completed')",
            (batch_id,)).fetchone()["c"]
        if orphan_published:
            problems.append({"check": "published_batch_consistency",
                             "violations": orphan_published})

        parent_kept = conn.execute(
            "SELECT COUNT(*) AS c FROM source_messages WHERE provider=?"
            " AND import_batch_id=? AND"
            " parent_provider_message_id IS NOT NULL",
            (provider, batch_id)).fetchone()["c"]
        unknown_with_speaker = conn.execute(
            "SELECT COUNT(*) AS c FROM source_messages WHERE provider=?"
            " AND import_batch_id=? AND"
            " normalized_sender='unknown' AND speaker IS NOT NULL",
            (provider, batch_id)).fetchone()["c"]
        human_speaker_null = conn.execute(
            "SELECT COUNT(*) AS c FROM source_messages WHERE provider=?"
            " AND import_batch_id=? AND"
            " normalized_sender='human' AND speaker IS NULL",
            (provider, batch_id)).fetchone()["c"]
    return {
        "ok": not problems,
        "problems": problems,
        "parent_messages_kept": parent_kept,
        "sample_checks": {
            "human_speaker_null": human_speaker_null,
            "unknown_with_speaker": unknown_with_speaker,
            "projection_hash_sampled": len(sample),
        },
    }


# ---------------------------------------------------------------- 批次簿记

def _set_raw_path(batch_id: str, raw_path: str, original_name: str) -> None:
    with db.formal() as conn:
        conn.execute(
            "UPDATE source_import_batches SET raw_path=?, original_filename=?"
            " WHERE batch_id=?", (raw_path, original_name, batch_id))


def _purge_failed_batch_artifacts(conn, batch_id: str) -> int:
    """失败批次清场（产品语义 2026-09-29 江乔生裁定：导入失败不留下
    任何会话消息数据）。

    只清理"本批新写且未发布"的行：消息、检索投影、快照与成员、清场
    后不再挂任何消息的空壳会话。保留：raw 归档母本、不可变版本档案
    （source_message_versions，SL-07）、审计事件。本批写入前已存在的
    其他批次数据（含已发布行）不受影响。
    """
    # F06（2026-10-03 审计 P1）：先记下本批观察过的会话——快照删除
    # 后就失去"本批碰过哪些会话"的依据；空壳清场只针对这些会话，
    # 不得全库 DELETE（历史成功导入的合法空会话挂着不可变快照的
    # FK，全库清会 IntegrityError 且毁掉合法数据）
    batch_conv_ids = [r["id"] for r in conn.execute(
        "SELECT DISTINCT s.conversation_id AS id FROM"
        " source_conversation_snapshots s WHERE s.batch_id=?",
        (batch_id,))]
    unpublished = conn.execute(
        "SELECT id FROM source_messages WHERE import_batch_id=?"
        " AND published=0", (batch_id,)).fetchall()
    ids = [r["id"] for r in unpublished]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM source_fts WHERE message_id IN ({marks})",
                     ids)
        conn.execute(
            f"DELETE FROM source_search_docs WHERE message_id IN ({marks})",
                     ids)
    conn.execute(
        "DELETE FROM source_messages WHERE import_batch_id=?"
        " AND published=0", (batch_id,))
    conn.execute(
        "DELETE FROM source_snapshot_members WHERE snapshot_id IN ("
        " SELECT snapshot_id FROM source_conversation_snapshots"
        " WHERE batch_id=?)", (batch_id,))
    conn.execute(
        "DELETE FROM source_conversation_snapshots WHERE batch_id=?",
        (batch_id,))
    # 空壳会话：仅本批观察过、清场后不挂任何消息、且没有其他批次
    # 快照引用的会话才删——历史合法空会话（含仅空会话成功导入）受
    # 不可变快照保护，不属于本批清场范围
    if batch_conv_ids:
        conv_marks = ",".join("?" * len(batch_conv_ids))
        conn.execute(
            f"DELETE FROM source_conversations WHERE id IN ({conv_marks})"
            " AND id NOT IN (SELECT DISTINCT conversation_id FROM"
            " source_messages)"
            " AND id NOT IN (SELECT DISTINCT conversation_id FROM"
            " source_conversation_snapshots)",
            batch_conv_ids)
    return len(ids)


def _fail_batch(batch_id: str, provider: str, error: str,
                stats: dict | None = None,
                lease: str | None = None) -> None:
    """批次失败定稿（P1-03：持当前租约才允许写）。

    事务内先校验租约（在 purge 之前）：租约已被接管或批已被接管方
    完成时，本 worker 放弃一切写入——不清场、不改状态、不写
    metadata，接管方的成功证据（快照/发布/metadata）不被破坏。
    """
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if lease is not None:
                _assert_lease(conn, batch_id, lease)
            # 失败清场：本批未发布数据当场清理，重导不再有残留占位
            purged = _purge_failed_batch_artifacts(conn, batch_id)
            # F13：租约被接管后不得覆盖接管方的终态（completed 保护）；
            # 本进程只在批次仍处 running/failed 时写失败
            conn.execute(
                "UPDATE source_import_batches SET status='failed', error=?,"
                " stats=COALESCE(?, stats), import_finished_at=?"
                " WHERE batch_id=? AND status<>'completed'",
                (error[:2000],
                 json.dumps(stats, ensure_ascii=False) if stats else None,
                 _now(), batch_id))
            if stats is not None:
                stats["failed_batch_purged_unpublished"] = purged
            conn.execute("COMMIT")
        except LeaseLost:
            conn.execute("ROLLBACK")
            raise  # 旧 worker：不写任何东西（含 metadata）
        except Exception:
            conn.execute("ROLLBACK")
            raise
    archive.write_metadata(provider, batch_id, {
        "status": "failed", "error": error[:2000],
        **({"stats": stats} if stats else {})})


def _record_failed_import(principal_id: str, staged: Path, provider: str,
                          reason: str, filename: str | None) -> None:
    """格式识别失败也要留痕：failed 批次（provider=unknown）。"""
    try:
        sha256, size = archive.sha256_file(staged)
    except OSError:
        sha256, size = "", 0
    batch_id = f"sib_{_uuid.uuid4().hex[:12]}"
    with db.formal() as conn:
        conn.execute(
            "INSERT INTO source_import_batches(batch_id, provider, status,"
            " original_filename, original_bytes, sha256, raw_path,"
            " parser_version, import_started_at, import_finished_at,"
            " error, imported_by)"
            " VALUES(?,?,'failed',?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(provider, sha256) DO UPDATE SET"
            " error=excluded.error, imported_by=excluded.imported_by,"
            " import_started_at=excluded.import_started_at,"
            " import_finished_at=excluded.import_finished_at",
            (batch_id, provider, filename or "", size, sha256, "",
             PARSER_VERSION, _now(), _now(), reason[:2000], principal_id))
