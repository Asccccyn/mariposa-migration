#!/usr/bin/env python3
"""surface 专用受限 binding 签发【仅 dry-run 计划】（EST_MARIPOSA_AUTO_SURFACING_V1 ①）。

她 2026-10-19 裁定：①同意但**暂不实际签发生产凭据**——本脚本只打印
将执行的确切计划（binding 身份/allowlist/E 侧接线/env 名），并校验前置
条件；不建绑定、不生成 token、不写库、不碰 pbcopy。实际签发等她批准
后按本计划人工/另批执行（先例：issue_stream_token.py 一 stream 一凭据，
token 明文 pbcopy 盲搬）。

allowlist 硬边界（她裁定 1）：只放行四个 surface 能力——检索候选、严格
判断、本地校验、按桶只读详情。hold/修改/删除/明确打开/确认回忆/主动
recall 一概不在内；principal 映射 jiaming 仅为通过 identity 校验，能力面
由本 allowlist 收死（比 jiaming 本人的主动权限窄）。

用法（dry-run）：
  MARIPOSA_ROOT=<生产根> .venv/bin/python scripts/surface_binding_plan.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

SURFACE_ALLOWLIST = [
    "memory.surfacing.prepare",
    "memory.surfacing.validate",
    "memory.surfacing.check",
    "memory.detail.read",
]
#: E 侧 config.integrations.mariposa.surfacing_credential_ref 应指向的
#: 环境变量名（estómago LaunchAgent start.sh 注入；值绝不落 plist/日志）
CREDENTIAL_ENV = "ESTOMAGO_MARIPOSA_SURFACE_TOKEN"

PLANNED_BINDING = {
    "binding_id": "binding_jiaming_surface",
    "principal_id": "jiaming",
    "entry_source": "estomago_surface",
    "capabilities_allowlist": SURFACE_ALLOWLIST,
    "note": "与char主动 recall 的既有绑定（binding_jiaming，无 allowlist=全权）"
            "物理分立；撤销互不影响",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="surface binding 签发计划（dry-run）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()

    from mariposa import config, db
    from mariposa.capabilities import registry

    # —— 前置校验（只读）——
    checks: list[tuple[str, bool, str]] = []
    for name in SURFACE_ALLOWLIST:
        cap = registry.REGISTRY.get(name)
        checks.append((f"registry 已注册 {name}", cap is not None,
                        "" if cap else "未注册"))
    checks.append((
        "四能力均为运行时隔离面（不进正式幂等缓存）",
        all(registry._is_recall_runtime(n) for n in SURFACE_ALLOWLIST), ""))
    checks.append((
        "无 hold/修改/删除/open/confirm/recall 能力混入",
        not any(("hold" in n or "update" in n or "delete" in n
                 or "open" in n or "confirm" in n or n.startswith(
                     "memory.recall")) for n in SURFACE_ALLOWLIST), ""))
    with db.formal() as conn:
        row = conn.execute(
            "SELECT binding_id, capabilities_allowlist, revoked FROM"
            " client_bindings WHERE binding_id=?",
            (PLANNED_BINDING["binding_id"],)).fetchone()
    checks.append((
        "目标 binding 未被占用（不存在或未撤销）",
        row is None or not row["revoked"],
        f"现状={dict(row) if row else 'absent'}"))
    with db.recall_runtime() as conn:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    checks.append(("运行库 surface 表就绪（迁移 16）",
                   {"surface_operations", "surface_runs"} <= tables, ""))

    ok = all(c[1] for c in checks)
    plan = {
        "mode": "DRY-RUN（不签发、不写库、不生成 token）",
        "planned_binding": PLANNED_BINDING,
        "credential_env": CREDENTIAL_ENV,
        "est_side_wiring": [
            "estómago config: integrations.mariposa.surfacing_credential_ref"
            f" = '{CREDENTIAL_ENV}'",
            "estómago LaunchAgent start.sh: export "
            f"{CREDENTIAL_ENV}=<批准后签发的 token（pbcopy 盲搬）>",
            "mariposa env: MARIPOSA_SURFACE_ENABLED / 预算 env 均未配置"
            "（保持关闭）",
        ],
        "issuance_sql_for_review": (
            "INSERT INTO client_bindings(binding_id, token_hash, principal_id,"
            " entry_source, capabilities_allowlist, created_at) VALUES(?,?,?,?,"
            "?,datetime('now'))  -- token=secrets.token_urlsafe 经"
            " identity._hash_token 哈希入库；明文零落屏（先例 issue_stream_token.py）"
        ),
        "rollback": "UPDATE client_bindings SET revoked=1 WHERE binding_id="
                    f"'{PLANNED_BINDING['binding_id']}'（一条 SQL 即断权限）",
        "checks": [{"item": c[0], "ok": c[1], "detail": c[2]} for c in checks],
    }
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=1))
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=1))
    print("\n[plan] 前置校验:", "全部通过" if ok else "存在未过项（见上）")
    print("[plan] 本脚本恒为 dry-run：实际签发需她批准后另行执行")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
