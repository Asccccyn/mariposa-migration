"""md 对话转写导入回归（裁定 2026-10-04 四——CURRENT §6）。

合成夹具（不用任何真实文档）：双方言解析、时间口径（claude Z=UTC、
gemini 裸时间 UTC+8）、时间戳行剥离、Thinking 分离、说话人映射、
确定性 id 幂等、坏格式结构化拒绝、出站时间分钟精度。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from mariposa import db
from mariposa.errors import MariposaError
from mariposa.identity import service as identity
from mariposa.source import importer, query
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


CLAUDE_MD = """# 合成会话标题

**Created:** 2026/9/20 02:08:37
**Updated:** 2026/9/20 03:00:00
**Exported:** 2026/10/3 08:19:04
**Model:** claude-sonnet-4-6
**Link:** [https://claude.ai/chat/494b2db6-b513-4663-be19-9781230c4658](https://claude.ai/chat/494b2db6-b513-4663-be19-9781230c4658)

---

## User
**2026-09-19T18:08:41.958Z**

合成用户消息正文。

## Claude
**2026-09-19T18:12:05.343Z**

### Thinking
````
合成思考过程，不应进入正文。
````

合成回复正文第一段。

```markdown
# 围栏内的标题不该被切
## User
```

合成回复正文第二段。

## User
**2026-09-19T18:20:00.000Z**

第二条用户消息。
"""

GEMINI_MD = """> From: https://gemini.google.com/u/1/app/d2d3aa87da9106f2

# you asked
message time: 2026-10-02 23:47:15

合成 gemini 用户消息。

---

# gemini response

合成 gemini 回复正文。

---

# you asked
message time: 2026-10-03 00:00:11

跨日第二条用户消息。
"""


def _tmp_md(content: str, name: str) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".md", prefix=name,
                                    delete=False, mode="w",
                                    encoding="utf-8")
    f.write(content)
    f.close()
    return f.name


class TestClaudeDialect:

    def test_import_and_shape(self, actors):
        path = _tmp_md(CLAUDE_MD, "synclaude")
        r = importer.import_file("jiaming", path)
        assert r["status"] == "completed", r
        assert r["provider"] == "claude"
        with db.formal() as conn:
            conv = conn.execute(
                "SELECT * FROM source_conversations WHERE"
                " provider_conversation_id=?",
                ("494b2db6-b513-4663-be19-9781230c4658",)).fetchone()
            assert conv, "会话 id 应取自 Link"
            assert conv["title"] == "合成会话标题"
            msgs = conn.execute(
                "SELECT * FROM source_messages WHERE conversation_id=?"
                " ORDER BY sequence", (conv["id"],)).fetchall()
        assert len(msgs) == 3
        # 时间：Z=UTC 直存；剥离后正文不含时间戳行
        assert msgs[0]["created_at"].startswith("2026-09-19T18:08:41")
        assert "18:08" not in (msgs[0]["text"] or "")
        assert msgs[0]["normalized_sender"] == "human"
        assert msgs[0]["speaker"] == "qiaosheng"
        assert msgs[1]["normalized_sender"] == "assistant"
        assert msgs[1]["speaker"] == "jiaming"
        # Thinking 分离：标志+不进正文；正文含围栏内伪标题（未被切）
        assert msgs[1]["has_thinking"] == 1
        assert "合成思考过程" not in (msgs[1]["text"] or "")
        assert "围栏内的标题不该被切" in (msgs[1]["text"] or "")
        assert len(msgs) == 3  # 围栏内 ## User 没有产生第四条
        # parent 链
        assert msgs[1]["parent_provider_message_id"] == msgs[0][
            "provider_message_id"]
        assert msgs[2]["parent_provider_message_id"] == msgs[1][
            "provider_message_id"]
        # 发布门禁
        assert all(m["published"] == 1 for m in msgs)

    def test_reimport_idempotent(self, actors):
        path = _tmp_md(CLAUDE_MD, "synclaude2")
        r1 = importer.import_file("jiaming", path)
        r2 = importer.import_file("jiaming", path)
        assert r1["status"] == "completed"
        assert r2["status"] == "already_imported"
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM source_messages WHERE"
                " provider_conversation_id=?",
                ("494b2db6-b513-4663-be19-9781230c4658",)).fetchone()["c"]
        assert n == 3, "重导不重复"


class TestGeminiDialect:

    def test_import_and_tz(self, actors):
        path = _tmp_md(GEMINI_MD, "syngemini")
        r = importer.import_file("jiaming", path)
        assert r["status"] == "completed", r
        assert r["provider"] == "gemini"
        with db.formal() as conn:
            conv = conn.execute(
                "SELECT * FROM source_conversations WHERE"
                " provider_conversation_id=?",
                ("d2d3aa87da9106f2",)).fetchone()
            assert conv, "会话 id 应取自 From URL 末段"
            assert conv["title"] and conv["title"].startswith("syngemini"), \
            conv["title"]  # 文件名 stem（含随机后缀）
            msgs = conn.execute(
                "SELECT * FROM source_messages WHERE conversation_id=?"
                " ORDER BY sequence", (conv["id"],)).fetchall()
        assert len(msgs) == 3
        # 裸时间 2026-10-02 23:47:15 +8 → UTC 15:47
        assert msgs[0]["created_at"].startswith("2026-10-02T15:47:15")
        # 跨日 00:00:11 +8 → 前一日 16:00 UTC
        assert msgs[2]["created_at"].startswith("2026-10-02T16:00:11")
        assert msgs[0]["speaker"] == "qiaosheng"
        assert msgs[1]["speaker"] == "jiaming"
        assert "message time" not in (msgs[0]["text"] or "")
        # 正文剥离时间行后保留内容
        assert "合成 gemini 用户消息" in (msgs[0]["text"] or "")


class TestBadFormat:

    def test_unrecognized_md_rejected(self, actors):
        path = _tmp_md("# 随便的文档\n\n没有说话人标题。\n", "badmd")
        with pytest.raises(MariposaError) as ei:
            importer.import_file("jiaming", path)
        assert ei.value.code in ("SOURCE_MD_FORMAT", "SOURCE_FORMAT_UNKNOWN")

    def test_claude_md_without_messages_rejected(self, actors):
        path = _tmp_md("# 只有标题\n**Created:** 2026/9/20 02:08:37\n",
                       "emptymd")
        with pytest.raises(MariposaError):
            importer.import_file("jiaming", path)


class TestMinutePrecision:

    def test_outbound_times_minute_only(self, actors):
        path = _tmp_md(CLAUDE_MD, "mintest")
        importer.import_file("jiaming", path)
        out = query.search("合成")
        for hit in out["hits"]:
            ts = hit.get("created_at") or ""
            assert ts == "" or len(ts) <= 16, \
                f"出站时间只到分钟：{ts}"
            assert "Z" not in ts and "." not in ts
        # 具体格式核对：上海时区显示
        hit = next(h for h in out["hits"]
                   if "合成用户消息" in (h.get("text") or ""))
        assert hit["created_at"] == "2026-09-20 02:08", hit["created_at"]


class TestFullAuditP1:
    """fe2ed05 全量审计 P1 回归：围栏边界 / 内容锚定 id / 前插重导。"""

    CLAUDE_FENCE = """# 围栏边界

**Link:** [https://claude.ai/chat/fence-conv-1](x)

---

## Claude
**2026-09-19T18:00:00.000Z**

````markdown
内部示例开头
## User
内部示例里的伪用户行
````

围栏后的正文。

## User
**2026-09-19T18:05:00.000Z**

### Thinking
这只是人类正文里的普通标题，没有围栏。

人类正文继续。
"""

    def test_four_backtick_fence_and_plain_thinking(self, actors):
        path = _tmp_md(self.CLAUDE_FENCE, "fence")
        r = importer.import_file("jiaming", path)
        assert r["status"] == "completed", r
        with db.formal() as conn:
            msgs = conn.execute(
                "SELECT * FROM source_messages WHERE"
                " provider_conversation_id='fence-conv-1'"
                " ORDER BY sequence").fetchall()
        # 两条消息（围栏内 ## User 不切块）
        assert len(msgs) == 2, [m["normalized_sender"] for m in msgs]
        assert msgs[0]["normalized_sender"] == "assistant"
        assert msgs[1]["normalized_sender"] == "human"
        # 围栏内容留在 assistant 正文且不被当标题
        assert "内部示例里的伪用户行" in (msgs[0]["text"] or "")
        assert msgs[0]["has_thinking"] == 0
        # 普通 Thinking 标题保留在人类正文
        assert "### Thinking" in (msgs[1]["text"] or "")
        assert "人类正文继续" in (msgs[1]["text"] or "")
        assert msgs[1]["has_thinking"] == 0

    GEMINI_CODE_TS = """> From: https://gemini.google.com/u/1/app/gts-1

# you asked
message time: 2026-10-02 23:47:15

正文开头。

```text
message time: 1999-13-45 99:99:99
```

正文结尾。
"""

    def test_gemini_timestamp_only_at_block_head(self, actors):
        path = _tmp_md(self.GEMINI_CODE_TS, "gts")
        r = importer.import_file("jiaming", path)
        assert r["status"] == "completed", r
        with db.formal() as conn:
            msgs = conn.execute(
                "SELECT * FROM source_messages WHERE"
                " provider_conversation_id='gts-1'").fetchall()
        assert len(msgs) == 1
        # 真实时间在块首（+8→UTC 15:47）；代码内示例保留在正文
        assert msgs[0]["created_at"].startswith("2026-10-02T15:47:15")
        assert "1999-13-45" in (msgs[0]["text"] or "")

    def test_prepend_reimport_content_anchored_ids(self, actors):
        base = """**Link:** [https://claude.ai/chat/prepend-1](x)

---

## User
**2026-09-19T10:00:00.000Z**

alpha 正文。

## User
**2026-09-19T11:00:00.000Z**

beta 正文。
"""
        prepended = """**Link:** [https://claude.ai/chat/prepend-1](x)

---

## User
**2026-09-19T09:00:00.000Z**

recovered 新消息。

## User
**2026-09-19T10:00:00.000Z**

alpha 正文。

## User
**2026-09-19T11:00:00.000Z**

beta 正文。
"""
        p1 = _tmp_md(base, "pb1")
        r1 = importer.import_file("jiaming", p1)
        assert r1["status"] == "completed"
        p2 = _tmp_md(prepended, "pb2")
        r2 = importer.import_file("jiaming", p2)
        assert r2["status"] == "completed", r2
        with db.formal() as conn:
            msgs = conn.execute(
                "SELECT text FROM source_messages WHERE"
                " provider_conversation_id='prepend-1'"
                " AND published=1 ORDER BY created_at").fetchall()
        texts = [m["text"] for m in msgs]
        # 三条全部可见：前插不吞新增、不重复旧文
        assert texts == ["recovered 新消息。", "alpha 正文。",
                         "beta 正文。"], texts
        stats = r2["stats"]
        # 身份不变的验收：三条全部可见且不重复。alpha 的 parent 链
        # 在文件里真实变化（None→recovered），记一条版本冲突属实的
        # 版本历史——不是身份漂移
        assert stats.get("version_conflicts", 0) <= 1

    def test_bad_date_structured(self, actors):
        bad = """# 坏日期

## User
**2026-13-99T99:99:99.999Z**

正文。
"""
        path = _tmp_md(bad, "baddate")
        with pytest.raises(MariposaError) as ei:
            importer.import_file("jiaming", path)
        assert ei.value.code in ("SOURCE_MD_FORMAT", "SOURCE_FORMAT")




def _hold_w(actors, text):
    from mariposa.memory import service as memory
    return memory.hold(
        actors["jiaming"], text=text, memory_date="2026-09-20",
        date_confidence="exact", original_title="w3",
        categories=["daily"], creation_mode="contemporaneous",
        raw_pending=False)


class TestFullAuditP2Misc:
    """fe2ed05 全量审计 P2 抽样回归：上传入口/WR-02/WR-03。"""

    def test_upload_keeps_md_extension(self, actors):
        from mariposa.app import app
        from fastapi.testclient import TestClient
        from mariposa import config as cfg
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.put("/api/source/upload?filename=chat.md",
                      content=("# you asked\nmessage time: 2026-10-02"
                               " 23:47:15\n\n上传md正文\n").encode(),
                      headers={"Authorization": "Bearer " + _token()})
        assert r.status_code == 200, r.text
        path = r.json()["data"]["upload_path"]
        assert path.endswith(".md.part"), \
            "SRC-03：上传须保留扩展名供导入分流；SRC-08：.part 进清理"
        ri = importer.import_file("jiaming", path)
        assert ri["status"] == "completed", ri

    def test_import_filename_null_accepted(self, actors):
        from mariposa.capabilities import registry
        import tempfile
        tmp = tempfile.mkdtemp()
        f = f"{tmp}/nn.md"
        open(f, "w").write("# you asked\nmessage time: 2026-10-02 23:47:15\n\nnull文件名正文\n")
        # P1-01（2026-10-05 审计）：宿主直读限 qiaosheng——本用例主题
        # 是 filename=None，身份换成直读许可持有者
        qs = identity.Principal("qiaosheng", "江乔生", "human", "web", "bq")
        r = registry.invoke(qs, "source.import",
                            {"path": f, "filename": None}, None)
        assert r["data"]["status"] == "completed"

    def test_wr02_daily_with_plan_ids_binds(self, actors):
        from mariposa import db
        from mariposa.plans import service as plans
        from mariposa.memory import service as mem
        p = plans.create("jiaming", "WR02计划", state="active")
        out = mem.hold(actors["jiaming"], text="WR02正文",
                       memory_date="2026-09-20",
                       date_confidence="exact", original_title="w2",
                       categories=["daily"],
                       creation_mode="contemporaneous",
                       raw_pending=False, plan_ids=[p["plan_id"]])
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM plan_memory_links WHERE"
                " memory_id=?", (out["memory_id"],)).fetchone()["c"]
        assert n == 1, "WR-02：daily+plan_ids 不得静默丢绑定"

    def test_wr03_revoke_empty_source_rejected(self, actors):
        from mariposa.errors import Forbidden
        from mariposa.memory import our_words as ow
        out = _hold_w(actors, "WR03正文")
        with db.formal() as conn:
            wid = conn.execute(
                "SELECT word_id FROM memory_our_words WHERE memory_id=?",
                (out["memory_id"],)).fetchone()
        assert wid is None
        # 手工造一条无来源话语
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO memory_our_words(word_id, memory_id, ordinal,"
                " speaker, text, expression_kind, source_ref, created_by,"
                " created_at) VALUES('ow-wr03', ?, 1, 'qiaosheng',"
                " '无来源话语', 'verbatim', NULL, 'jiaming',"
                " datetime('now'))", (out["memory_id"],))
        with pytest.raises(Forbidden):
            ow.correct_source("jiaming", "ow-wr03", None,
                              "remove_wrong_binding", replacement=None,
                              note=None, expected_source_version=0)


def _token():
    # 测试根无 devseed 文件——直接给 jiaming 造一条绑定 token
    import mariposa.identity.service as ids
    import hashlib as _hl
    import secrets as _sec
    tok = "t-" + _sec.token_urlsafe(12)
    with __import__("mariposa.db", fromlist=["db"]).formal() as conn:
        conn.execute(
            "INSERT INTO client_bindings(binding_id, token_hash,"
            " principal_id, entry_source, revoked, created_at)"
            " VALUES('bind-upload-test', ?, 'jiaming', 'web', 0,"
            " datetime('now'))",
            (_hl.sha256(tok.encode()).hexdigest(),))
    return tok
