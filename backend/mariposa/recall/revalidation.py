"""Current resource checks shared by operation replay and frozen pages."""
from __future__ import annotations

from .. import config
from . import phase_policy


def normalize_field(field: str) -> str:
    return "our_words" if isinstance(field, str) and field.startswith("our_words.") else field


def revalidate_card(conn, card: dict, *, rejected=frozenset(),
                    intent: str | None = None, navigation: bool = False):
    """Return (current projection, reason). Never mutate the saved card.

    Reject the whole saved card when a field becomes unavailable: excerpt and
    evidence share that projection, and changing carriers would change cursors.
    """
    ref = card.get("resource_ref") or ""
    fields = card.get("matched_fields") or []
    if (ref.startswith("our_word:") or card.get("channel") == "words"
            or "our_words" in map(normalize_field, fields)) and not config.RECALL_WORDS_ENABLED:
        return None, "channel_words_disabled"
    if ref.startswith("source_msg:") and not config.RECALL_RAW_FALLBACK_ENABLED:
        return None, "channel_raw_disabled"
    if ref in rejected:
        return None, "candidate_rejected"
    if ref.startswith("source_msg:"):
        row = conn.execute("SELECT published FROM source_messages WHERE id=?",
                           (ref[len("source_msg:"):],)).fetchone()
        return ((card, None) if row is not None and row["published"]
                else (None, "resource_inactive_or_missing"))

    mid = card.get("memory_id")
    if ref.startswith("memory:"):
        mid = ref[len("memory:"):]
    elif ref.startswith("our_word:"):
        row = conn.execute(
            "SELECT w.*, m.current_version_no, m.visibility, m.compression_state"
            " FROM memory_our_words w JOIN memories m ON m.memory_id=w.memory_id"
            " WHERE w.word_id=?", (ref[len("our_word:"):],)).fetchone()
        if row is None or row["visibility"] != "active" or row["compression_state"] != "full":
            return None, "resource_inactive_or_missing"
        mid = row["memory_id"]
        generation = int(row["source_binding_version"] or 0)
        if "source_binding_version" not in card:
            # Old packets never pinned a source identity. Only a never-bound
            # word can still be verified without guessing its prior binding.
            if row["source_ref"] or generation:
                return None, "source_identity_unverifiable"
        elif (card["source_binding_version"] != generation
              or (card.get("source_ref") or None) != (row["source_ref"] or None)):
            return None, "source_binding_changed"
        from ..retrieval.words import _word_evidence
        current = _word_evidence(row, conn)
        saved = (card.get("evidence") or [{}])[0] or {}
        if current[0].get("evidence_kind") != saved.get("evidence_kind"):
            return None, "source_evidence_changed"
    elif not mid:
        return None, "resource_unverifiable"

    row = conn.execute(
        "SELECT visibility, current_version_no FROM memories WHERE memory_id=?",
        (mid,)).fetchone()
    if row is None or row["visibility"] != "active":
        return None, "resource_inactive_or_missing"
    if str(card.get("content_version")) != str(row["current_version_no"]):
        return None, "content_version_changed"
    cats = sorted(r["category"] for r in conn.execute(
        "SELECT category FROM memory_categories WHERE memory_id=?", (mid,)))
    for ev in card.get("evidence") or []:
        if isinstance(ev, dict) and ev.get("field") == "categories":
            saved = (ev.get("structured_value") or {}).get("categories")
            if isinstance(saved, list) and sorted(saved) != cats:
                return None, "categories_changed"
    # Words is a dedicated cross-phase channel; phase filters govern event hits.
    if intent == "find_words" or ref.startswith("our_word:") or navigation:
        return card, None
    facts = phase_policy.facts_for_many(conn, [mid])[mid]
    try:
        allowed = phase_policy.eligible_fields(phase_policy.phase_from_facts(facts))
    except phase_policy.DataGap:
        allowed = phase_policy.CORE_FIELDS
    if any(normalize_field(field) not in allowed for field in fields):
        return None, "phase_fields_changed"
    return card, None
