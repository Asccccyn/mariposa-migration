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
            finally:
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
