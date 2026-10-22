# Event source binding deployment — 2026-10-21

Implemented and deployed separate event source selection, independent of quotes.
Mariposa reuses source bindings and full member manifests, with migration 40
marking explicitly selected event spans. Existing quote bindings are not promoted.
Live hold submits selected sequence ranges via stable archive receipts. Historical
Agents can submit endpoints and the observed source revision. Reads validate every
member and paginate without truncating original text. Source edits invalidate old
continuations; binding corrections preserve audit data.

Validation: 201 distinct Mariposa tests passed across bounded batches; one optional
external-export test skipped because its file path was not configured. 59 estomago
tests passed, including tool-prefix immutability, original read pagination, archive
sync, quote handling, edit/unseen/unsynced range rejection. Host and frontend type
checks passed; Mariposa frontend build passed. The exact production-base estomago
patch passed its range tests and type check. No real OR/JEV calls were made.

User-approved bucket 00453 backfill: ranges 345–350 and 357–459, 6 + 103 messages.
Both use current valid source revisions and verified parent paths. The old three
quote-derived bindings remain as provenance; two explicit event spans are used
for original reading. The deployed reader returned all 109 messages in 6 default
pages, with exact text/order equality to the reviewed source snapshot. A forced
small-page rehearsal reconstructed the same originals across 53 pages. Memory
body, quotes and other business content were not rewritten.

Both services returned HTTP 200 after restart. Protected chat tables, frozen
windows/trajectories/provider requests, runtime settings, JEV cache tables and
memory business tables had unchanged hashes before/after deployment and backfill.

Mariposa release: /Users/owner/Projects/mariposa-releases/event-source-20261021
Estomago release: /Users/owner/Projects/estomago-releases/event-source-20261021
Estomago release is the previous production base plus only the two changed host
source files; unrelated wake/keepwarm branch changes were not deployed.

Compatibility: existing frozen tool catalogs are untouched. New catalogs/windows
advertise source_ranges. An old catalog can save an unbound memory but cannot
claim that quotes establish event scope. Other old buckets remain unbound until
an Agent selects their source ranges; no bulk quote-derived backfill occurred.

Raw original text and private manifests are retained only in restricted local
verification files, not this repository. Actual bucket readback used the deployed
service function directly; it was not a model-provider request.

Code commits:
- mariposa: 61483abd1c80c97ee9ea265340c126388f644572
- estomago: 60c69980fae4ee0ee15628159da8391483767191
