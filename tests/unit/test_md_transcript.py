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
