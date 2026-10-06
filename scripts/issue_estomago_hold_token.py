#!/usr/bin/env python3
"""给 estómago 发长期 hold 服务凭据（"内置绑定"，2026-10-05 她的裁定）。

estómago 的 DS 模型替周家明落笔记记忆——按"记忆由周家明落笔"的
架构约束，服务身份挂在 jiaming 主体下，**受限白名单**只放行它要用的
两个能力：

  - memory.hold（写记忆 + quotes 钉引用，operation_id 幂等）
  - memory.mood.vocab（词表只读，供工具说明书动态注入）

其余一切（读检索/删除/维护面）即使 token 泄漏也不可用；撤销走既有
binding 撤销（revoke / oauth/revoke），无过期（长期服务身份）。

⚠️ 运行顺序：必须**先重启生产服务（新代码）再跑本脚本**——脚本会跑
schema.migrate()，若在旧代码运行中执行会把库迁到新 schema（如
删 why_remember 列），旧代码随即报错。

用法：
  MARIPOSA_ROOT=<生产根> .venv/bin/python scripts/issue_estomago_hold_token.py

token 明文经 pbcopy 盲搬（不落屏、不落命令行、不落库——库里只有
SHA256）；重复运行签发全新绑定，旧绑定留审计痕迹，换凭据先撤旧。
"""
import json
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from mariposa import db, identity, schema  # noqa: E402

ALLOWLIST = ["memory.hold", "memory.mood.vocab"]
ENTRY_SOURCE = "estomago_builtin"


def main() -> int:
    schema.migrate()
    schema.migrate_runtime()

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
                 "jiaming", ENTRY_SOURCE, json.dumps(ALLOWLIST)))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    try:
        subprocess.run(["pbcopy"], input=token.encode(), check=True)
        copied = True
    except (OSError, subprocess.CalledProcessError):
        copied = False
    if not copied:
        # token 没到剪贴板=永久丢失——当即撤销防孤儿（同 issue_stream_token）
        with db.formal() as conn:
            conn.execute(
                "UPDATE client_bindings SET revoked=1 WHERE binding_id=?"
                " AND revoked=0", (binding_id,))
        print("⚠ pbcopy 不可用：token 未能交付——绑定已当场撤销，"
              "修复剪贴板后重跑")
        return 1
    print("✓ estómago hold 内置绑定已建（principal=jiaming，"
          "白名单：" + ", ".join(ALLOWLIST) + "）")
    print(f"✓ binding_id={binding_id}（撤销用）")
    print("✓ token 已复制到剪贴板（pbcopy）——粘到 estómago 配置的"
          " mariposa.credential_ref 所指位置；本脚本不会再次显示它")
    print("  提醒：粘贴完成后即可正常复制其他内容；若剪贴板被覆盖且"
          "未粘贴，重跑本脚本换新 token 并撤销旧绑定")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
