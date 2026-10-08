"""召回判断总开关政策（MANUAL_HANDOFF_JUDGE_SWITCH_V1，2026-10-08 裁定）。

持久正本 = 正式库 recall_judge_policy（部署级单行，迁移 35）：

- enabled=1 + provider 就绪 → 所选 provider 单独判断，原有有界交付
  （JUDGE_CAP=40 送判 / DELIVERY_LIMIT=3）；
- enabled=1 + provider 缺失/超时/非法输出/无许可 → 结构化
  unavailable/partial——不暗切 provider、不暗改关闭、不假报"没有相关记忆"；
- enabled=0（明确人类政策记录）→ 零判断构造/调用/缓存读取/网络/子进程，
  本次候选全集分页交付（bypassed_by_user）。

"关闭"只能来自明确的人类政策记录；缺行/损坏/未知 provider 一律
"未配置"（阻断正文），不解释成关闭直出（J05）。env（MARIPOSA_RECALL_
JUDGE_PROVIDER）只在政策表为空时作**首次导入**，不与数据库形成两套
运行时优先级。estómago 不保存开关副本。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .. import config, db
from ..errors import Forbidden

POLICY_VERSION = "recall-v1.8"
#: 已注册 provider（写侧白名单；codex_sdk 槽位由 WP6 落地，读侧仍可
#: 显示"未安装"而非未知）
KNOWN_PROVIDERS = ("typesafe_jev", "codex_sdk")


def _provider_known(name: str | None) -> bool:
    """读侧已知判定：正式 provider 或当前测试注入（register_for_tests）。

    生产进程 _INJECTED 恒空；测试沿用"改 config.RECALL_JUDGE_PROVIDER
    + 注入 fake"的旧模式时，注入名按已知处理，避免被误判未配置。
    """
    if not name:
        return False
    if name in KNOWN_PROVIDERS:
        return True
    from ..retrieval.judges import base as _jb
    return name in _jb._INJECTED


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_bootstrapped() -> None:
    """env 首次导入（生产一次性；测试随 config 属性变化重导入）。

    - 政策表为空 → 按旧 env 语义种入：有效已知 provider（typesafe_
      jev）→ enabled=1 保留开启；disabled/未配 → enabled=1+provider
      =NULL（未配置阻断，与旧行为同语义，不升级时偷偷放宽）；
    - 行仍是 env 导入态（updated_by=system:env_import）且 config 所指
      provider 变为已知 → 跟随更新（部署后补配 env 的升级路径）；
    - **人类写过政策后（updated_by≠env_import）env 永久失效**——
      数据库是唯一运行时正本，不形成两套优先级。
    """
    with db.formal() as conn:
        row = conn.execute(
            "SELECT provider, updated_by FROM recall_judge_policy"
            " WHERE id=1").fetchone()
        attr = (config.RECALL_JUDGE_PROVIDER or "").strip()
        provider = attr if _provider_known(attr) else None
        if row is not None:
            if row["updated_by"] != "system:env_import" or \
                    (row["provider"] or None) == provider:
                return
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 写锁内双检：并发只有一个赢家
            row = conn.execute(
                "SELECT provider, updated_by FROM recall_judge_policy"
                " WHERE id=1").fetchone()
            if row is not None and (
                    row["updated_by"] != "system:env_import"
                    or (row["provider"] or None) == provider):
                conn.execute("ROLLBACK")
                return
            if row is None:
                conn.execute(
                    "INSERT INTO recall_judge_policy(id, revision, enabled,"
                    " provider, model_id, allowed_data, updated_by,"
                    " updated_at)"
                    " VALUES(1, 1, 1, ?, NULL, '{}',"
                    " 'system:env_import', ?)", (provider, _now()))
            else:
                conn.execute(
                    "UPDATE recall_judge_policy SET provider=?,"
                    " updated_at=? WHERE id=1"
                    " AND updated_by='system:env_import'",
                    (provider, _now()))
            # OR IGNORE：env 重导入（部署后补配）不重复记 bootstrap 幂等行
            conn.execute(
                "INSERT OR IGNORE INTO recall_judge_policy_history("
                "revision, enabled, provider, model_id, changed_by,"
                " idempotency_key, created_at) VALUES(1, 1, ?, NULL,"
                " 'system:env_import', 'bootstrap-env-import', ?)",
                (provider, _now()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_policy() -> dict:
    """读当前政策行（不触发任何模型/网络调用）。

    返回 present=False 表示尚无政策记录（新实例待配置）。
    坏行（负 revision/非法 enabled）按未配置返回并带 degraded 标注，
    不冒充任何一种模式。
    """
    ensure_bootstrapped()
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM recall_judge_policy WHERE id=1").fetchone()
    if row is None:
        return {"present": False, "revision": 0, "enabled": None,
                "provider": None, "model_id": None, "mode": "unconfigured",
                "degraded_reasons": ["policy_row_missing"],
                "policy_version": POLICY_VERSION}
    out = {"present": True,
           "revision": int(row["revision"]),
           "enabled": bool(row["enabled"]),
           "provider": row["provider"],
           "model_id": row["model_id"],
           "policy_version": POLICY_VERSION,
           "updated_by": row["updated_by"], "updated_at": row["updated_at"]}
    degraded: list[str] = []
    if row["revision"] < 1:
        degraded.append("policy_revision_invalid")
    try:
        allowed = json.loads(row["allowed_data"] or "{}")
        if not isinstance(allowed, dict):
            raise ValueError
    except (ValueError, TypeError):
        allowed = {}
        degraded.append("policy_allowed_data_invalid")
    out["allowed_data"] = allowed
    if out["enabled"] is False:
        # 唯一合法"关闭"形态：明确人类记录。provider 配置保留展示
        #（关开关不销毁所选 provider 配置），但不再被构造/调用。
        out["mode"] = "off"
    elif _provider_known(out["provider"]):
        out["mode"] = "on"
    else:
        # enabled=1 但 provider 缺失/未知 → 未配置（阻断），不是关闭；
        # 未知 provider 名也不被解释成关闭直出（J05）
        out["mode"] = "unconfigured"
        if out["provider"]:
            degraded.append(f"provider_unknown:{out['provider']}")
    out["degraded_reasons"] = degraded
    return out


def effective() -> dict:
    """检索路径的政策快照：mode/judge_required/revision 一次取齐。

    每次查询固定 policy_revision 与模式；续页/重放核对同一快照。
    judge_required：只有明确关闭（off）才为 False——on 与 unconfigured
    都要求判断（unconfigured 因 provider 缺失而阻断正文，≠关闭直出）。
    """
    p = get_policy()
    p["judge_required"] = p["mode"] != "off"
    return p


def update_policy(principal_id: str, *, expected_revision: int,
                  enabled: bool, provider: str | None,
                  model_id: str | None = None,
                  idempotency_key: str | None = None) -> dict:
    """人类政策写入（CAS + 幂等 + 审计；仅 qiaosheng，能力层同权校验）。

    - expected_revision 必须匹配当前行（无行时为 0）；不匹配 →
      REVISION_CONFLICT，不盲覆盖；
    - provider 写侧白名单：None 或 KNOWN_PROVIDERS；未知名拒绝，
      不落坏行再靠读侧兜底；
    - 相同 idempotency_key 重复提交只应用一次（重放返回已生效政策）；
    - 关闭（enabled=False）保留 provider/model_id 值——重开时配置还在。
    """
    if principal_id != "qiaosheng":
        raise Forbidden(
            "召回判断政策仅人类网页登录（qiaosheng）可写；模型与 "
            "worker 无切换权", code="FORBIDDEN", principal=principal_id)
    if provider is not None and provider not in KNOWN_PROVIDERS:
        raise Forbidden(
            f"provider 必须是 {list(KNOWN_PROVIDERS)} 或 null（未配置）",
            code="INVALID_ARGUMENT", provider=provider)
    if enabled and provider is None:
        raise Forbidden(
            "开启判断必须选择 provider（先配 provider 再开，或保持关闭）",
            code="INVALID_ARGUMENT")
    ensure_bootstrapped()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key:
                hit = conn.execute(
                    "SELECT revision FROM recall_judge_policy_history"
                    " WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if hit is not None:
                    conn.execute("ROLLBACK")
                    return get_policy()
            row = conn.execute(
                "SELECT revision FROM recall_judge_policy WHERE id=1"
            ).fetchone()
            current = int(row["revision"]) if row else 0
            if int(expected_revision) != current:
                raise Forbidden(
                    "政策 revision 冲突：页面数据已过期，请刷新后重试",
                    code="REVISION_CONFLICT",
                    expected_revision=int(expected_revision),
                    current_revision=current)
            new_revision = current + 1
            new_enabled = 1 if enabled else 0
            if row is None:
                conn.execute(
                    "INSERT INTO recall_judge_policy(id, revision, enabled,"
                    " provider, model_id, allowed_data, updated_by,"
                    " updated_at) VALUES(1, ?, ?, ?, ?, '{}', ?, ?)",
                    (new_revision, new_enabled, provider, model_id,
                     principal_id, _now()))
            else:
                conn.execute(
                    "UPDATE recall_judge_policy SET revision=?, enabled=?,"
                    " provider=?, model_id=?, updated_by=?, updated_at=?"
                    " WHERE id=1",
                    (new_revision, new_enabled, provider, model_id,
                     principal_id, _now()))
            conn.execute(
                "INSERT INTO recall_judge_policy_history(revision, enabled,"
                " provider, model_id, changed_by, idempotency_key,"
                " created_at) VALUES(?,?,?,?,?,?,?)",
                (new_revision, new_enabled, provider, model_id,
                 principal_id, idempotency_key, _now()))
            conn.execute("COMMIT")
            # 审计事件在事务外补写（隔离事务自开连接——事务内调用会
            # 自锁 formal 库）；政策变更的原子留痕=上方 history 行
            from ..audit import service as _audit
            _audit.record_isolated(
                "recall.judge_policy.updated", principal_id,
                resource_id="recall_judge_policy",
                payload={"revision": new_revision,
                         "enabled": bool(new_enabled),
                         "provider": provider,
                         "model_id": model_id,
                         "idempotency_key": idempotency_key})
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK")
            # 唯一索引兜底的幂等碰撞（并发同 key）：返回已生效政策
            if idempotency_key and "idx_judge_policy_history_idem" in str(e):
                return get_policy()
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_policy()


def provider_readiness(provider: str | None) -> dict:
    """provider 就绪探针（零网络/零模型调用；J07：刷新页面不触发推理）。

    typesafe_jev：复用 replay 同款信号（构造器 disabled_reason/数据
    profile/API key 缺失）。codex_sdk：槽位在 WP6 前 explicit blocked。
    """
    if provider is None:
        return {"provider": None, "ready": False,
                "blocked_reason": "provider_not_selected"}
    if provider not in KNOWN_PROVIDERS:
        return {"provider": provider, "ready": False,
                "blocked_reason": "provider_unknown"}
    if provider == "typesafe_jev":
        from ..retrieval.judges import base as _jb
        from ..retrieval.judges import typesafe_jev as _tj
        p = _jb.get_provider_by_name("typesafe_jev")
        reason = None
        if isinstance(p, _tj.TypeSafeJevJudge):
            if getattr(p, "_disabled_reason", None):
                reason = p._disabled_reason
            elif not (getattr(p, "_data_profile", None) or frozenset()):
                reason = "allowed_data_profile_empty"
            elif (not getattr(p, "_api_key", None)
                  and type(p).judge is _tj.TypeSafeJevJudge.judge):
                reason = "api_key_missing"
        else:
            reason = "constructor_unavailable"
        return {"provider": provider, "ready": reason is None,
                "blocked_reason": reason}
    if provider == "codex_sdk":
        try:
            from ..retrieval.judges import codex_sdk as _cs
        except ImportError:
            return {"provider": provider, "ready": False,
                    "blocked_reason": "sdk_not_installed"}
        return _cs.readiness()
    return {"provider": provider, "ready": False,
            "blocked_reason": "provider_unknown"}
