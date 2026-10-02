"""媒体对象最小版（§16.2）：hash 去重、本地对象目录、鉴权读取。

上传两步（prepare/finalize）：主模型经 MCP 拿 upload token 与引用，
字节经专用 HTTP 端点进入，不把大文件 base64 塞进工具参数。
"""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import audit, config, db
from ..errors import Forbidden, NotFound

_MAX_SIZE = 20 * 1024 * 1024
_ALLOWED_MIME = re.compile(
    r"^(image/(png|jpeg|gif|webp)|audio/(mpeg|wav|ogg|mp4)|video/mp4|application/pdf)$")

_staging: dict[str, dict] = {}  # upload_token -> 元数据（进程内；重启丢弃未完成上传）
_STAGING_TTL_S = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upload_prepare(principal_id: str, mime: str, size: int) -> dict:
    if not _ALLOWED_MIME.match(mime):
        raise Forbidden(f"mime not allowed: {mime}")
    if size <= 0 or size > _MAX_SIZE:
        raise Forbidden(f"size out of range 1..{_MAX_SIZE}")
    token = secrets.token_urlsafe(24)
    _staging[token] = {"mime": mime, "size": size, "owner": principal_id,
                       "created": _now(), "staged": False}
    return {"upload_token": token, "stage_url": f"/api/media/stage/{token}",
            "expires_in_s": _STAGING_TTL_S}


def _staging_dir() -> Path:
    d = config.RUNTIME_DIR / "staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_expired(meta: dict) -> bool:
    """全量审计 P1-07：expires_in_s 此前只是返回值——TTL 现在真执行。"""
    created = datetime.fromisoformat(meta["created"])
    return (datetime.now(timezone.utc)
            - created).total_seconds() > _STAGING_TTL_S


def _drop_token(token: str) -> None:
    _staging.pop(token, None)
    (_staging_dir() / token).unlink(missing_ok=True)


def stage_bytes(principal_id: str, token: str, data: bytes) -> dict:
    """P1-07：字节在此真正落盘暂存（此前只算 hash 就回"已暂存"）；
    owner / TTL / 精确字节数都在此校验。"""
    meta = _staging.get(token)
    if meta is None or _meta_expired(meta):
        _drop_token(token)
        raise NotFound("upload token unknown or expired")
    if meta["owner"] != principal_id:
        raise Forbidden("upload token belongs to another principal")
    if len(data) != meta["size"]:
        raise Forbidden(f"byte count {len(data)} != declared {meta['size']}")
    (_staging_dir() / token).write_bytes(data)
    meta["content_hash"] = hashlib.sha256(data).hexdigest()
    meta["staged"] = True
    return {"content_hash": meta["content_hash"], "staged": True,
            "size": len(data)}


def upload_finalize(principal_id: str, token: str) -> dict:
    """P1-07：finalize 只收 token——字节已由 stage 端点落盘，不再接受
    data_b64 整文件进工具参数（与 §16.2 合同相反的旧实现废除）。"""
    meta = _staging.get(token)
    if meta is not None and not _meta_expired(meta):
        # owner 检查先于 staged 状态（MEDIA-02：他人 token 是 403，
        # 不是"未暂存"的 404）
        if meta["owner"] != principal_id:
            raise Forbidden("upload token belongs to another principal")
    if (meta is None or _meta_expired(meta) or not meta.get("staged")):
        _drop_token(token)
        raise NotFound("upload token unknown, expired or not staged")
    path = _staging_dir() / token
    data = path.read_bytes()
    content_hash = hashlib.sha256(data).hexdigest()
    if len(data) != meta["size"] or content_hash != meta["content_hash"]:
        # 暂存文件被改（size/hash 不一致）→ 拒绝并作废 token
        _drop_token(token)
        raise Forbidden("staged bytes fail size/hash recheck",
                        code="MEDIA_STAGED_MISMATCH")
    obj_dir = config.RUNTIME_DIR / "objects"
    obj_dir.mkdir(parents=True, exist_ok=True)
    key = f"{content_hash}{_ext_for(meta['mime'])}"
    path = obj_dir / key
    with db.formal() as conn:
        existing = conn.execute(
            "SELECT content_hash FROM media_objects WHERE content_hash=?",
            (content_hash,)).fetchone()
        if existing is None:
            if not path.exists():
                path.write_bytes(data)  # hash 去重：同内容只落一次盘
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO media_objects(content_hash, mime, size,"
                    " storage_key, owned_by, created_at) VALUES(?,?,?,?,?,?)",
                    (content_hash, meta["mime"], len(data), key, principal_id, _now()))
                audit.record(conn, "media.stored", principal_id,
                             resource_id=content_hash[:16],
                             payload={"mime": meta["mime"], "size": len(data)})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    _drop_token(token)
    return {"content_hash": content_hash, "deduplicated": bool(existing)}


def _ext_for(mime: str) -> str:
    return {
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "image/webp": ".webp", "audio/mpeg": ".mp3", "audio/wav": ".wav",
        "audio/ogg": ".ogg", "audio/mp4": ".m4a", "video/mp4": ".mp4",
        "application/pdf": ".pdf",
    }.get(mime, ".bin")


def get_media(principal_id: str, content_hash: str) -> tuple[dict, Path]:
    # A03 修复：字节端点与 Registry media.get 同一授权（owners 专用）；
    # 已知 hash 不等于读取许可
    if principal_id not in ("qiaosheng", "jiaming"):
        from ..errors import Forbidden as _F
        raise _F("media bytes require owner principal",
                 principal=principal_id)
    with db.formal() as conn:
        row = conn.execute("SELECT * FROM media_objects WHERE content_hash=?",
                           (content_hash,)).fetchone()
    if row is None:
        raise NotFound("media not found")
    path = config.RUNTIME_DIR / "objects" / row["storage_key"]
    if not path.exists():
        raise NotFound("media object file missing", storage_key=row["storage_key"])
    return dict(row), path


def list_media(limit: int = 50) -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT content_hash, mime, size, owned_by, created_at FROM media_objects"
            " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
