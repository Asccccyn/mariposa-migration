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


def snapshot(source: str, dest: str | None = None) -> dict:
    """生产 -> staging 副本：程序逐字节复制 + hash 清单（§20.2）。

    只读 source；正文不进日志/输出/模型（锁信同：只复制字节）。
    """
    import shutil
    src = Path(source)
    dst = Path(dest) if dest else config.RUNTIME_DIR / "migration_staging"
    if not src.exists():
        return {"ok": False, "error": f"source not found: {src}"}
    if dst.exists() and any(dst.iterdir()):
        return {"ok": False, "error": f"dest not empty: {dst}（先清空或换目录）"}
    dst.mkdir(parents=True, exist_ok=True)
    copied, manifest = 0, []
    for f in _iter_bucket_files(src):
        rel = f.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)  # 逐字节；不解析不输出
        manifest.append({"path": str(rel).replace("\\", "/"),
                         "bytes": f.stat().st_size,
                         "sha256": hashlib.sha256(f.read_bytes()).hexdigest()})
        copied += 1
    (dst / "_manifest.json").write_text(
        json.dumps({"source": str(src), "files": manifest}, ensure_ascii=False),
        encoding="utf-8")
    return {"ok": True, "copied": copied, "dest": str(dst),
            "note": "副本已就绪；正文未解析未输出；源只读"}


# 旧 frontmatter 键 -> 迁移目标/字段 白名单（§20.2 数据清单）
_TYPE_TARGET = {"dynamic": "memories", "feel": "memories", "permanent": "memories",
                "plan": "plans", "i": "self_entries"}
# 2026-10-01：信件拆出 mariposa（独立项目另行开发）；letter 桶不迁移，
# 落 UNMAPPED（正文/哈希均不进报告），由信件项目自行处理旧数据。
_KNOWN_KEYS = {"type", "date", "created", "importance", "pinned", "protected",
               "why_remembered", "meaning", "tags", "domains", "valence",
               "arousal", "author", "lock_type", "unlock_date", "locked_by",
               "relations", "title", "content_hash", "deleted_at",
               "tombstoned_at", "erasure_mode", "erased_at", "tombstone",
               "deleted", "physical_erasure", "plan_status", "status"}


def dry_run_real(fixtures: str, out: str | None = None) -> dict:
    """对真实副本做逐项元数据映射（正文不进报告；锁信只保留锁参数）。

    核对维度（§20.3）：旧 ID、日期、类型目标、锁 metadata、删除终态、
    pinned/importance、meaning 层数、未知字段（legacy extension 清单）。
    """
    fdir = Path(fixtures)
    files = _iter_bucket_files(fdir)
    stats: dict = {"total": 0, "by_target": {}, "no_date": 0, "bad_frontmatter": 0,
                   "unknown_type": 0, "deletion_terminal": 0,
                   "pinned": 0, "with_meaning": 0, "with_relations": 0,
                   }
    entries, unknown_keys = [], {}
    for f in files:
        stats["total"] += 1
        rel = str(f.relative_to(fdir)).replace("\\", "/")
        name = f.name
        legacy_id = name.rsplit("_", 1)[-1].replace(".md", "") if "_" in name else name
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            meta, body = parse_frontmatter(text)
        except OSError:
            stats["bad_frontmatter"] += 1
            continue
        btype = str(meta.get("type") or meta.get("bucket_type") or "").strip().lower()
        # v2.0 P-A01 零残留：Memory archive 无兼容分支——旧归档桶
        # 走通用 UNMAPPED（未知类型如实报告，不做专门处置）
        target = _TYPE_TARGET.get(btype)
        if target is None:
            if btype:
                stats["unknown_type"] += 1
            else:
                stats["bad_frontmatter"] += 1
            entries.append({"path": rel, "target": "UNMAPPED", "type": btype})
            continue
        stats["by_target"][target] = stats["by_target"].get(target, 0) + 1
        date = str(meta.get("date") or "")[:10] or name[:10]
        if not date[:4].isdigit():
            stats["no_date"] += 1
        entry: dict = {"path": rel, "legacy_id": legacy_id, "type": btype,
                       "target": target,
                       "memory_date": date if date[:4].isdigit() else None,
                       "payload_sha256": hashlib.sha256(
                           body.strip().encode("utf-8")).hexdigest(),
                       "content_bytes": len(body.encode("utf-8"))}
        if str(meta.get("pinned", "")).lower() in ("true", "1"):
            entry["pinned"] = True
            stats["pinned"] += 1
        if meta.get("importance") is not None:
            entry["importance_raw"] = str(meta.get("importance"))
        if isinstance(meta.get("meaning"), list):
            entry["meaning_layers"] = len(meta["meaning"])
            stats["with_meaning"] += 1
        if meta.get("relations"):
            entry["has_relations"] = True
            stats["with_relations"] += 1
        terminal = [k for k in ("deleted_at", "tombstoned_at", "erased_at",
                                "tombstone", "deleted", "physical_erasure")
                    if str(meta.get(k, "")).strip() not in ("", "false", "None")]
        if terminal:
            entry["deletion_terminal_fields"] = terminal
            stats["deletion_terminal"] += 1
        # §5 迁移映射：dont_surface 保留隐藏语义；tags_only/digested 待审
        if str(meta.get("dont_surface", "")).lower() in ("true", "1"):
            entry["migrate_as_hidden"] = True
            stats["dont_surface_hidden"] = stats.get("dont_surface_hidden", 0) + 1
        if str(meta.get("tags_only", "")).lower() in ("true", "1") or (
                str(meta.get("digested", "")).lower() in ("true", "1")):
            entry["needs_migration_review"] = True
            stats["needs_migration_review"] = stats.get("needs_migration_review", 0) + 1
        for k in meta:
            if k not in _KNOWN_KEYS:
                unknown_keys[k] = unknown_keys.get(k, 0) + 1
        entries.append(entry)
    report = {"ok": True, "kind": "dry_run_real_copy", "source_dir": str(fdir),
              "stats": stats, "entries": entries,
              "legacy_extension_keys": sorted(unknown_keys),
              "note": "正文与锁信正文均未进入报告（hash/字节代替）；"
                      "apply 到正式库需另行授权"}
    _emit(report, out)
    return report


