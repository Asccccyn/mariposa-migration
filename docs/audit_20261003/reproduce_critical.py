"""Isolated audit probes: exit 1 means a confirmed contract violation remains.

Run from the repository root with .venv/bin/python. Creates synthetic data only
in a fresh system temporary directory; no network, model load, or production DB.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "backend"), str(REPO)]
ISOLATED_ROOT = tempfile.mkdtemp(prefix="mariposa-audit-critical-")
os.environ.update(
    MARIPOSA_ROOT=ISOLATED_ROOT,
    MARIPOSA_ALLOW_CREATE="1",
    MARIPOSA_RECALL_ENABLED="1",
    MARIPOSA_WORDS_RECALL_ENABLED="1",
    MARIPOSA_RAW_FALLBACK_ENABLED="1",
    MARIPOSA_SEMANTIC_PROVIDER="",
    MARIPOSA_RECALL_JUDGE_PROVIDER="test_deterministic",
    HF_HUB_OFFLINE="1",
    TRANSFORMERS_OFFLINE="1",
    PYTHONDONTWRITEBYTECODE="1",
)
if hasattr(signal, "alarm"):
    signal.alarm(45)

from tests.conftest import reset_all  # noqa: E402
from mariposa import config, db  # noqa: E402
from mariposa.errors import ViewReceiptInvalid, StaleOperation  # noqa: E402
from mariposa.identity.service import Principal  # noqa: E402
from mariposa.memory import service as memory, extras, views, recollections  # noqa: E402
from mariposa.recall import service as recall, store  # noqa: E402
from mariposa.source import importer  # noqa: E402

P = Principal("jiaming", "synthetic audit owner", "agent", "cc", "audit-binding")


def hold(text, **kwargs):
    return memory.hold(
        P, text=text, memory_date="2026-10-03", categories=["daily"],
        date_confidence="exact", original_title="synthetic audit",
        creation_mode="contemporaneous", raw_pending=False, **kwargs,
    )["memory_id"]


def fresh_receipt_after_update():
    mid = hold("synthetic version one")
    extras.update_text("jiaming", mid, 1, text="synthetic version two")
    opened = views.open_memory(P, mid)
    views.confirm_view(P, mid, opened["view_receipt"])
    try:
        recollections.append(P, mid, opened["view_receipt"], "synthetic recollection", True)
    except ViewReceiptInvalid:
        return False, {"content_version": opened["version"],
                       "receipt_representation_version": opened["representation_version"]}
    return True, {"fresh_receipt_accepted": True}


def words_switch_applies_to_replay():
    hold("synthetic move event", our_words=[
        {"speaker": "qiaosheng", "text": "搬家一起挑窗帘", "expression_kind": "paraphrase"}
    ])
    args = {"query_plan": {"original_request": "搬家原话", "channels": ["words"],
                           "lexical_terms": ["搬家"]}}
    saved = recall.start(P, args)
    if not saved["candidates"]:
        raise RuntimeError("probe precondition: words candidate absent")
    config.RECALL_WORDS_ENABLED = False
    try:
        fresh = recall.start(P, args)
        try:
            replay = recall.revalidate_replayed("start", saved, args)
        except StaleOperation:
            return True, {"replay_rejected": True}
        return not replay["candidates"], {"fresh_count": len(fresh["candidates"]),
                                          "replay_count": len(replay["candidates"])}
    finally:
        config.RECALL_WORDS_ENABLED = True


def terminal_status_is_not_overwritten():
    args = {"query_plan": {"original_request": "nonexistent audit token",
                           "channels": ["event"], "lexical_terms": ["audit-no-match"]}}
    sid = recall.start(P, args)["recall_session_id"]
    original = recall.require_owned_session
    armed = [True]

    def interleave(*a, **kw):
        result = original(*a, **kw)
        if armed[0]:
            armed[0] = False
            recall.close(P, {"session_id": sid, "outcome": "cancelled"})
        return result

    try:
        with patch.object(recall, "require_owned_session", interleave):
            recall.accept(P, {"session_id": sid, "close": True})
    except Exception:
        status = store.get_session(sid)["status"]
        if status == "CANCELLED":
            return True, {"status": status}
        raise
    status = store.get_session(sid)["status"]
    return status == "CANCELLED", {"expected": "CANCELLED", "actual": status}


def mood_previous_value_is_preserved():
    mid = hold("synthetic mood event")
    old_note = "AUDIT_SYNTHETIC_OLD_NOTE_20261003"
    memory.mood_write("jiaming", mid, tags=["old-tag"], note=old_note)
    memory.mood_write("jiaming", mid, tags=["new-tag"], note=None)
    with db.formal() as conn:
        retained = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE resource_id=? AND payload LIKE ?",
            (mid, "%" + old_note + "%"),
        ).fetchone()[0]
    return retained > 0, {"audit_events_retaining_previous_note": retained}


def source_failed_cleanup_is_batch_scoped():
    fixture_dir = Path(ISOLATED_ROOT) / "synthetic-inputs"
    fixture_dir.mkdir()
    empty = fixture_dir / "empty.json"
    bad = fixture_dir / "bad.json"
    empty.write_text(json.dumps([{"uuid": "audit-empty", "chat_messages": []}]))
    bad.write_text(json.dumps([
        {"uuid": "audit-bad", "chat_messages": [{"uuid": "audit-message",
         "sender": "human", "created_at": "2026-10-03T00:00:00Z",
         "content": [{"type": "text", "text": "synthetic source text"}]}]},
        42,
    ]))
    first = importer.import_file("jiaming", str(empty))
    if first["status"] != "completed":
        raise RuntimeError("probe precondition: empty conversation import failed")
    try:
        second = importer.import_file("jiaming", str(bad))
    except sqlite3.IntegrityError as exc:
        return False, {"cleanup_exception": type(exc).__name__}
    with db.formal() as conn:
        leftover = conn.execute(
            "SELECT COUNT(*) FROM source_messages WHERE provider_conversation_id='audit-bad'"
        ).fetchone()[0]
    return second["status"] == "failed" and leftover == 0, {
        "batch_status": second["status"], "leftover_messages": leftover,
    }


results = []
for probe in (fresh_receipt_after_update, words_switch_applies_to_replay,
              terminal_status_is_not_overwritten, mood_previous_value_is_preserved,
              source_failed_cleanup_is_batch_scoped):
    reset_all()
    try:
        passed, evidence = probe()
        result = {"probe": probe.__name__, "status": "PASS" if passed else "FAIL", **evidence}
    except Exception as exc:
        result = {"probe": probe.__name__, "status": "ERROR", "exception": type(exc).__name__,
                  "detail": str(exc)}
    results.append(result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
if hasattr(signal, "alarm"):
    signal.alarm(0)
sys.exit(2 if any(r["status"] == "ERROR" for r in results)
         else 1 if any(r["status"] == "FAIL" for r in results) else 0)
