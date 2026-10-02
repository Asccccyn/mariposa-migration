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
                "SELECT version_no, hold_text, event_text FROM"
                " memory_versions"
                " WHERE memory_id=? AND representation='full'"
                " ORDER BY version_no DESC LIMIT 1", (mid,)).fetchone()
            # CB-026：两种合法历史正文形态——event_text（v2 列）与
            # hold_text（v1 旧数据）；version_body 同一口径，此前只看
            # hold_text 把 event_text 完整版本误判 LEGACY_CONTENT_GAP
            body = ((full["event_text"] if full else None)
                    or (full["hold_text"] if full else None) or "")
            if full and body.strip():
                report["recoverable"] += 1
                report["items"].append({
                    "memory_id": mid,
                    "action": "restore_full_version",
                    "source_version": full["version_no"],
                    # RA-025：绑定报告生成时的目标当前版本——apply 时
                    # 重验，dry-run 后的并发写入使旧 report 拒绝生效
                    "target_version_at_report": r["current_version_no"]})
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
                    "SELECT version_no, hold_text, event_text FROM"
                    " memory_versions"
                    " WHERE memory_id=? AND version_no=?",
                    (mid, it["source_version"])).fetchone()
                m = conn.execute(
                    "SELECT current_version_no, compression_state FROM"
                    " memories WHERE memory_id=?",
                    (mid,)).fetchone()
                # RA-025：dry-run 后状态变化（正常修订/已恢复并前进）
                # ——旧 report 拒绝生效，不得把当前正文改回旧版本
                expected_v = it.get("target_version_at_report")
                if expected_v is not None and \
                        m["current_version_no"] != expected_v:
                    continue  # 状态已前进：本项跳过（no-op）
                body = ((full["event_text"] if full else None)
                        or (full["hold_text"] if full else None) or "")
                # CB-026：幂等——最新版本已是同内容 restore 产物则
                # 跳过（重复 apply 不再每次追加新版本）
                latest = conn.execute(
                    "SELECT origin_kind, payload_hash FROM"
                    " memory_versions WHERE memory_id=? AND version_no=?",
                    (mid, m["current_version_no"])).fetchone()
                body_hash = __import__("hashlib").sha256(
                    body.encode()).hexdigest()
                if (latest and latest["origin_kind"] == "restore"
                        and latest["payload_hash"] == body_hash):
                    continue
                new_v = m["current_version_no"] + 1
                conn.execute(
                    "INSERT INTO memory_versions(memory_id, version_no,"
                    " representation, hold_text, compressed_summary,"
                    " why_remember, authored_by, confirmed_by, origin_kind,"
                    " payload_hash, created_at)"
                    " VALUES(?,?,'full',?,NULL,NULL,'offline_v17',NULL,"
                    "'restore',?,datetime('now'))",
                    (mid, new_v, body, body_hash))
                conn.execute(
                    "UPDATE memories SET current_version_no=?,"
                    " compression_state='full',"
                    " updated_at=datetime('now') WHERE memory_id=?",
                    (new_v, mid))
                # CB-026：恢复后同步重建检索投影（此前成功恢复不满足
                # 当前检索结构，retrieval_documents 仍指向遗忘摘要）
                from mariposa.memory import service as _mem
                _mem.rebuild_full_projection(conn, mid)
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
