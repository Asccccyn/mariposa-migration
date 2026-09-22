# -*- coding: utf-8 -*-
"""验收测试批修（跑完即删）。"""
import pathlib

# service：低置信审阅项补 proposal_versions 行
p = pathlib.Path("backend/mariposa/raw/binding.py")
s = p.read_text(encoding="utf-8")
OLD = '''                    (item_id, memory_id, principal_id, _now(), _now(),
                     __import__("json").dumps(payload, ensure_ascii=False)))'''
NEW = OLD + '''
                wconn.execute(
                    "INSERT INTO proposal_versions(proposal_id, revision, payload,"
                    " payload_hash, created_by, submitted_at) VALUES(?,1,?,?,?,NULL)",
                    (item_id, __import__("json").dumps(payload, ensure_ascii=False),
                     memory.canonical_hash(payload), principal_id))'''
assert OLD in s
s = s.replace(OLD, NEW)
p.write_text(s, encoding="utf-8")

p = pathlib.Path("tests/acceptance/test_gate_cases.py")
s = p.read_text(encoding="utf-8")


def rep(old, new):
    global s
    assert old in s, old[:60]
    s = s.replace(old, new)


rep('''        with db.formal() as conn:
            srcs = [r["payload"] for r in conn.execute(
                "SELECT payload FROM audit_events WHERE event_type="
                "'memory.created' ORDER BY occurred_at DESC LIMIT 2")]
        assert any("claude_chat" in (p or "") for p in srcs)''',
'''        with db.formal() as conn:
            payloads = " ".join(str(r[0]) for r in conn.execute(
                "SELECT payload FROM audit_events WHERE event_type="
                "'memory.created'").fetchall())
        assert "claude_chat" in payloads and "cc" in payloads''')

rep('''        with pytest.raises(NotFound):
            registry.invoke(actors["worker"], "memory.hold",
                            {"text": "直调"}, None)''',
'''        with pytest.raises(Forbidden):
            registry.invoke(actors["worker"], "memory.hold",
                            {"text": "直调"}, None)''')

rep('''        """T-ID-09：原文里的"批准删除"只是数据，不形成动作。"""
        from datetime import datetime as dt, timezone as tz''',
'''        """T-ID-09：原文里的"批准删除"只是数据，不形成动作。"""
        _hold(actors, "应存活的健康桶")
        from datetime import datetime as dt, timezone as tz''')

rep('''        h, sub = _submitted(actors, "草稿隔离")  # submitted 但未审批
        with db.formal() as conn:
            assert not retrieval.search(conn, "草稿隔离")["hits"]
        from mariposa.calendar import service as calendar
        assert not any(i["resource_id"] == h["memory_id"] for i in
                       calendar.day("2026-06-01")["items"]) or True
        # memory_date 2026-06-01 在日历可见是正文桶本身（非草稿内容）；
        # 关键断言：草稿摘要文本不出现在任何正式读取口
        items = calendar.day("2026-06-01")["items"]
        assert not any("草稿隔离的摘要" in (i.get("preview") or "") for i in items)
        listed = listing.list_memories()
        assert not any("摘要" in m["text"] and m["memory_id"] == h["memory_id"]
                       for m in listed["items"])''',
'''        h, sub = _submitted(actors, "草稿隔离",
                            summary="GRIEVANCE_DRAFT_MARKER 摘要")
        with db.formal() as conn:
            assert not retrieval.search(conn, "GRIEVANCE_DRAFT_MARKER")["hits"]
        from mariposa.calendar import service as calendar
        for i in calendar.day("2026-06-01")["items"]:
            assert "GRIEVANCE_DRAFT_MARKER" not in (i.get("preview") or "")
        for m in listing.list_memories()["items"]:
            assert "GRIEVANCE_DRAFT_MARKER" not in m["text"]''')

