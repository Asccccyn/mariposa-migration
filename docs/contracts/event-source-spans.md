# Event source spans

Event evidence and quoted words are independent. A quote's source reference is
provenance for that quote; it does not establish the event's boundaries.

## Storage and validation

Migration 40 adds `memory_source_spans`, keyed by an existing source binding.
The source type/conversation/endpoints remain in existing source records and
`memory_source_bindings`; the complete ordered revision manifest remains in
`memory_source_binding_members`. No raw messages are copied. Old bindings are
not automatically promoted. No event span means `EVENT_SOURCE_UNBOUND` in detail,
or `event_source_status: unbound` in direct source reads.

Each event span must cover the entire validated parent path, with full message
boundaries and current published revisions. Partial quote offsets, missing
members, siblings, stale hashes and superseded revisions are rejected. Multiple
spans may reference different conversations; the caller chooses each span.

## Live hold

The model supplies `source_ranges: [{start_seq, end_seq}]` using conversation_read
or existing message markers. The host fills the verified current conversation,
requires every selected message to have been read at its current revision, and
maps each message through its durable source-sync receipt. It sends
`event_source_ranges: [{conversation_id, members}]` inside the existing atomic
memory.hold operation. The backend validates and saves the bucket and spans in
one transaction. Quotes never produce event spans. Omitting ranges preserves the
memory as unbound, and the host reports that state. No last-hold heuristic or
additional model is used. Cross-day and cross-window ranges have no time filter.

## Historical creation and backfill

An importing Agent reads the original conversation and selects the event scope.
It may call existing `source.range.open` with `for_binding: true`, receiving one
`source_revision`. Creation then uses `memory.hold` with `operation_id` and:

```json
{"event_source_ranges":[{"conversation_id":"c","start_message_id":"s","end_message_id":"e","source_revision":"the observed revision"}]}
```

Backfill uses existing `source.binding.bind`, the same endpoints and revision,
plus `memory_id` and `event_scope: true`. Internal import adapters may supply the
complete member manifest instead. The version is checked in the write
transaction; the latest revision is never silently substituted. Existing event
binding corrections require a new observed selection, preserve correction audit
history, and increment the binding version. Removing evidence leaves the memory.

## Reading and cache boundaries

Source reads use only explicit event spans. Every member is checked for revision
drift, not just endpoints; changed evidence is unavailable until reselected.
`source.memory.open` now returns bounded pages with `next_cursor`; long messages
use `partial_item`, `item_index` and code-point `text_range`. Adjacent fragments
with the same message ID reconstruct the exact message. Any source-snapshot
change invalidates a continuation. The bucket UI supports continuation and
assembles message fragments. Existing `memory.detail.read(section=source)` uses
the same spans and existing pagination.

Layer 1 still returns memory_ref/date/full text. Layer 2 still returns ordinary
business fields without original conversations. Legacy full-detail calls also
use event evidence semantics. No retrieval/JEV call is required to read sources.

Existing frozen host tool catalogs and model prefixes are not rewritten. The new
hold input is advertised in newly created tool catalogs/windows; an older frozen
hold contract can still save an unbound memory, but cannot claim event coverage.
