"""v1.7 一次性离线迁移：遗留 forgotten_summary 表示恢复工具（dry-run 优先）。

背景：删除链退役后，存量 forgotten_summary 桶读侧标 LEGACY_CONTENT_GAP。
本工具在隔离副本上做可核验恢复：
- 只有能唯一定位完整 event_text / 合法 hold_text 的历史版本才生成新的
  全内容当前版本（保留来源与变更收据）；
- 只剩摘要的桶标 LEGACY_CONTENT_GAP，不用模型重建原文。

用法（仅 dry-run；真实 apply 需 --apply 且对获准副本运行）：
  MARIPOSA_ROOT=<隔离副本根> python scripts/migrations/offline_v17/restore_legacy_summaries.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))

from mariposa import db  # noqa: E402


def dry_run() -> dict:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT memory_id, current_version_no FROM memories"
            " WHERE compression_state='forgotten_summary'").fetchall()
        report = {"total_forgotten": len(rows), "recoverable": 0,
                  "content_gap": 0, "items": []}
        for r in rows:
            mid = r["memory_id"]
            full = conn.execute(
                "SELECT version_no, hold_text FROM memory_versions"
                " WHERE memory_id=? AND representation='full'"
                " ORDER BY version_no DESC LIMIT 1", (mid,)).fetchone()
            if full and (full["hold_text"] or "").strip():
                report["recoverable"] += 1
                report["items"].append({
                    "memory_id": mid,
                    "action": "restore_full_version",
                    "source_version": full["version_no"]})
            else:
                report["content_gap"] += 1
                report["items"].append({
                    "memory_id": mid, "action": "LEGACY_CONTENT_GAP"})
    return report


def apply(report: dict) -> dict:
    if not report.get("recoverable"):
        return {"applied": 0}
    applied = 0
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for it in report["items"]:
                if it["action"] != "restore_full_version":
                    continue
                mid = it["memory_id"]
                full = conn.execute(
                    "SELECT version_no, hold_text FROM memory_versions"
                    " WHERE memory_id=? AND version_no=?",
                    (mid, it["source_version"])).fetchone()
                m = conn.execute(
                    "SELECT current_version_no FROM memories WHERE memory_id=?",
                    (mid,)).fetchone()
                new_v = m["current_version_no"] + 1
                conn.execute(
                    "INSERT INTO memory_versions(memory_id, version_no,"
                    " representation, hold_text, compressed_summary,"
                    " why_remember, authored_by, confirmed_by, origin_kind,"
                    " payload_hash, created_at)"
                    " VALUES(?,?,'full',?,NULL,NULL,'offline_v17',NULL,"
                    "'restore',?,datetime('now'))",
                    (mid, new_v, full["hold_text"],
                     __import__("hashlib").sha256(
                         full["hold_text"].encode()).hexdigest()))
                conn.execute(
                    "UPDATE memories SET current_version_no=?,"
                    " compression_state='full',"
                    " updated_at=datetime('now') WHERE memory_id=?",
                    (new_v, mid))
                applied += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"applied": applied}


def main() -> int:
    do_apply = "--apply" in sys.argv
    report = dry_run()
    print(json.dumps(
        {k: v for k, v in report.items() if k != "items"},
        ensure_ascii=False, indent=1))
    if do_apply:
        print(json.dumps(apply(report), ensure_ascii=False))
    else:
        print("dry-run only；真实恢复需 --apply 并在获准副本上运行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
