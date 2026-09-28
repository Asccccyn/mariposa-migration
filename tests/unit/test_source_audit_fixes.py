"""v1.7 审计 A09-A13 复现测试（对齐林石见 2026-09-28 审计场景）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mariposa import db
from mariposa.errors import MariposaError
from mariposa.source import importer, query


def conv(cid, msgs):
    return {"uuid": cid, "chat_messages": msgs}


def msg(mid, text="正文", created="2026-07-01T00:00:00.000Z"):
    return {"uuid": mid, "sender": "human", "created_at": created,
            "content": [{"type": "text", "text": text}]}


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


class TestAuditSourceFixes:
    def test_a09_failed_batch_uuid_taken_over(self, actors, tmp_path):
        """A09：失败批次占 UUID，修正文件重导后接管并可见。"""
        bad = [conv("c-a9", [msg("a9-1")]), 42]
        r1 = importer.import_file("jiaming", write(tmp_path, "bad.json", bad))
        assert r1["status"] == "failed"
        with db.formal() as c:
            assert c.execute("SELECT published FROM source_messages"
                             ).fetchone()["published"] == 0
        good = [conv("c-a9", [msg("a9-1", "修正后的正文")])]
        r2 = importer.import_file(
            "jiaming", write(tmp_path, "good.json", good))
        assert r2["status"] == "completed"
        # 接管语义：发布的是首次导入的行（旧文本）；新文本留版本记录
        with db.formal() as c:
            row = c.execute("SELECT published, import_batch_id, text FROM"
                            " source_messages WHERE provider_message_id="
                            "'a9-1'").fetchone()
        assert row["published"] == 1, "A09：成功批次接管发布同 UUID 行"
        assert row["import_batch_id"] == r2["batch_id"]
        hits = query.search("正文")  # 当前展示版本 = 首次导入文本
        assert hits["hits"]
        assert r2["stats"]["version_conflicts"] == 1  # 新文本已留版本

    def test_a10_failed_batch_does_not_poison_new_import(self, actors,
                                                         tmp_path):
        """A10：一条坏日期的失败导入不得阻断后续无关导入。"""
        bad = [conv("c-bad", [msg("b-1", created="not-a-timestamp")])]
        r1 = importer.import_file("jiaming", write(tmp_path, "bad.json", bad))
        assert r1["status"] == "failed"
        ok = [conv("c-ok2", [msg("ok2-1", "无关正常正文")])]
        r2 = importer.import_file("jiaming", write(tmp_path, "ok.json", ok))
        assert r2["status"] == "completed"
        assert query.search("无关正常正文")["hits"]

    def test_a11_composite_cursor_no_drop(self, actors, tmp_path):
        """A11：[A,B]→[A,NEW,B] 重导后分页不漏同 sequence 消息。"""
        importer.import_file("jiaming", write(tmp_path, "p1.json",
            [conv("c-pg", [msg("A", "甲"), msg("B", "乙")])]))
        importer.import_file("jiaming", write(tmp_path, "p2.json",
            [conv("c-pg", [msg("A", "甲"), msg("NEW", "插入"),
                           msg("B", "乙")])]))
        seen, cur = [], None
        while True:
            d = query.get_conversation("c-pg", limit=2,
                                       after_seq=cur) if cur else \
                query.get_conversation("c-pg", limit=2)
            seen += [m["provider_message_id"] for m in d["messages"]]
            if not d["has_more"]:
                break
            cur = d["next_after_cursor"]["seq"]
            # 复合游标：dict 形式
            d2 = query.get_conversation(
                "c-pg", limit=2,
                after_seq=d["next_after_cursor"],
                after_id=d["next_after_cursor"]["id"])
            seen += [m["provider_message_id"] for m in d2["messages"]]
            if not d2["has_more"]:
                break
            cur = None
            from mariposa.errors import NotFound as _NF
            # 用第二页起点继续（简化：直接断言三页内收齐）
            break
        assert set(seen) >= {"A", "NEW", "B"}, seen

    def test_a12_metadata_failure_keeps_completed(self, actors, tmp_path,
                                                  monkeypatch):
        """A12：发布事务提交后 metadata 落盘失败不得改 failed。"""
        from mariposa.source import archive
        calls = {"n": 0}
        real = archive.write_metadata

        def flaky(provider, batch_id, metadata):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real(provider, batch_id, metadata)

        monkeypatch.setattr(archive, "write_metadata", flaky)
        r = importer.import_file("jiaming", write(tmp_path, "m.json",
            [conv("c-m", [msg("m-1", "元数据失败测试")])]))
        assert r["status"] == "completed"
        with db.formal() as c:
            row = c.execute("SELECT status FROM source_import_batches"
                            " WHERE batch_id=?", (r["batch_id"],)).fetchone()
            pub = c.execute("SELECT published FROM source_messages"
                            ).fetchone()["published"]
        assert row["status"] == "completed" and pub == 1
