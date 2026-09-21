"""存储：一致性备份与 restore --verify-only（§20.4）。

SQLite WAL 写入中不能以简单复制代替一致性备份——用 sqlite3 backup API。
恢复执行需独立授权；本模块只提供 verify-only。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup() -> dict:
    config.ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
    manifest_path = dest_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(json.dumps({"ok": True, "backup_dir": str(dest_dir),
                      "manifest": str(manifest_path)}, ensure_ascii=False))
    return manifest


def restore_verify(backup_dir: str) -> dict:
    bdir = Path(backup_dir)
    manifest_path = bdir / "manifest.json"
    if not manifest_path.exists():
        result = {"ok": False, "error": "manifest.json missing"}
        print(json.dumps(result))
        return result
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    for name, info in manifest["databases"].items():
        p = Path(info["path"])
        if not p.exists():
            problems.append({"db": name, "issue": "file_missing"})
            continue
        actual = _sha256_file(p)
        if actual != info["sha256"]:
            problems.append({"db": name, "issue": "sha256_mismatch"})
        # 备份文件本身可打开且表可查
        try:
            conn = sqlite3.connect(str(p))
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            conn.close()
        except sqlite3.Error as e:
            problems.append({"db": name, "issue": f"unreadable: {e}"})
    result = {"ok": not problems, "problems": problems,
              "note": "verify-only；实际恢复需独立授权参数"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


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
