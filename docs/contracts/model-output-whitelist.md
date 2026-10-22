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

MCP defaults to one complete JSON payload in content[0].text, for both ordinary and
paged results. structuredContent is absent by default. JSON remains JSON; this is
transport selection, not a conversion to Markdown. Explicit params._meta.content_envelope
supports text (default), single (structured payload plus a text pointer), and dual
(legacy two-copy compatibility). Structured-only clients must request single; legacy
clients requiring both channels can request dual and must choose one model input.
Initialization advertises this contract. Error results use the negotiated channel too.
Invalid negotiation is rejected before business execution. HTTP and stored receipts do
not change. estomago uses HTTP, unwraps once, then projects at its final model boundary.

Protocol reference: https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-06-18/server/tools.mdx
Tools declare no outputSchema requiring structuredContent. External-client end-to-end
interoperability has not been claimed: tests cover text-only, structured-only (opt-in),
and combined consumers against the actual local JSON-RPC wire.

MCP workflow checks also preserve a canonical candidate_ref when a recall card originally
supplies only resource_ref. recall.status has its own contract (session ID/state/revision,
candidate states, exceptional invalidation), rather than being treated as a search page.

JEV retains the questions/type/instructions/noul protocol. Candidate occurrence dates no longer
require exact confidence. Only evidence whose date was previously suppressed uses a semantic-state cache suffix,
so it cannot reuse a previous no-date judgment. Unchanged evidence retains its existing
cache identity. Existing cache data is not deleted.
