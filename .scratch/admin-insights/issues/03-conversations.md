## Parent

Admin insights specification (link added in release PR).

## What to build

Let an Admin select retained review logs and ask about conversation/feedback contents through `read_conversations`, using the active Admin browser signer and bounded text pages with stable conversation/message citations.

## Acceptance criteria

- [ ] Admin Database-only, live signer capability and no impersonation.
- [ ] User/time/feedback metadata selection; bounded transcript/feedback records and honest coverage, legacy gaps and unreadable records.
- [ ] Existing browser signer request/resume authentication and cancellation protections remain intact; existing profile lookup behavior unchanged.
- [ ] Backend receives no decryption key; Sage gets only selected bounded plaintext; raw payloads excluded from traces/tool memory; transient plaintext cleared.
- [ ] Retained-log deletion is respected, selected text can be continued without silently dropping records.
- [ ] Cryptographic fixture tests plus transport tests cover paging, citations, cancellation, auth changes, invalid/stale/foreign replies and signer refusal.

## Blocked by

None. Shared registration edits are integrated by the parent.
