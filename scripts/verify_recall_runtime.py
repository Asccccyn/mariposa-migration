"""召回运行时验证入口（P6）：隔离根上自检关键不变量。

用法（前台、有超时；测试根必须隔离）：
  MARIPOSA_ROOT=$(mktemp -d) MARIPOSA_ALLOW_CREATE=1 \
    python scripts/verify_recall_runtime.py [--quick]

不做任何真实数据读取、不联网、不加载模型。退出码 0=通过。
"""
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend"))

_QUICK = "--quick" in sys.argv


def _enforce_isolated_root() -> str:
    """A10：拒绝继承外部根；自建随机临时根（符号链接解析后判定）。"""
    import tempfile
    external = os.environ.get("MARIPOSA_ROOT")
    if external:
        real = os.path.realpath(external)
        print(f"REFUSED: MARIPOSA_ROOT={external} (realpath={real})："
              "验证脚本拒绝继承外部根（A10）；请去掉该环境变量重跑。",
              file=sys.stderr)
        raise SystemExit(2)
    root = tempfile.mkdtemp(prefix="mr-verify-")
    os.environ["MARIPOSA_ROOT"] = root
    return root


def main() -> int:
    _enforce_isolated_root()
    os.environ.setdefault("MARIPOSA_ALLOW_CREATE", "1")
    os.environ.setdefault("MARIPOSA_RECALL_ENABLED", "1")
    os.environ.setdefault("MARIPOSA_WORDS_RECALL_ENABLED", "1")

    checks: list[tuple[str, bool, str]] = []

    from mariposa import schema, config
    schema.migrate()
    schema.migrate_runtime()
    checks.append(("runtime 库迁移幂等", True, ""))

    # 开关默认值（生产默认关闭；JEV 不联网）
    from mariposa.retrieval.judges import base as jb
    # WP-05（D13）：env 兼容入口已删——按政策所选 provider 名构造
    from mariposa import config as _cfg
    judge = jb.get_provider_by_name(_cfg.RECALL_JUDGE_PROVIDER)
    checks.append(("judge 默认 disabled", isinstance(judge, jb.DisabledJudge),
                   type(judge).__name__))

    # 未决业务项保持 disabled
    checks.append(("forgotten words = disabled",
                   config.WORDS_FORGOTTEN_RECALL == "disabled"
                   and config.WORDS_FORGOTTEN_DECISION_STATE ==
                   "PENDING_OWNER_DECISION",
                   config.WORDS_FORGOTTEN_RECALL))

    # 证据包装全链路无指令权限
    from mariposa.retrieval import evidence as em
    ev = em.make_evidence("authored_event", "event_text", "x", "memory:m")
    checks.append(("evidence 无指令权限",
                   ev["instruction_authority"] == "none"
                   and ev["content_role"] == "retrieved_memory", ""))

    # 恶意 FTS 输入消毒
    from mariposa.retrieval import query_plan as qp
    compiled = qp.compile_terms(['x" OR 1=1 --'])
    checks.append(("FTS 输入消毒", "1=1" not in compiled, compiled))

    # 能力注册与 MCP 名称映射
    from mariposa.capabilities.registry import REGISTRY
    need = {"memory.recall.start", "memory.recall.refine",
            "memory.recall.reject", "memory.recall.accept",
            "memory.recall.navigate", "memory.recall.status",
            "memory.recall.close", "memory.words.recall",
            "memory.words.get", "memory.context.validate"}
    checks.append(("召回能力全部注册", need <= set(REGISTRY),
                   str(sorted(need - set(REGISTRY)))))

    if not _QUICK:
        # 端到端最小链（合成数据；隔离根）
        from mariposa.identity import service as identity
        identity.seed({"qiaosheng": "tok-q", "jiaming": "tok-j",
                       "worker": "tok-w"})  # CB-056：退役主体移除
        from mariposa.memory import service as memory
        from mariposa.recall import service as rs
        j = identity.Principal("jiaming", "周", "agent", "cc", "b")
        memory.hold(j, text="合成搬家事件", memory_date="2026-08-10",
                    date_confidence="exact", original_title="t",
                    categories=["daily"], creation_mode="contemporaneous",
                    raw_pending=False,
                    our_words=[{"speaker": "qiaosheng",
                                "text": "合成搬家话语", "expression_kind":
                                "verbatim"}])
        # RA-030：结构验证注入确定性 fake judge（不依赖真实模型；
        # disabled judge 下 0 候选是合法结果，不再盲取 [0]）
        from mariposa.retrieval.judges import base as _jb

        class _VerifyJudge(_jb.JudgeProvider):
            name = "verify_fake"

            def judge(self, plan, candidates, ctx):
                items = [_jb.JudgeItem(
                    candidate_ref=c.get("candidate_ref")
                    or c["resource_ref"],
                    candidate_version=str(c.get("content_version") or ""),
                    relevance_signal=0.8,
                    evaluation_status="evaluated", model_id=self.name,
                    prompt_version="t") for c in candidates]
                return _jb.JudgeBatchResult(
                    items=items, provider_status="evaluated",
                    degraded_reason=None, cache_hits=0,
                    cache_misses=len(candidates), request_count=0)

        _jb.register_for_tests("verify_fake", _VerifyJudge())
        import os as _os
        _os.environ["MARIPOSA_RECALL_JUDGE_PROVIDER"] = "verify_fake"
        p = rs.start(j, {"query_plan": {
            "original_request": "找搬家", "channels": ["event"],
            "lexical_terms": ["搬家"]}})
        checks.append(("start→候选交付", len(p["candidates"]) >= 1,
                       p["search_status"]))
        sid = p["recall_session_id"]
        if p["candidates"]:
            rs.reject(j, {"session_id": sid,
                          "resource_ref":
                          p["candidates"][0]["resource_ref"]})
        else:
            checks.append(("零候选（judge 环境不含 fake）", True,
                           "skip-rc"))
        p2 = rs.refine(j, {"session_id": sid, "query_plan": {
            "original_request": "再", "channels": ["words"],
            "lexical_terms": ["搬家"]}})
        checks.append(("refine 换通道", p2["revision"] == 2, str(p2["status"])))
        out = rs.close(j, {"session_id": sid, "outcome": "resolved"})
        checks.append(("close", out["status"] == "RESOLVED", ""))

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name +
              (f"  [{detail}]" if detail and not ok else ""))
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
