"""Codex SDK 判断 provider（MANUAL_HANDOFF_JUDGE_SWITCH_V1 §5，WP6）。

设计边界（她裁定 2026-10-08）：
- Codex 是**第二个可选判断器**，不是第二个主模型：与 Jev 消费同一份事实
  材料（查询语义目标 + 获准候选证据投影，带 candidate_ref/content_version/
  证据角色），绝不串联成"Jev 先筛、Codex 再看剩下的"；
- 输出只含候选 ID/相关判断/允许短依据——Mariposa 按 ID 装配原记录；
  Codex 不生成回忆故事、不写记忆；
- 外发许可与 Jev **分立配置**（MARIPOSA_CODEX_ALLOWED_DATA，词表同
  S15 profile；未配/拼错 fail-closed=无任何许可，不转授 Jev 的批准）；
- 判断缓存与 Jev 分键（prompt/schema 版本不同 → cache_key 不同；不读
  Jev 分数缓存）；
- 关闭模式完全不进入本模块（recall/judge_policy 在构造前拦截）。

锁定版本：openai-codex 0.161.0（隔离 venv 实测签名；Python ≥3.10，
pydantic v2）。真实调用链：Codex(config) → thread_start(ephemeral=True,
sandbox=read_only, cwd=隔离空目录) → thread.run(input, output_schema=…)
→ Turn.items 解析 assistant JSON。**live 未实测**（无认证）：本文件离线
实现 + fake transport 测试；真实 smoke 是她批准后的独立 live 项。

readiness（零网络/零模型）：SDK 未装/未认证/未配 model → blocked（如
实；不冒称可用，不影响关闭模式与其他 provider）。
"""
from __future__ import annotations

import json
import os
import threading
import time

from ... import config
from . import base

#: 锁定版（隔离 venv /tmp 探针实测；升级须重跑 J09-J12 离线套件）
CODEX_SDK_VERSION = "0.161.0"
CODEX_PROMPT_VERSION = "codex-sdk-structured-v1"
CODEX_SCHEMA_VERSION = "codex-output-v1"

#: Codex 判断的外发许可词表（与 Jev S15 同义；许可本身分立配置）
CODEX_GRANT_WORDS = ("event_excerpt", "word_excerpt", "source_excerpt",
                     "title_cue")

#: 出站判断请求的 deadline（ms）——与 Jev 同为有界等待；默认值未经实测，
#: 真实耗时上限属 live 验收（不冒称已测性能）
CODEX_TIMEOUT_MS = int(os.environ.get(
    "MARIPOSA_CODEX_JUDGE_TIMEOUT_MS", "20000"))

