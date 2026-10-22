# Three-tier memory read deployment — 2026-10-21

Implemented and deployed: estomago `9a81173`, Mariposa `5c8a816`.

## Model contract

- Layer 1: each surfaced event has exactly memory_ref, occurrence date and complete event text.
- Layer 2: memory_read_full with section=memory returns ordinary bucket content (date, title, mood, complete text, quoted words, categories), necessary object IDs and genuine read controls.
- Layer 3: the same memory_ref can directly select section=relations or section=source, without first reading layer 2. Relations include direction, real relation semantics and one-hop related content. Source uses the existing bucket-bound original-record service. No recall/JEV rerun or summary generation.

The existing source chain is frontend App.tsx's bucket button → SourceEvidence.tsx → source.memory.open({memory_id}) → source.binding.open_for_memory. That entry consumes memory_id only; binding rows, endpoints and offsets remain internal. Ordinary words now return word_id/speaker/text (plus meaningful source_gap). Source refs and correction versions are fetched on demand using existing memory.words.get(word_id); stale correction versions still fail. Canonical HTTP data and stored fields are retained.

Section-specific snapshots/cursors prevent mixing the three reads. Source and related-target changes invalidate their own continuation snapshots. Long original text fragments are lossless; the final short fragment cannot be overwritten by the following batch. Empty source lists and unavailable ranges remain distinct. Original source-message timestamps/attachments are preserved as business evidence, unlike binding audit timestamps.

## Validation

- 70 backend tests passed, including same-bucket source opening directly as the first call, ordinary-view field removal, operation-reference recovery, stale correction rejection, related content/direction, long Unicode originals, cross-section cursor rejection, partial source failure, and edits during pagination.
- 73 estomago unit tests across eight files passed, plus local HTTP integration acceptance. Type checks and UI build passed.
- A model-side seam test takes memory_ref from the actual serialized synthetic surfacing block and calls section=source directly; only memory.detail.read is invoked, without a preceding detail read or new search/JEV call.
- Production health is 200 for both services. The dedicated surface credential exposes all three section values and rejects fabricated references for each. Served UI bytes match the pinned build.
- Chat/history/trajectory/provider-request, formal-memory and JEV-cache fingerprints match before deployment. Provider request delta is zero. No real OR/JEV call was made.
- Real-data previews were exported through the deployed read-only builders; they are not represented as a live surfaced delivery.

## Production paths and compatibility

estomago: /Users/owner/Projects/estomago-releases/three-tier-memory-20261021
Mariposa: /Users/owner/Projects/mariposa-releases/three-tier-memory-20261021

estomago was built by applying this commit to the previous pinned production base, then rebuilding UI. Unrelated wake/keepwarm working-branch changes were not deployed. The source branch's prebuilt bundle was not copied into production.

Existing cached tool schemas and historical augmentation text are never rewritten. Old tool schemas without section retain their prior default behavior; new windows use the three-tier model schema. UI buttons use the new explicit sections. Existing provider eligibility and reference authorization/expiry rules are unchanged; this does not claim OR auto-surfacing support was added.

Backups/rollback wrappers: /Users/owner/Documents/Codex/2026-10-21/jev/work/deploy-backups/three-tier-memory-20261021
To roll back, atomically restore the corresponding *-start-rollback.sh to the service start.sh and restart/check health. Roll back code only, never overwrite later user data with old database backups.

Deployment evidence: /Users/owner/Documents/Codex/2026-10-21/jev/outputs/three-tier-production-deployment.json
Read-only real bucket previews: /Users/owner/Documents/Codex/2026-10-21/jev/outputs/three-tier-model-json/
