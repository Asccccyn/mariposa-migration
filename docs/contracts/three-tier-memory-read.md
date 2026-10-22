# Three-tier memory reading

The existing `memory.detail.read` accepts optional section=memory|relations|source.
Omitting section retains the legacy full-bucket contract. The three-tier host passes
an explicit section and view=model. Each section has its own runtime snapshot key and
cursor prefix, without changing storage schema or the public detail_ref.

- memory: complete event text, occurrence date, title, categories, mood and our_words.
  Ordinary words contain speaker/text and a source_gap only when meaningful. Editing first enumerates memory.our_words.list(memory_id) to obtain word_id on demand.
  Source refs and correction versions are not repeated here or in model-facing word
  lists/cards. HTTP canonical records and database columns are unchanged.
- relations: genuine relationship type/direction and directly related content, with
  target identifiers. Provenance binding edges are not presented as relationships.
- source: calls the same source.binding.open_for_memory(memory_id) service used by
  source.memory.open and the existing bucket “view original” UI. It does not first
  load the bucket's event body. Source message/conversation IDs and actual fragment
  offsets are retained; binding-row IDs/audit fields are not model reading content.

The existing UI passes only memory_id (frontend/src/App.tsx → SourceEvidence.tsx).
The server resolves binding IDs, conversation/message endpoints and offsets internally.
Thus removing per-word source_ref does not remove the bucket's source-opening link.
For explicit word-source correction, memory.words.get(word_id) provides source_ref and
source_binding_version on demand; the existing correction CAS still requires both
observed values and rejects stale versions. No automatic latest-version substitution.

Source messages are flattened before bounded pagination so arbitrarily long text can
be reconstructed verbatim. A final short fragment cannot be overwritten by a later
batch from the same section. Missing ranges remain distinct from empty results and
readable ranges are preserved alongside missing ones. Cross-section/stale cursors fail.

No new retrieval/JEV call, summary generation, body truncation, permission expansion,
reference-lifetime change or forced chat-window rotation is introduced.
