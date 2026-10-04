"""存储：一致性备份与 restore --verify-only（§20.4）。

SQLite WAL 写入中不能以简单复制代替一致性备份——用 sqlite3 backup API。
恢复执行需独立授权；本模块只提供 verify-only。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_object_references() -> tuple[list[str], list[str]]:
    """F07（2026-10-03 审计 P1）：恢复集引用收集。

    备份必须携带库中引用的字节，否则"库在、母本/媒体没了"的备份
    verify 仍 ok 却不可恢复。引用面：source_import_batches.raw_path
    （Raw Archive 母本）与 media_objects.storage_key（媒体对象）。
    """
    raw_paths: list[str] = []
    storage_keys: list[str] = []
    if config.FORMAL_DB.exists():
        conn = sqlite3.connect(str(config.FORMAL_DB))
        try:
            if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND"
                    " name='source_import_batches'").fetchone():
                # SRC-05（2026-10-04 二批）：空字符串=尚无归档引用
                #（格式错误导入会落 ''）——不收集，否则一次坏输入
                # 永久污染后续所有备份的恢复校验
                raw_paths = [r[0] for r in conn.execute(
                    "SELECT DISTINCT raw_path FROM source_import_batches"
                    " WHERE raw_path IS NOT NULL AND raw_path <> ''")]
            if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND"
                    " name='media_objects'").fetchone():
                storage_keys = [r[0] for r in conn.execute(
                    "SELECT DISTINCT storage_key FROM media_objects")]
        finally:
            conn.close()
    return raw_paths, storage_keys


def _copy_object(src: Path, rel: str, dest_dir: Path) -> dict | None:
    """把一个被引用对象复制进备份目录并记录身份；源缺失返回
    missing 标记（verify 阶段 fail closed，不静默降级为 DB-only）。"""
    entry: dict = {"rel": rel, "source_path": str(src)}
    if not src.is_file():
        entry["missing_at_backup"] = True
        return entry
    target = dest_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    entry["sha256"] = _sha256_file(target)
    entry["bytes"] = target.stat().st_size
    return entry


def backup() -> dict:
    config.ensure_dirs()
    # F11（2026-10-03 审计 P2）：秒级目录名让同秒两次备份互相覆盖——
    # 加微秒保证唯一
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest_dir = config.RUNTIME_DIR / "backups" / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"created_at": datetime.now(timezone.utc).isoformat(),
                      "databases": {}}
    for name, db_path in (("formal", config.FORMAL_DB),
                          ("workspace", config.WORKSPACE_DB)):
        if not db_path.exists():
            continue
        target = dest_dir / f"{name}.sqlite3"
        src = sqlite3.connect(str(db_path))
        try:
            dst = sqlite3.connect(str(target))
            with dst:
                src.backup(dst)
            dst.close()
        finally:
            src.close()
        manifest["databases"][name] = {
            "path": str(target),
            "sha256": _sha256_file(target),
            "bytes": target.stat().st_size,
        }
    # F07：恢复集 = 数据库 + 被引用的 Raw 母本 + 媒体对象
    raw_paths, storage_keys = _collect_object_references()
    manifest["raw_archives"] = []
    for p in raw_paths:
        rp = Path(p)
        rel = f"raw/{rp.relative_to(config.SOURCE_RAW_DIR)}" \
            if _is_under(rp, config.SOURCE_RAW_DIR) else \
            f"raw/orphan/{rp.name}"
        entry = _copy_object(rp, rel, dest_dir)
        if entry:
            # SRC-03（2026-10-04 二批）：归档运行必需 manifest 一并
            # 备份——只有 payload 没有 manifest 的恢复集不能重建
            # 归档身份（校验依据 manifest 记录的精确 payload 路径）
            mf = rp.parent / "manifest.json"
            if mf.is_file():
                rel_dir = str(Path(rel).parent)
                mf_rel = (f"{rel_dir}/manifest.json"
                          if rel_dir not in (".", "")
                          else "manifest.json")
                _copy_object(mf, mf_rel, dest_dir)
            manifest["raw_archives"].append(entry)
    manifest["media_objects"] = []
    for key in storage_keys:
        entry = _copy_object(config.RUNTIME_DIR / "objects" / key,
                             f"objects/{key}", dest_dir)
        if entry:
            manifest["media_objects"].append(entry)
    manifest_path = dest_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(json.dumps({"ok": True, "backup_dir": str(dest_dir),
                      "manifest": str(manifest_path)}, ensure_ascii=False))
    # F11：返回值与打印一致地带 ok——main() 按 ok 定退出码，此前
    # 成功备份 CLI 也 exit 1
    manifest = {**manifest, "ok": True, "backup_dir": str(dest_dir)}
    return manifest


#: verify 的必需数据库集（CB-002）：formal/workspace 缺一即 fail closed
_REQUIRED_DBS = ("formal", "workspace")


def restore_verify(backup_dir: str) -> dict:
    bdir = Path(backup_dir)
    manifest_path = bdir / "manifest.json"
    if not manifest_path.exists():
        result = {"ok": False, "error": "manifest.json missing"}
        print(json.dumps(result))
        return result
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # CB-002（2026-10-02 审计 P1）：空清单不得通过——一个数据库都
    # 没有的备份没有恢复材料可言
    databases = manifest.get("databases")
    if not isinstance(databases, dict) or not databases:
        result = {"ok": False, "error": "manifest databases empty"}
        print(json.dumps(result, ensure_ascii=False))
        return result
    problems = []
    for name in _REQUIRED_DBS:
        if name not in databases:
            problems.append({"db": name, "issue": "missing_from_manifest"})
    # CB-002：验证调用方指定的备份目录本身。manifest 里的 path 是备份
    # 时的绝对路径——目录迁移/复制后按它验证会给出假结果（旧位置完好
    # =损坏副本通过；旧位置删除=好副本误报缺失）。按 bdir 下的约定
    # 文件名定位；path 字段仅作描述，不再信任。
    for name, info in databases.items():
        p = bdir / f"{name}.sqlite3"
        if not p.is_file():
            problems.append({"db": name, "issue": "file_missing"})
            continue
        if info.get("bytes") is not None and p.stat().st_size != info["bytes"]:
            problems.append({"db": name, "issue": "size_mismatch"})
            continue
        actual = _sha256_file(p)
        if actual != info.get("sha256"):
            problems.append({"db": name, "issue": "sha256_mismatch"})
            continue
        # 备份文件必须可打开且通过 SQLite 完整性检查——"存在且哈希
        # 对"只证明字节一致，不证明是可恢复的库
        try:
            conn = sqlite3.connect(str(p))
            try:
                ic = conn.execute("PRAGMA integrity_check").fetchone()
                if ic is None or ic[0] != "ok":
                    problems.append(
                        {"db": name,
                         "issue": f"integrity_check: "
                                  f"{ic[0] if ic else 'no result'}"})
                    continue
                # RA-027（2026-10-02 复审 P2）：验应用身份——formal
                # 必须含 memories 表、workspace 必须含既有 workspace
                # 表集（当前迁移仍建 workspace 7 表）；无关 SQLite
                # 不再判"可供 Mariposa 恢复"
                required = (["memories", "memory_versions", "principals"]
                            if name == "formal"
                            else ["mariposa_db_meta"])
                for t in required:
                    if not conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE"
                            " type='table' AND name=?",
                            (t,)).fetchone():
                        problems.append(
                            {"db": name,
                             "issue": f"not_a_mariposa_{name}_backup"
                                      f"（缺表 {t}）"})
                        break
                # F12（2026-10-03 审计 P2）：库身份必须各归其位——
                # formal 备份替换 workspace 位置后哈希可以重算合法，
                # 但启动 identity 校验会拒绝；verify 同标准拒绝
                expected_identity = f"{name}_v1"
                meta = conn.execute(
                    "SELECT value FROM mariposa_db_meta WHERE"
                    " key='identity'").fetchone()
                # 无 meta 行=迁移前旧库，启动时也放行补章（同口径）；
                # 有章不匹配=错误文件冒名，verify 与启动一致拒绝
                if meta is not None and meta[0] != expected_identity:
                    problems.append({
                        "db": name, "issue": "identity_mismatch",
                        "expected": expected_identity,
                        "got": meta[0]})
            finally:
                conn.close()
        except sqlite3.Error as e:
            problems.append({"db": name, "issue": f"unreadable: {e}"})
    # F07（2026-10-03 审计 P1）：恢复集不只是数据库——库中引用的
    # Raw 母本与媒体对象的字节必须在备份内且哈希一致，否则"库在、
    # 对象没了"的备份不得宣称可完整恢复（媒体读取 NOT_FOUND、
    # archive verify 失败都是恢复后必然撞上的坑）
    problems.extend(_verify_object_restore_set(bdir, manifest))
    result = {"ok": not problems, "problems": problems,
              "note": "verify-only；实际恢复需独立授权参数"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _verify_object_restore_set(bdir: Path, manifest: dict) -> list[dict]:
    """对象恢复集核对：备份 formal 库内的每条对象引用，都必须能在
    备份目录找到对应字节且哈希一致；清单里登记的每个对象文件也必须
    实际在场。旧格式备份（无对象清单）只要库里有引用同样 fail closed。"""
    problems: list[dict] = []
    declared_raw = {e.get("source_path"): e
                    for e in (manifest.get("raw_archives") or [])}
    declared_media = {e["rel"][len("objects/"):]
                      for e in (manifest.get("media_objects") or [])
                      if not e.get("missing_at_backup")}
    for e in (manifest.get("raw_archives") or []):
        if e.get("missing_at_backup"):
            problems.append({"object": e.get("source_path"),
                             "issue": "raw_archive_missing_at_backup"})
            continue
        p = bdir / e["rel"]
        if not p.is_file() or _sha256_file(p) != e.get("sha256"):
            problems.append({"object": e["rel"],
                             "issue": "raw_archive_bytes_mismatch"})
            continue
        # SRC-03：恢复集必须能重建归档运行——缺 manifest 的 payload
        # 无法按记录的精确路径校验母本，不判完整
        mf = p.parent / "manifest.json"
        if not mf.is_file():
            problems.append({"object": e["rel"],
                             "issue": "raw_archive_manifest_missing"})
    for e in (manifest.get("media_objects") or []):
        p = bdir / e["rel"]
        if not p.is_file() or _sha256_file(p) != e.get("sha256"):
            problems.append({"object": e["rel"],
                             "issue": "media_bytes_mismatch"})
    fdb = bdir / "formal.sqlite3"
    if not fdb.is_file():
        return problems
    try:
        conn = sqlite3.connect(f"file:{fdb}?mode=ro", uri=True)
        try:
            if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND"
                    " name='source_import_batches'").fetchone():
                for (rp,) in conn.execute(
                        "SELECT DISTINCT raw_path FROM"
                        " source_import_batches"
                        " WHERE raw_path IS NOT NULL"):
                    e = declared_raw.get(rp)
                    if e is None or e.get("missing_at_backup"):
                        problems.append({"object": rp,
                                         "issue":
                                         "raw_archive_not_in_backup"})
            if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND"
                    " name='media_objects'").fetchone():
                for (key,) in conn.execute(
                        "SELECT DISTINCT storage_key FROM media_objects"):
                    if key not in declared_media:
                        problems.append({"object": key,
                                         "issue":
                                         "media_object_not_in_backup"})
        finally:
            conn.close()
    except sqlite3.Error as e:
        problems.append({"object": "formal.sqlite3",
                         "issue": f"reference_scan_unreadable: {e}"})
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mariposa.storage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup")
    p_r = sub.add_parser("restore")
    p_r.add_argument("--from", dest="from_dir", required=True)
    p_r.add_argument("--verify-only", action="store_true", default=True)
    args = ap.parse_args(argv)
    if args.cmd == "backup":
        r = backup()
    else:
        r = restore_verify(args.from_dir)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
