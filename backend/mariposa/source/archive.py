"""Raw Archive（复核 v1.1 §2.2 强化）。

- 一次固定快照：导入输入先流式复制到受控暂存（同时计算写入字节 hash），
  再原子发布为归档 payload；解析/复核只读这份固定字节，不重开原始路径。
- 文件名隔离：内部只用 payload-<sha16> 生成名；原始文件名仅存 manifest。
- 校验依据 manifest 记录的精确 payload 路径，不用 glob 猜母本。
- 0444 是应用级只读保护，不宣称不可被管理员删除的 WORM。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..errors import MariposaError

_CHUNK = 1 << 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def batch_dir(provider: str, batch_id: str) -> Path:
    return config.SOURCE_RAW_DIR / provider / batch_id


def stage_input(src: Path) -> Path:
    """把导入输入字节复制到受控暂存（后续一切处理只读它）。"""
    config.SOURCE_INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    staged = config.SOURCE_INCOMING_DIR / (
        f"staging-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        f"-{os.urandom(6).hex()}.part")
    with open(src, "rb") as fsrc, open(staged, "wb") as fdst:
        while True:
            chunk = fsrc.read(_CHUNK)
            if not chunk:
                break
            fdst.write(chunk)
    return staged


def publish_snapshot(provider: str, batch_id: str, staged: Path,
                     sha256: str, size: int, original_filename: str,
                     imported_by: str) -> Path:
    """原子发布暂存为归档母本并写 manifest（幂等：已存在且 hash 一致则复用）。

    归档命名内部生成（payload-<sha16>），原始文件名只作 manifest 元数据。
    """
    dest_dir = batch_dir(provider, batch_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    payload = dest_dir / f"payload-{sha256[:16]}"
    if payload.exists():
        actual, actual_size = sha256_file(payload)
        if actual != sha256 or actual_size != size:
            raise MariposaError(
                "归档目录存在同名但内容不同的 payload；拒绝覆盖",
                code="SOURCE_ARCHIVE_MISMATCH")
    else:
        os.replace(staged, payload)  # 原子发布（同文件系统 incoming→raw）
    _mark_readonly(payload)

    manifest = {
        "batch_id": batch_id,
        "provider": provider,
        "payload": payload.name,
        "original_filename": original_filename,
        "sha256": sha256,
        "bytes": size,
        "imported_by": imported_by,
        "imported_at": _now(),
        "immutability": "应用级只读保护（0444）；不清洗、不覆盖、不因重"
                        "解析丢弃；非管理员不可删的 WORM 存储",
    }
    # SRC-05：manifest 临时写+原子替换——半截 manifest 不得留在
    # 归档目录（verify 会把它当损坏恢复集）
    _mf_tmp = dest_dir / "manifest.json.part"
    _mf_tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8")
    os.replace(_mf_tmp, dest_dir / "manifest.json")
    return payload


def write_metadata(provider: str, batch_id: str, metadata: dict) -> None:
    dest_dir = batch_dir(provider, batch_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "metadata.json"
    payload = {"batch_id": batch_id, "written_at": _now(), **metadata}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def load_manifest(provider: str, batch_id: str) -> dict:
    mpath = batch_dir(provider, batch_id) / "manifest.json"
    if not mpath.exists():
        raise MariposaError("manifest.json missing", code="SOURCE_ARCHIVE_MISSING")
    try:
        return json.loads(mpath.read_text(encoding="utf-8"))
    except ValueError as e:
        raise MariposaError(f"manifest.json corrupted: {e}",
                            code="SOURCE_ARCHIVE_CORRUPT") from e


def publish_bytes(provider: str, batch_id: str, data: bytes, sha256: str,
                  manifest_extra: dict) -> Path:
    """在线 live ingest 的母本落盘（迁移 30 契约 §4.3）。

    与 publish_snapshot 同一规约：原子替换、payload-<sha16> 命名、
    manifest 临时写+替换、0444 只读。幂等：同 hash 已存在且一致则
    复用（崩溃重试不重写）。调用方必须在 DB 事务提交**之前**完成
    本调用——ACK 成功时母本必然已在盘上；DB 失败只留下待核对孤立
    资产，不出现反向。
    """
    dest_dir = batch_dir(provider, batch_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    payload = dest_dir / f"payload-{sha256[:16]}"
    if payload.exists():
        actual, _ = sha256_file(payload)
        if actual != sha256:
            raise MariposaError(
                "归档目录存在同名但内容不同的 payload；拒绝覆盖",
                code="SOURCE_ARCHIVE_MISMATCH")
    else:
        tmp = dest_dir / (f"payload-{sha256[:16]}.part-"
                          f"{os.urandom(4).hex()}")
        tmp.write_bytes(data)
        os.replace(tmp, payload)
    _mark_readonly(payload)
    manifest = {
        "batch_id": batch_id,
        "provider": provider,
        "payload": payload.name,
        "sha256": sha256,
        "bytes": len(data),
        "written_at": _now(),
        "immutability": "应用级只读保护（0444）；在线 live 母本与导出"
                        "快照同一规约",
        **manifest_extra,
    }
    _mf_tmp = dest_dir / "manifest.json.part"
    _mf_tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8")
    os.replace(_mf_tmp, dest_dir / "manifest.json")
    return payload


def verify_archived(provider: str, batch_id: str, sha256: str) -> dict:
    """完整性校验：按 manifest 记录的精确 payload 路径核对哈希。"""
    try:
        manifest = load_manifest(provider, batch_id)
    except MariposaError as e:
        return {"ok": False, "issue": e.code}
    payload_name = manifest.get("payload")
    if not payload_name:
        return {"ok": False, "issue": "manifest_payload_missing"}
    payload = batch_dir(provider, batch_id) / payload_name
    if not payload.is_file():
        return {"ok": False, "issue": "archived_payload_missing",
                "payload": payload_name}
    actual, size = sha256_file(payload)
    if actual != sha256:
        return {"ok": False, "issue": "sha256_mismatch",
                "expected": sha256, "actual": actual}
    return {"ok": True, "archived_as": payload_name, "bytes": size}


def cleanup_staging(max_age_hours: int = 48) -> int:
    """暂存保留期清理（受控暂存与 .part 残留）。"""
    import time
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    if not config.SOURCE_INCOMING_DIR.exists():
        return 0
    for p in config.SOURCE_INCOMING_DIR.iterdir():
        if p.name.endswith(".part") and p.stat().st_mtime < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _mark_readonly(path: Path) -> None:
    try:
        os.chmod(path, 0o444)
    except OSError:
        pass
