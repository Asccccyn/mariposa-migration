#!/usr/bin/env python3
"""为 estómago 归档 worker 签发受限 stream 凭据（迁移 30，2026-10-05）。

做三件事（一条链）：
  1. 新建受限 binding：principal=worker、entry_source=estomago_archive、
     capabilities_allowlist=["source.ingest","source.ingest.status"]；
  2. 登记 stream grant：origin 实例/房间/说话人服务端钉死；
  3. token 明文经 pbcopy 盲搬给她的密码管理器——不落屏、不落库、
     不进命令行参数（凭证纪律）。

用法：
  MARIPOSA_ROOT=<生产根> .venv/bin/python scripts/issue_stream_token.py \
      --stream-id <stream> --origin-instance estomago \
      --origin-conversation-id <room> [--senders user,assistant]

重复运行同一 stream 会因 UNIQUE 冲突拒绝——一 stream 一凭据；换凭据
先撤旧 grant（ingest.revoke_grant）再发新的。
"""
import argparse
import json
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from mariposa import db, identity, schema  # noqa: E402
from mariposa.source import ingest as source_ingest  # noqa: E402

ALLOWLIST = ["source.ingest", "source.ingest.status"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream-id", required=True)
    ap.add_argument("--origin-instance", required=True)
    ap.add_argument("--origin-conversation-id", required=True)
    ap.add_argument("--senders", default="user,assistant")
    ap.add_argument("--scope", default="private",
                    help="owner scope（首版私聊=private）")
    args = ap.parse_args()

    schema.migrate()
    schema.migrate_runtime()
    senders = [s.strip() for s in args.senders.split(",") if s.strip()]

    token = secrets.token_urlsafe(32)
    binding_id = f"bdg_{secrets.token_hex(10)}"
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO client_bindings(binding_id, token_hash,"
                " principal_id, entry_source, capabilities_allowlist,"
                " created_at) VALUES(?,?,?,?,?,datetime('now'))",
                (binding_id, identity.service._hash_token(token),
                 "worker", "estomago_archive",
                 json.dumps(ALLOWLIST)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    try:
        grant = source_ingest.create_grant(
            binding_id, args.stream_id, args.origin_instance,
            args.origin_conversation_id, senders, args.scope,
            created_by="qiaosheng")
    except Exception:
        with db.formal() as conn:
            conn.execute(
                "DELETE FROM client_bindings WHERE binding_id=?",
                (binding_id,))
        raise

    try:
        subprocess.run(["pbcopy"], input=token.encode(), check=True)
        copied = True
    except (OSError, subprocess.CalledProcessError):
        copied = False
    try:
        subprocess.run(["pbcopy"], input=token.encode(), check=True)
        copied = True
    except (OSError, subprocess.CalledProcessError):
        copied = False
    if copied:
        print("✓ 受限 binding 已建（principal=worker，白名单："
              + ", ".join(ALLOWLIST) + ")")
        print(f"✓ stream grant 已登记：{json.dumps(grant, ensure_ascii=False)}")
        print("✓ token 已复制到剪贴板（pbcopy）——粘贴到你的密码管理器；"
              "本脚本不会再次显示它")
    else:
        # 自审⑥：token 没到剪贴板=永久丢失——当即将 binding/grant 撤销
        # 成不可用态，重跑本脚本全新签发（旧 binding 留审计痕迹）
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE client_bindings SET revoked=1 WHERE"
                    " binding_id=? AND revoked=0", (binding_id,))
                conn.execute(
                    "UPDATE source_stream_grants SET revoked_at=datetime"
                    "('now') WHERE stream_id=? AND revoked_at IS NULL",
                    (args.stream_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        print("⚠ pbcopy 不可用：token 未能交付——binding 与 grant 已当场"
              "撤销（防丢 token 变孤儿），修复剪贴板后直接重跑本脚本")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