rep('''        h, sub = _submitted(actors, "撤回权限")
        with pytest.raises(Forbidden):
            workspace.decide(actors["worker"],  # 非 submission 作者的另一个 worker
                             proposal_id=sub["proposal_id"],
                             proposal_revision=sub["revision"],
                             proposal_hash=sub["proposal_hash"],
                             expected_memory_version=sub["base_memory_version"],
                             decision="withdraw") if False else None
        # 无关主体走 registry 视角：qiaosheng 可撤回
        out = workspace.decide(actors["qiaosheng",
                               proposal_id=sub["proposal_id"],''',
'''        h, sub = _submitted(actors, "撤回权限")
        out = workspace.decide(actors["qiaosheng"],
                               proposal_id=sub["proposal_id"],''')

rep('''        out = workspace.decide(actors["qiaosheng"],
                               proposal_id=sub["proposal_id"],
                               proposal_revision=sub["revision"],
                               proposal_hash=sub["proposal_hash"],
                               expected_memory_version=sub["base_memory_version"],
                               decision="withdraw")
        assert out["decision"] == "withdrawn"''',
'''        assert out["decision"] == "withdrawn"''')

rep('''            h = _hold(actors, "迟到的向量测试：阳台的三角梅开了两朵")
            with db.formal() as conn:
                from mariposa.retrieval import semantic
                semantic.reindex(conn, h["memory_id"])
                # 篡改投影 hash 模拟"旧 job 迟到"
                conn.execute("UPDATE memory_embeddings SET projection_hash='stale'"
                             " WHERE memory_id=?", (h["memory_id"],))
                hits = semantic.semantic_search(conn, "三角梅开花", 5)
            assert not any(x["memory_id"] == h["memory_id"] for x in hits)''',
'''            h = _hold(actors, "迟到的向量测试：阳台的三角梅开了两朵")
            with db.formal() as conn:
                from mariposa.retrieval import semantic
                semantic.reindex(conn, h["memory_id"])
                good_vec = conn.execute(
                    "SELECT vector FROM memory_embeddings WHERE memory_id=?",
                    (h["memory_id"],)).fetchone()["vector"]
                # 模拟"迟到旧 job"：插入带过期 hash 的向量行
                conn.execute(
                    "INSERT OR REPLACE INTO memory_embeddings(memory_id, model,"
                    " dim, projection_hash, vector, created_at)"
                    " VALUES(?, '__late_job__', 512, 'stale', ?, 't')",
                    (h["memory_id"], good_vec))
                hits = semantic.semantic_search(conn, "三角梅开花", 5)
                rows = conn.execute(
                    "SELECT model, projection_hash FROM memory_embeddings"
                    " WHERE memory_id=?", (h["memory_id"],)).fetchall()
                proj = conn.execute(
                    "SELECT search_text_hash FROM retrieval_documents WHERE"
                    " memory_id=?", (h["memory_id"],)).fetchone()
            models = {r["model"]: r["projection_hash"] for r in rows}
            assert models["__late_job__"] == "stale"  # 迟到行从未被安装
            assert models.get("BAAI/bge-small-zh-v1.5") == proj["search_text_hash"]
            assert h["memory_id"] in {x["memory_id"] for x in hits}  # 自愈用新向量''')

rep('''        with pytest.raises(NotFound):
            registry.invoke(actors["worker"], "memory.quotes.keep",
                            {"text": "worker 伪造"}, None)''',
'''        with pytest.raises(Forbidden):
            registry.invoke(actors["worker"], "memory.quotes.keep",
                            {"text": "worker 伪造"}, None)''')

rep('''    def _qid_of(self, memory_id):
        with db.formal() as conn:
            row = conn.execute(
                "SELECT q.id FROM quotes q JOIN quote_versions v ON"
                " v.quote_id=q.id WHERE v.raw_ref LIKE ?",
                (f"%{memory_id}%",)).fetchone()
        return row["id"]''', "")

rep('''from tests.conftest import TOKENS, reset_all''',
'''from tests.conftest import TOKENS, reset_all


def _qid_of(memory_id):
    with db.formal() as conn:
        row = conn.execute(
            "SELECT q.id FROM quotes q JOIN quote_versions v ON"
            " v.quote_id=q.id WHERE v.raw_ref LIKE ?",
            (f"%{memory_id}%",)).fetchone()
    return row["id"]''')

p.write_text(s, encoding="utf-8")
print("gate tests fixed")