def inventory(source: str | None, out: str | None = None) -> dict:
    src = Path(source) if source else Path(LEGACY_BUCKETS_DEFAULT[0])
    if not src.exists():
        return {"ok": False, "error": f"source not found: {src}"}
    files = _iter_bucket_files(src)
    by_dir: dict[str, dict] = {}
    for f in files:
        rel = str(f.parent.relative_to(src))
        stat = f.stat().st_size
        d = by_dir.setdefault(rel, {"files": 0, "bytes": 0})
        d["files"] += 1
        d["bytes"] += stat
    report = {
        "ok": True,
        "source": str(src),
        "total_files": len(files),
        "total_bytes": sum(d["bytes"] for d in by_dir.values()),
        "by_directory": by_dir,
        "note": "仅元数据统计；正文内容未读取、未输出",
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
        # D02（2026-10-01）：Home/Self/Diary 随旧 Content 体系退役——旧桶
        # out_of_scope（正文/哈希不进报告），数据由后续裁定处理
        is_retired_content = meta.get("type") in ("self", "diary")
        payload_hash = hashlib.sha256(
            body.strip().encode("utf-8")).hexdigest()
        if is_letter or is_retired_content:
            # 信件已出 mariposa 范围：仅登记存在性与哈希，正文不进报告
            entries.append({
                "legacy_id": legacy_id,
                "target": "out_of_scope",
                "payload_hash": payload_hash,
                "content_bytes": len(body.encode("utf-8")),
            })
            continue
        plan = {
            "legacy_id": legacy_id,
            "target": "memories",
            "mapping": {
                "memory_date": meta.get("date") or name[:10],
                "hold_text": body.strip(),
                "why_remember": meta.get("why_remembered"),
                "importance": meta.get("importance"),
                "pinned": str(meta.get("pinned", "")).lower() in ("true", "1"),
                "meaning_layers": meta.get("meaning") if isinstance(meta.get("meaning"), list) else [],
            },
            "payload_hash": payload_hash,
            "content_bytes": len(body.encode("utf-8")),
        }
        entries.append(plan)

    report = {
        "ok": True,
        "kind": "dry_run",
        "fixture_dir": str(fdir),
        "entries": entries,
        "counts": {"total": len(entries),
                   "to_memories": sum(1 for e in entries if e["target"] == "memories"),
                   "out_of_scope": sum(1 for e in entries if e["target"] == "out_of_scope")},
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
        if e["target"] == "out_of_scope" and "hold_text" in e.get("mapping", {}):
            problems.append({"legacy_id": e["legacy_id"], "issue": "out_of_scope_body_present"})
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
    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("--source", required=True)
    p_snap.add_argument("--dest")
    p_snap.add_argument("--out")
    p_rr = sub.add_parser("dry-run-real")
    p_rr.add_argument("--fixtures", required=True)
    p_rr.add_argument("--out")
    p_ver = sub.add_parser("verify")
    p_ver.add_argument("--report", required=True)
    p_app = sub.add_parser("apply")
    p_app.add_argument("--report", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "inventory":
        r = inventory(args.source, args.out)
    elif args.cmd == "dry-run":
        r = dry_run(args.fixtures, args.out)
    elif args.cmd == "snapshot":
        r = snapshot(args.source, args.dest)
    elif args.cmd == "dry-run-real":
        r = dry_run_real(args.fixtures, args.out)
    elif args.cmd == "verify":
        r = verify(args.report)
    else:
        r = apply_from_report(args.report)
    return 0 if r.get("ok") else 1


def apply_from_report(report_path: str) -> dict:
    """迁移演练 apply：把 dry-run 报告（合成 fixture）落到当前正式库并核对。

    仅接受 fixture 规模的报告（与 dry-run 同防）；真实数据 apply 需获准快照。
    """
    from . import db as _db
    from .identity import service as identity
    from .memory import service as memory
    _migrator = identity.Principal("system", "迁移执行器", "system", "migration",
                                   "binding_migration")
    p = Path(report_path)
    report = json.loads(p.read_text(encoding="utf-8"))
    entries = report.get("entries", [])
    if len(entries) > 50:
        return {"ok": False, "error": "报告超出合成样本规模；真实迁移需获准快照"}
    fdir = Path(report["fixture_dir"])
    applied, problems, skipped_existing = [], [], []
    from datetime import datetime as _dt
    from .retrieval import projection as _pj
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
        # MIG-06 幂等：同 (legacy_id, source_type) 已迁移 -> 返回既有映射
        ensure_id_map_schema()
        with _db.formal() as conn:
            prior = conn.execute(
                "SELECT new_id, payload_hash FROM migration_id_map WHERE"
                " legacy_id=? AND source_type=?", (e["legacy_id"], e["target"])
            ).fetchone()
        if prior:
            if prior["payload_hash"] != recomputed:
                problems.append({"legacy_id": e["legacy_id"],
                                 "issue": "id_map_hash_mismatch"})
            else:
                skipped_existing.append({"legacy_id": e["legacy_id"],
                                         "new_id": prior["new_id"]})
            continue
        if e["target"] == "memories":
            cats = e["mapping"].get("categories") or ["daily"]
            # CB-003（2026-10-02 审计 P1）：Memory 创建、置顶/隐藏/复审
            # 标记与 ID 映射同一写事务提交。此前 hold 自带事务先提交、
            # 其余副作用在 autocommit 里各自落盘——中间中断会让重试
            # 再建一个正式 Memory（一源两资源一映射）。
            with _db.formal() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    out = memory.hold_in_tx(
                        conn, _migrator,
                        text=body.strip(),
                        why_remember=meta.get("why_remembered"),
                        memory_date=e["mapping"]["memory_date"],
                        date_confidence="inferred",
                        categories=cats)
                    mid = out["memory_id"]
                    if e["mapping"].get("pinned"):
                        conn.execute("UPDATE memories SET pinned=1 WHERE memory_id=?",
                                     (mid,))
                    if e.get("migrate_as_hidden"):
                        conn.execute("UPDATE memories SET visibility='hidden',"
                                     " updated_at=? WHERE memory_id=?",
                                     (_dt.utcnow().isoformat(), mid))
                        _pj.remove(conn, mid)  # 隐藏不进新检索（§20.3）
                    if e.get("needs_migration_review"):
                        conn.execute(
                            "INSERT OR IGNORE INTO memory_tags(memory_id, namespace,"
                            " tag, whose, confidence, created_by) VALUES(?,"
                            " 'migration', 'needs_review', 'qiaosheng', 'system',"
                            " 'migration')", (mid,))
                    conn.execute(
                        "INSERT INTO migration_id_map(legacy_id, source_type, new_id,"
                        " payload_hash, migrated_at) VALUES(?,?,?,?,?)",
                        (e["legacy_id"], "memories", mid, recomputed,
                         _dt.utcnow().isoformat()))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            applied.append({"legacy_id": e["legacy_id"], "new_id": mid,
                            "target": "memories"})
        else:
            # out_of_scope（信件等已拆出 mariposa 的资源）：跳过不落地
            skipped_existing.append({"legacy_id": e["legacy_id"],
                                     "reason": "out_of_scope"})
            continue
    # 逐项核对：新库可读、pinned 保留
    verified = 0
    for a in applied:
        with _db.formal() as conn:
            row = conn.execute("SELECT pinned FROM memories WHERE memory_id=?",
                               (a["new_id"],)).fetchone()
        if row is not None:
            verified += 1
    result = {"ok": not problems, "applied": len(applied),
              "skipped_already_migrated": len(skipped_existing),
              "verified": verified, "problems": problems,
              "note": "幂等：重复 apply 按 migration_id_map 返回既有映射；"
                      "真实数据 apply 仍需另行授权"}
    print(json.dumps({k: v for k, v in result.items() if k != "problems"},
                     ensure_ascii=False))
    return result


def ensure_id_map_schema() -> None:
    from . import db as _db
    with _db.formal() as conn:
        conn.execute("""
CREATE TABLE IF NOT EXISTS migration_id_map(
  legacy_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  new_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  migrated_at TEXT NOT NULL,
  PRIMARY KEY(legacy_id, source_type)
)""")


if __name__ == "__main__":
    sys.exit(main())