#: 输出 JSON Schema（锁定版 output_schema: JsonObject——pydantic dict 包装；
#: 参数名以锁定版签名为准，live 校验后如需调整须重跑离线套件）
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_ref": {"type": "string"},
                    "candidate_version": {"type": ["string", "null"]},
                    "relevant": {
                        "type": ["string", "null"],
                        "enum": ["relevant", "uncertain", "irrelevant",
                                 None],
                    },
                    "reason": {"type": ["string", "null"], "maxLength": 200},
                },
                "required": ["candidate_ref", "relevant"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_RELEVANCE_MAP = {"relevant": 1.0, "uncertain": 0.0, "irrelevant": -1.0}

#: 进程内串行锁：并发 1（§5.1——不叠加并发判断负担）
_CODEX_LOCK = threading.Lock()

#: 测试注入点（离线 fake transport；不进生产配置——与 base._INJECTED 同
#: 惯例：生产进程恒为 None）
_FAKE_TRANSPORT = None


def set_transport_for_tests(transport) -> None:
    """注入 fake transport：fn(thread_spec: dict) -> dict（离线；零网络）。

    thread_spec: {prompt, output_schema, ephemeral, sandbox, cwd, timeout_ms}
    返回: {"status": "ok", "items": [...]} 或
          {"status": "error", "code": str}
    """
    global _FAKE_TRANSPORT
    _FAKE_TRANSPORT = transport


def clear_transport() -> None:
    global _FAKE_TRANSPORT
    _FAKE_TRANSPORT = None


def sdk_available() -> bool:
    try:
        import openai_codex  # noqa: F401
        return True
    except ImportError:
        return False


def _grants_env() -> str:
    return (os.environ.get("MARIPOSA_CODEX_ALLOWED_DATA", "")
            or os.environ.get("CODEX_ALLOWED_DATA", "")).strip()


def parse_grants(raw: str):
    """解析外发许可（fail-closed：拼错/未知词=整份拒绝，与 Jev S15 同口径）。"""
    if not raw:
        return frozenset()
    words = [w.strip() for w in raw.split(",") if w.strip()]
    if not words or any(w not in CODEX_GRANT_WORDS for w in words):
        return None  # 非法：整体禁用（不猜、不放宽）
    return frozenset(words)


def readiness() -> dict:
    """provider 就绪探针（零网络/零模型；网页刷新不触发推理——J07 同口径）。"""
    out = {"provider": "codex_sdk",
           "sdk_version": CODEX_SDK_VERSION if sdk_available() else None,
           "ready": False, "blocked_reason": None}
    if _FAKE_TRANSPORT is None and not sdk_available():
        out["blocked_reason"] = "sdk_not_installed"
        return out
    if _FAKE_TRANSPORT is None:
        has_key = bool((os.environ.get("MARIPOSA_CODEX_API_KEY", "")
                        or os.environ.get("OPENAI_API_KEY", "")).strip())
        if not has_key:
            out["blocked_reason"] = "auth_missing"
            return out
        model = (os.environ.get("MARIPOSA_CODEX_MODEL_ID", "")
                 or config.RECALL_JUDGE_MODEL_ID if
                 os.environ.get("MARIPOSA_CODEX_MODEL_ID", "") else "")
        if not model:
            out["blocked_reason"] = "model_missing"
            return out
    grants = parse_grants(_grants_env())
    if grants is None:
        out["blocked_reason"] = "allowed_data_invalid"
        return out
    if not grants:
        out["blocked_reason"] = "allowed_data_missing"
        return out
    out["ready"] = True
    return out


def _judge_prompt(query_plan: dict, candidates: list,
                  grants: frozenset | None = None) -> str:
    """出站判断请求（共享事实材料语义——与 Jev 同源投影，非 Jev 筛后集合）。

    只含：查询语义目标（anchors/original_request）与各候选的获准证据
    （candidate_ref/content_version/channel/证据角色+片段）。不含密钥、
    系统身份全文、UI CoT、无关聊天。
    RRA-024（2026-10-09 复审）：grants 给出时按**字段角色**过滤——同卡
    证据元素只在其 evidence_kind 对应许可内时外发；title_cue 仅在
    title_cue 许可下携带（此前只做卡级必要角色准入，同卡未许可证据/
    标题仍被无条件序列化）。
    """
    anchors = [t for t in ((query_plan.get("lexical_terms") or [])
                           + (query_plan.get("exact_phrases") or []))
               if isinstance(t, str) and t]
    payload = {
        "task": "判断每条候选记忆与查询的相关性。只输出结构化判断，"
                "不生成回忆故事，不改写候选内容。",
        "query": {
            "original_request": query_plan.get("original_request", ""),
            "semantic_query": query_plan.get("semantic_query", ""),
            "anchors": anchors,
        },
        "candidates": [],
    }
    def _ev_allowed(ev: dict) -> bool:
        if grants is None:
            return True
        if not ev.get("snippet"):
            return True  # 结构事实（无正文）恒可发
        kind = ev.get("evidence_kind") or ""
        role = {"authored_event": "event_excerpt",
                "event_excerpt": "event_excerpt",
                "word_excerpt": "word_excerpt",
                "raw_verbatim": "source_excerpt",
                "source_excerpt": "source_excerpt"}.get(kind)
        return role is None or role in grants

    for c in candidates:
        evs = []
        for ev in (c.get("evidence") or [])[:4]:
            if not _ev_allowed(ev):
                continue
            evs.append({
                "kind": ev.get("evidence_kind", ""),
                "field": ev.get("field", ""),
                "snippet": (ev.get("snippet") or "")[:600],
            })
        row = c.get("_row") or {}
        title_ok = (grants is None or "title_cue" in grants)
        payload["candidates"].append({
            "candidate_ref": c.get("candidate_ref") or c["resource_ref"],
            "candidate_version": c.get("content_version"),
            "channel": c.get("channel", "event"),
            "title_cue": ((row.get("title") or "")[:80]
                          if title_ok and isinstance(
                              row.get("title"), str) else ""),
            "evidence": evs,
        })
    return json.dumps(payload, ensure_ascii=False)


def _parse_items(raw_text: str, sent_refs: list, sent_versions: dict):
    """J09 严格校验：非法输出不冒充判断。

    返回 (items, status, reason)：
    - items: 通过对账的 JudgeItem 列表；
    - 陌生/重复 ref、版本错配、非法 relevant 值、NaN/inf/bool/越界、
      非法 JSON → status=unavailable + reason（不填 0 分、不静默截断）。
    """
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return [], "unavailable", "codex_output_not_json"
    if not isinstance(data, dict) or not isinstance(data.get("items"),
                                                    list):
        return [], "unavailable", "codex_output_shape_invalid"
    items = []
    seen = set()
    for row in data["items"]:
        if not isinstance(row, dict):
            return [], "unavailable", "codex_item_shape_invalid"
        ref = row.get("candidate_ref")
        if not isinstance(ref, str) or ref not in sent_refs:
            return [], "unavailable", "codex_unknown_ref"
        if ref in seen:
            return [], "unavailable", "codex_duplicate_ref"
        seen.add(ref)
        rel = row.get("relevant")
        if rel is None:
            items.append(base.JudgeItem(
                candidate_ref=ref,
                candidate_version=sent_versions.get(ref),
                evaluation_status="unavailable"))
            continue
        if rel not in _RELEVANCE_MAP:
            return [], "unavailable", "codex_relevant_invalid"
        ver = row.get("candidate_version")
        if ver is not None and not isinstance(ver, str):
            return [], "unavailable", "codex_version_type_invalid"
        want = sent_versions.get(ref)
        if ver and want and ver != want:
            return [], "unavailable", "codex_version_mismatch"
        reason = row.get("reason")
        if reason is not None and (not isinstance(reason, str)
                                   or len(reason) > 200):
            return [], "unavailable", "codex_reason_invalid"
        items.append(base.JudgeItem(
            candidate_ref=ref,
            candidate_version=ver if ver else want,
            relevance_signal=_RELEVANCE_MAP[rel],
            evaluation_status="evaluated",
            confidence_kind="not_applicable",
            model_id="codex_sdk",
            prompt_version=CODEX_PROMPT_VERSION,
            input_projection_version=str(
                config.PROJECTION_REVISION)))
    # 漏返回的送判候选按 unavailable 入账（不凭空消失）
    for ref in sent_refs:
        if ref not in seen:
            items.append(base.JudgeItem(
                candidate_ref=ref,
                candidate_version=sent_versions.get(ref),
                evaluation_status="unavailable"))
    return items, "evaluated", None


class CodexSdkJudge(base.JudgeProvider):
    """Codex SDK provider（openai-codex 0.161.0；live 未实测——见模块注释）。"""

    name = "codex_sdk"

    def __init__(self, *, model_id: str | None = None,
                 allowed_data=None):
        grants = parse_grants(_grants_env())
        self._grants = grants if grants is not None else frozenset()
        self._grants_invalid = grants is None
        # WP-02（CX-03）：政策传入即胜出（政策唯一正本，env 仅回落）；
        # 政策面合法值已经 update_policy 白名单校验，不重复 env 解析
        if allowed_data:
            self._grants = frozenset(allowed_data)
            self._grants_invalid = False
        self._model_id = model_id or (
            os.environ.get("MARIPOSA_CODEX_MODEL_ID", "")
            or "").strip() or None

    def apply_policy(self, *, model_id=None, allowed_data=None):
        """WP-02（CX-03）：政策值注入（政策唯一正本，env 仅回落）；
        政策面合法值已经 update_policy 白名单校验。"""
        if model_id:
            self._model_id = model_id
        if allowed_data:
            self._grants = frozenset(allowed_data)
            self._grants_invalid = False

    def outbound_grants(self) -> frozenset:
        """Codex 自身的外发许可（与 Jev 分立；拼错/未配=无许可 fail-closed）。"""
        return self._grants

    def judge(self, query_plan: dict, candidates: list,
              execution_context: dict) -> base.JudgeBatchResult:
        if self._grants_invalid:
            return base.JudgeBatchResult(
                provider_status="unavailable",
                degraded_reason="allowed_data_profile_invalid")
        if not self._grants:
            return base.JudgeBatchResult(
                provider_status="unavailable",
                degraded_reason="allowed_data_policy_missing")
        # WP-02（CX-15=C-008）：出站投影按卡必要角色过滤（与 replay/
        # selection 的 required_excerpt_roles 同源）——单许可下未许可卡
        # 的正文/证据不进 prompt；许可集为空已在上游 fail-closed。
        from .typesafe_jev import required_excerpt_roles
        authorized = [c for c in candidates
                      if required_excerpt_roles(c) <= self._grants]
        if not authorized and candidates:
            return base.JudgeBatchResult(
                provider_status="unavailable",
                degraded_reason="no_authorized_candidates")
        prompt = _judge_prompt(query_plan, authorized,
                               grants=self._grants)
        # RRA-025：对账身份从**实际送判集合**（authorized）派生——被
        # 许可过滤的候选不在送判集，provider 返回其 ref=陌生 ref 整批
        # 作废（此前 sent_refs 取过滤前全集，未送判 ref 被伪标 evaluated）
        sent_refs = [c.get("candidate_ref") or c["resource_ref"]
                     for c in authorized]
        sent_versions = {c.get("candidate_ref") or c["resource_ref"]:
                         (str(c["content_version"])
                          if c.get("content_version") is not None else None)
                         for c in authorized}
        # RRA-013：锁等待有界（排队不超 CODEX_TIMEOUT_MS；拿不到锁=
        # unavailable，不无限挂——run 自身无界如实声明）
        _deadline = time.monotonic() + CODEX_TIMEOUT_MS / 1000
        if not _CODEX_LOCK.acquire(timeout=max(0.1, CODEX_TIMEOUT_MS / 1000)):
            return base.JudgeBatchResult(
                provider_status="unavailable",
                degraded_reason="codex_lock_timeout")
        try:
            raw, err = self._call_model(prompt, _deadline)
        finally:
            _CODEX_LOCK.release()
        if err is not None:
            return base.JudgeBatchResult(
                provider_status="unavailable", degraded_reason=err)
        items, status, reason = _parse_items(raw, sent_refs,
                                             sent_versions)
        if status != "evaluated":
            return base.JudgeBatchResult(
                provider_status="unavailable", degraded_reason=reason)
        return base.JudgeBatchResult(items=items,
                                     provider_status="evaluated",
                                     degraded_reason=None,
                                     request_count=1)

    def _call_model(self, prompt: str, deadline: float | None = None):
        """一次判断请求（临时 thread；有界 deadline；受控关闭）。

        离线：fake transport（测试注入）。live：thread_start(ephemeral=
        True, sandbox=read_only, cwd=隔离空目录) → thread.run(output_schema)
        → items 解析（锁定版签名）。live 路径在认证就绪前不会被启用
        （readiness blocked → judge_policy 显示未就绪）。
        """
        if _FAKE_TRANSPORT is not None:
            deadline = time.monotonic() + CODEX_TIMEOUT_MS / 1000
            spec = {
                "prompt": prompt, "output_schema": _OUTPUT_SCHEMA,
                "ephemeral": True, "sandbox": "read_only",
                "cwd": "<isolated-empty>", "timeout_ms": CODEX_TIMEOUT_MS,
            }
            started = time.monotonic()
            out = _FAKE_TRANSPORT(spec)
            elapsed = time.monotonic() - started
            if elapsed > (deadline - started):
                return None, "timeout"
            if not isinstance(out, dict):
                return None, "transport_invalid"
            if out.get("status") != "ok":
                return None, str(out.get("code") or "transport_error")
            text = out.get("text")
            if not isinstance(text, str):
                return None, "transport_text_missing"
            return text, None
        if not sdk_available():
            return None, "sdk_not_installed"
        # —— live 路径（锁定版 0.161.0；未认证环境到不了这里）——
        try:
            from openai_codex import (Codex, CodexConfig, Sandbox,
                                       TextInput)
        except ImportError:
            return None, "sdk_not_installed"
        if deadline is not None and time.monotonic() >= deadline:
            return None, "codex_deadline_before_run"
        import shutil
        import tempfile
        client = Codex(CodexConfig())
        tmpdir = tempfile.mkdtemp(prefix="codex-judge-")
        try:
            thread = client.thread_start(
                model=self._model_id, sandbox=Sandbox.read_only,
                cwd=tmpdir, ephemeral=True,
                developer_instructions=_DEV_INSTRUCTIONS)
            # RRA-013（2026-10-09 复审）：锁定版 0.161.0 的 Thread.run
            # **无 timeout 参数**（inspect.signature 实证）——此前发明的
            # timeout kwarg 在真实 SDK 会 TypeError（fake 自行接受掩盖了
            # 不匹配）。调用按真实签名；deadline 语义如实降级：锁等待
            # （acquire timeout）+ run 前剩余时间检查有界，run 自身无
            # 中断机制（真实超时/隔离未兑现——live 前保持 readiness
            # blocked，RRA-018 如实声明）
            result = thread.run(
                [TextInput(text=prompt)],
                output_schema=_OUTPUT_SCHEMA,
                model=self._model_id)
            turn = getattr(result, "turn", result)
            texts = []
            for item in (getattr(turn, "items", None) or []):
                root = getattr(item, "root", item)
                if type(root).__name__ == "AgentMessageThreadItem":
                    msg = getattr(root, "message", None) or getattr(
                        root, "text", None)
                    if isinstance(msg, str):
                        texts.append(msg)
            if not texts:
                return None, "codex_no_agent_message"
            return "\n".join(texts), None
        except Exception as exc:  # noqa: BLE001——SDK 异常统一降级，不外泄细节
            return None, f"codex_error:{type(exc).__name__}"
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001——收尾失败不掩盖判断结果
                    pass
            # WP-02（E05/JFA-E05）：临时 thread 目录回收——judge N 次后
            # tempfile 根下不留 codex-judge-* 残留（不泄漏输入材料）
            shutil.rmtree(tmpdir, ignore_errors=True)


_DEV_INSTRUCTIONS = (
    "你是记忆召回的相关性判断器。只依据请求中给出的候选证据输出结构化"
    "判断（relevant/uncertain/irrelevant + ≤200字短依据）。不调用工具、"
    "不读取请求之外的文件、不生成回忆叙述、不改写候选内容。")
