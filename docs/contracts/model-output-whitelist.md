# Model result contracts (2026-10-21)

Projection is after business execution, authorization, version checks and idempotency.
HTTP and stored receipts remain canonical. MCP model output uses capabilities/model_output.py;
explicit detail reads use memory/model_projection.py. No database columns are removed.

Retained metadata and exact purpose:

| Key family | Purpose |
|---|---|
| memory_id / word_id / recollection_id / mark_id / relation_id / binding_id / item_id / plan_id / episode_id | Address the corresponding get/update/revise/revoke/correct operation |
| version / revision / current_revision | Supply expected_version / expected_revision against the observed version, never substitute the latest version during projection |
| source_binding_version | Supply expected_source_version for word-source correction |
| view_receipt / receipt_id | Explicit view confirmation or recollection append |
| next_cursor, snapshot_id, section, offsets | Continue the same frozen page or reconstruct actual fragments |
| result_set_id, recall_session_id, continuation reference | Continue/refine/accept/reject the current recall session |
| episode source_message_id/content_hash/char_offset and selection conversation/member/offset keys | Required evidence parameters for episode.apply; hash pins the observed source, not diagnostic metadata |
| changed / uncertain / unverified / content_gap / incomplete / invalidated / historical / current_state | Prevent stale, inferred, absent, partial or superseded content being mistaken for current complete fact |

Business content is copied verbatim: occurrence date, title, body, categories, mood,
speaker/quotes, recollections, real relationship type/direction/target and plan/I content.
No new summaries or body truncation. Unknown occurrence dates are identified explicitly;
creation/storage timestamps are never substituted. Punctuation-only quotes remain content.

Ordinary result paths projected: memory open/get/hold/update/list/date/category/emotion/cards;
recall results and pages; words/recollections/keeps; deletion review; relation link/list/correction;
episodes; plans; I; bootstrap; source search/message/range/selection/conversation/memory reads.

Explicit history queries retain history content. Explicit include_content source reads retain
original provider evidence, rather than guessing which nested provider fields are business data.
Maintenance/service-only receipts and surfacing prepare/validate/check retain their internal
contracts. They are not injected as recollection prose. Unsupported capability families retain
separate contracts; this is not a claim that every registered administrative endpoint was rewritten.

MCP text/structured dual-channel compatibility is unchanged. Projection makes the business
payload smaller; consuming both channels remains the responsibility of an external MCP client.
estomago uses HTTP, unwraps once, then projects at its final model boundary.

JEV retains the questions/type/instructions/noul protocol. Candidate occurrence dates no longer
require exact confidence. Only evidence whose date was previously suppressed uses a semantic-state cache suffix,
so it cannot reuse a previous no-date judgment. Unchanged evidence retains its existing
cache identity. Existing cache data is not deleted.
