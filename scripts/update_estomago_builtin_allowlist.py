#!/usr/bin/env python3
"""同步 estómago 内置绑定（estomago_builtin）的 capabilities_allowlist（2026-10-22 她指令）。

背景：estómago 主模型凭据是受限白名单 binding（jiaming 主体）。发币时白名单=14 项；
后续 est 侧新增了 I 面/计划/订正/删除评审/原话/keep/关系/recollections 等工具，
白名单从未同步——char调用 memory_i_write 被 FORBIDDEN（I_FORBIDDEN，
2026-10-22 她报障"mariposa 的 I char说不会写"）。这正是
issue_estomago_hold_token.py 护栏注释警告的场景。

本脚本只做白名单热更新（不换 token、不重启——绑定在请求时读库，立即生效）：
  - 目标：全部未撤销的 estomago_builtin 绑定（jiaming 主体）
  - 备份：改前把旧 JSON 原样写入同目录 allowlist-backup-<时间>.json（可手工回滚）
  - 幂等：已是目标值则跳过
  - 白名单来源：est 调用面清点（apps/host 全部 mariposa 能力调用，
    jiaming 主体；surfacing 四项走 estomago_surface 专用 binding 不在此列；
    source.ingest* 走 worker 归档 binding 不在此列）。

用法（生产，改库前自动备份）：
  cd <mariposa 仓> && MARIPOSA_ROOT=<生产根> .venv/bin/python scripts/update_estomago_builtin_allowlist.py [--dry-run]

回滚：把备份文件里的旧数组写回 client_bindings.capabilities_allowlist 即可。
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from mariposa import db  # noqa: E402

ENTRY_SOURCE = "estomago_builtin"

# est jiaming 主体的当前全部调用面（2026-10-22 清点；与 issue_estomago_hold_token.py 同步维护）
ALLOWLIST = [
    # —— hold 面（原 14 项原样保留，含已不在调用面的 recall.status/accept/reject 三项——
    #    白名单只做加法不做减法，防未清点路径回归）——
    "memory.hold", "memory.hold.status", "memory.mood.vocab",
    "bootstrap.get", "bootstrap.next",
    "memory.recall.start", "memory.recall.refine", "memory.recall.page",
    "memory.recall.status", "memory.recall.accept", "memory.recall.reject",
    "episode.apply", "episode.get", "episode.list",
    # —— I 面（本次修复主目标：char自维护 I 条目）——
    "i.get", "i.versions.read",
    "i.items.list", "i.item.get",
    "i.item.create", "i.item.revise", "i.item.restore", "i.item.history",
    # —— 计划面 ——
    "plan.create", "plan.update", "plan.list", "plan.get",
    # —— 订正/关系面 ——
    "memory.relations.correct", "memory.relations.link", "memory.relations.list",
    "relations.corrections.list", "source.binding.correct",
    "memory.our_words.source.correct",
    # —— 原话（recollections）面 ——
    "memory.recollections.append", "memory.recollections.list", "memory.recollections.revise",
    # —— 桶读/维护面 ——
    "memory.open", "memory.get", "memory.update", "memory.view.confirm",
    "memory.keep.revoke", "memory.keeps.list",
    # —— 删除评审面（char决定侧）——
    "memory.deletion.decide", "memory.deletion.get", "memory.deletion.list",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    target = json.dumps(ALLOWLIST, ensure_ascii=False)
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT binding_id, principal_id, revoked, capabilities_allowlist "
            "FROM client_bindings WHERE entry_source = ? AND revoked = 0",
            (ENTRY_SOURCE,),
        ).fetchall()
        if not rows:
            print("没有未撤销的 estomago_builtin 绑定——先确认发币记录")
            return 1

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = Path(__file__).resolve().parent / f"allowlist-backup-{stamp}.json"
        changed = 0
        for r in rows:
            old = r["capabilities_allowlist"]
            if old == target:
                print(f"{r['binding_id']}: 已是目标白名单（{len(ALLOWLIST)} 项），跳过")
                continue
            changed += 1
            if args.dry_run:
                print(f"{r['binding_id']}: dry-run——将从 {len(json.loads(old)) if old else 'unrestricted'} 项更新为 {len(ALLOWLIST)} 项")
                continue
            conn.execute(
                "UPDATE client_bindings SET capabilities_allowlist = ? WHERE binding_id = ?",
                (target, r["binding_id"]),
            )
            print(f"{r['binding_id']}: 白名单已更新为 {len(ALLOWLIST)} 项（principal={r['principal_id']}）")

    if changed and not args.dry_run:
        backup_path.write_text(json.dumps(
            {r["binding_id"]: r["capabilities_allowlist"] for r in rows}, ensure_ascii=False, indent=2) + "\n")
        print(f"旧值备份：{backup_path}")
        print("立即生效（请求时读库）；回滚=把备份内旧值写回 capabilities_allowlist。")
    elif not changed:
        print("无需变更。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
