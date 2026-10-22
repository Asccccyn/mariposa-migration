# Model JSON encoding and quote IDs

Ordinary bucket/card reading contains quote speaker/text, not word_id. The native word
ID remains stored and is returned by the existing explicit memory.our_words.list endpoint
when enumerating records for an edit; memory.words.get then supplies correction state.
No new alias mapping or automatic latest-version substitution is introduced.

Model tool content is serialized once as JSON. The HTTP request/JSON-RPC response then
serializes that content string in its transport envelope. Backslashes visible in the
outer wire are not proof that the model's content was double-encoded. Decode transport
once, then parse the tool JSON once: the result must be an object and its business text
must match the original character-for-character. Never globally unescape \\n, \\r or
\\u escapes in stored text; Windows paths and quoted code can contain those literally.

Regression cases cover quotes, genuine newlines, literal escape sequences, Windows
paths, tabs/CRLF, C0 controls, emoji/ZWJ, supplementary Unicode and a literal replacement
character quoted from an error message. Invalid unpaired surrogates cannot silently
produce valid UTF-8. No production text is sanitized or rewritten by this change.

Source completeness is a separate concern. estomago's current memoryHold builds bucket
source selections from the chosen quotes. Such bindings can prove quote provenance but
do not prove full-event context. Returning the exact bound fragments is not equivalent
to returning the entire conversation/event. Do not repair this by silently concatenating
or rebinding all chronological neighbors: existing range validation requires a verified
parent path and explicit event boundaries. Binding data is unchanged in this batch.
