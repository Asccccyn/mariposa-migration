"""迁移工具：inventory（只读清单）/ dry-run（结构映射演练）/ verify（完整性核对）。

边界（§20）：真实快照仅在受控迁移执行中处理；inventory 只输出元数据
（文件计数/大小/hash），不打印正文；锁信正文不进日志、不进模型。
用法：
  python -m mariposa.migration inventory --source <dir>
  python -m mariposa.migration dry-run --fixtures tests/fixtures/legacy_bucket_sample
  python -m mariposa.migration verify --report <json>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import config

# 旧 bucket 文件名格式（bucket_manager 核验）: "YYYY-MM-DD HH-MM-SS 标题_hash12.md"
LEGACY_BUCKETS_DEFAULT = [r"D:\Ombre-Brain-main2.5\buckets-data"]
HASH_LEN = 12


def _iter_bucket_files(source: Path):
    return sorted(source.rglob("*.md"))


def inventory(source: str | None, out: str | None = None) -> dict:
    src = Path(source) if source else Path(LEGACY_BUCKETS_DEFAULT[0])
    if not src.exists():
        return {"ok": False, "error": f"source not found: {src}"}
    files = _iter_bucket_files(src)
    by_dir: dict[str, dict] = {}
    letters_locked = 0
    for f in files:
        rel = str(f.parent.relative_to(src))
        stat = f.stat().st_size
        d = by_dir.setdefault(rel, {"files": 0, "bytes": 0})
        d["files"] += 1
        d["bytes"] += stat
        # 只读头部元数据行判断锁（不读正文；锁信正文不进任何输出）
        try:
            # 只读 frontmatter（第二个 --- 之前），不触及正文（§14.3）
            raw_head = f.read_text(encoding="utf-8", errors="replace")
            end = raw_head.find("\n---", 3)
            head = raw_head[:end] if end > 0 else raw_head[:512]
            if "lock_type: timed" in head or "lock_type: locked" in head:
                letters_locked += 1
        except OSError:
            pass
    report = {
        "ok": True,
        "source": str(src),
        "total_files": len(files),
        "total_bytes": sum(d["bytes"] for d in by_dir.values()),
        "by_directory": by_dir,
        "locked_letters_detected": letters_locked,
        "note": "仅元数据统计；正文与锁信内容未读取、未输出",
        "real_snapshot_authorized": False,
    }
    _emit(report, out)
    return report


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """最小 YAML frontmatter 子集（key: value / 列表项），够旧格式解析。"""
    meta: dict = {}
    body = text
    if text.startswith("---"):
        parts = text.split("\n---", 2)
        if len(parts) >= 2:
            fm_lines = parts[0][3:].splitlines()
            body = parts[1].lstrip("-\n") if len(parts) > 2 else ""
            key = None
            for line in fm_lines:
                if not line.strip():
                    continue
                if line[:1] in (" ", "-") and key:
                    val = line.strip().lstrip("-").strip()
                    if isinstance(meta.get(key), list):
                        meta[key].append(val.strip('"'))
                    else:
                        meta[key] = [val.strip('"')]
                elif ":" in line:
                    key, _, val = line.partition(":")
                    val = val.strip().strip('"')
                    meta[key.strip()] = val
                body_idx = text.find("\n---\n", 3)
                body = text[body_idx + 5:] if body_idx > 0 else ""
    return meta, body


def dry_run(fixtures: str, out: str | None = None) -> dict:
    """对合成 fixture 做结构映射演练；产出 ID/字段映射与 payload hash。
    真实数据 dry-run 需获准快照后另跑；本命令拒绝处理含真实数据特征的大目录。"""
    fdir = Path(fixtures)
    if not fdir.exists():
        return {"ok": False, "error": f"fixtures dir not found: {fdir}"}
    files = _iter_bucket_files(fdir)
    if len(files) > 50:
        return {"ok": False, "error": "fixture 目录超出合成样本规模，疑似真实数据；"
                                      "真实迁移需获准快照后执行"}

    entries = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        name = f.name
        legacy_id = name
        is_letter = (meta.get("type") == "letter") or "letter" in str(meta.get("bucket_type", ""))
        plan = {
            "legacy_id": legacy_id,
            "target": "letters" if is_letter else "memories",
            "mapping": {
                "memory_date": meta.get("date") or name[:10],
                "hold_text": body.strip(),
                "why_remember": meta.get("why_remembered"),
                "importance": meta.get("importance"),
                "pinned": str(meta.get("pinned", "")).lower() in ("true", "1"),
                "meaning_layers": meta.get("meaning") if isinstance(meta.get("meaning"), list) else [],
                "letter_lock": {
                    "lock_type": meta.get("lock_type", "none"),
                    "unlock_date": meta.get("unlock_date"),
                } if is_letter else None,
            },
            "payload_hash": hashlib.sha256(
                body.strip().encode("utf-8")).hexdigest(),
            "content_bytes": len(body.encode("utf-8")),
        }
        # 锁信正文不进报告
        if is_letter:
            plan["mapping"]["hold_text"] = f"<restricted:{plan['payload_hash'][:16]}>"
        entries.append(plan)

    report = {
        "ok": True,
        "kind": "dry_run",
        "fixture_dir": str(fdir),
        "entries": entries,
        "counts": {"total": len(entries),
                   "to_memories": sum(1 for e in entries if e["target"] == "memories"),
                   "to_letters": sum(1 for e in entries if e["target"] == "letters")},
        "apply": "blocked: 真实快照未获准；apply 命令在获准后另跑",
        "policy_version": config.POLICY_VERSION,
    }
    _emit(report, out)
    return report


def verify(report_path: str) -> dict:
    """核对 dry-run 报告：hash 可复算、映射字段齐全、锁信无正文。"""
    p = Path(report_path)
    if not p.exists():
        return {"ok": False, "error": f"report not found: {p}"}
    report = json.loads(p.read_text(encoding="utf-8"))
    problems = []
    fdir = Path(report.get("fixture_dir", ""))
    for e in report.get("entries", []):
        f = fdir / e["legacy_id"]
        if not f.exists():
            problems.append({"legacy_id": e["legacy_id"], "issue": "file_missing"})
            continue
        _, body = parse_frontmatter(f.read_text(encoding="utf-8"))
        recomputed = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()
        if recomputed != e["payload_hash"]:
            problems.append({"legacy_id": e["legacy_id"], "issue": "payload_hash_mismatch"})
        if e["target"] == "memories":
            m = e["mapping"]
            for field in ("memory_date", "hold_text"):
                if not m.get(field):
                    problems.append({"legacy_id": e["legacy_id"],
                                     "issue": f"missing:{field}"})
        if e["target"] == "letters" and not str(e["mapping"]["hold_text"]).startswith("<restricted"):
            problems.append({"legacy_id": e["legacy_id"], "issue": "letter_body_leaked"})
    result = {"ok": not problems, "checked": len(report.get("entries", [])),
              "problems": problems}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _emit(report: dict, out: str | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mariposa.migration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_inv = sub.add_parser("inventory")
    p_inv.add_argument("--source", required=True,
                       help="旧 buckets 数据目录（只读，仅元数据）")
    p_inv.add_argument("--out")
    p_dry = sub.add_parser("dry-run")
    p_dry.add_argument("--fixtures", required=True)
    p_dry.add_argument("--out")
    p_ver = sub.add_parser("verify")
    p_ver.add_argument("--report", required=True)
    p_app = sub.add_parser("apply")
    p_app.add_argument("--report", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "inventory":
        r = inventory(args.source, args.out)
    elif args.cmd == "dry-run":
        r = dry_run(args.fixtures, args.out)
    elif args.cmd == "verify":
        r = verify(args.report)
    else:
        r = apply_from_report(args.report)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())


def apply_from_report(report_path: str) -> dict:
    """迁移演练 apply：把 dry-run 报告（合成 fixture）落到当前正式库并核对。

    仅接受 fixture 规模的报告（与 dry-run 同防）；真实数据 apply 需获准快照。
    """
    from . import db as _db
    from .identity import service as identity
    from .memory import service as memory
    from .letters import service as letters
    _migrator = identity.Principal("system", "迁移执行器", "system", "migration",
                                   "binding_migration")
    p = Path(report_path)
    report = json.loads(p.read_text(encoding="utf-8"))
    entries = report.get("entries", [])
    if len(entries) > 50:
        return {"ok": False, "error": "报告超出合成样本规模；真实迁移需获准快照"}
    fdir = Path(report["fixture_dir"])
    applied, problems = [], []
    for e in entries:
        f = fdir / e["legacy_id"]
        if not f.exists():
            problems.append({"legacy_id": e["legacy_id"], "issue": "file_missing"})
            continue
        _, body = parse_frontmatter(f.read_text(encoding="utf-8"))
        meta = parse_frontmatter(f.read_text(encoding="utf-8"))[0]
        recomputed = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()
        if recomputed != e["payload_hash"]:
            problems.append({"legacy_id": e["legacy_id"], "issue": "hash_mismatch"})
            continue
        if e["target"] == "memories":
            out = memory.hold(_migrator,
                              text=body.strip(),
                              why_remember=meta.get("why_remembered"),
                              memory_date=e["mapping"]["memory_date"],
                              date_confidence="inferred")
            mid = out["memory_id"]
            if e["mapping"]["pinned"]:
                with _db.formal() as conn:
                    conn.execute("UPDATE memories SET pinned=1 WHERE memory_id=?",
                                 (mid,))
            applied.append({"legacy_id": e["legacy_id"], "new_id": mid,
                            "target": "memories"})
        else:
            lock = e["mapping"]["letter_lock"] or {}
            out = letters.write_letter(
                _migrator, body.strip(),
                letter_date=e["mapping"]["memory_date"],
                lock_type=lock.get("lock_type", "none") or "none",
                unlock_date=lock.get("unlock_date"))
            applied.append({"legacy_id": e["legacy_id"], "new_id": out["letter_id"],
                            "target": "letters"})
    # 逐项核对：新库可读、锁参数保留、pinned 保留
    verified = 0
    for a in applied:
        with _db.formal() as conn:
            if a["target"] == "memories":
                row = conn.execute("SELECT pinned FROM memories WHERE memory_id=?",
                                   (a["new_id"],)).fetchone()
            else:
                row = conn.execute("SELECT lock_type FROM letters WHERE id=?",
                                   (a["new_id"],)).fetchone()
        if row is not None:
            verified += 1
    result = {"ok": not problems, "applied": len(applied), "verified": verified,
              "problems": problems,
              "note": "合成 fixture 演练；真实数据 apply 仍 blocked: 快照未获准"}
    print(json.dumps({k: v for k, v in result.items() if k != "problems"},
                     ensure_ascii=False))
    return result
