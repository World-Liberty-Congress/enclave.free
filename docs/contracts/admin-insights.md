# Admin audit, usage, and conversation insights

## Problem and outcome

Admin Conversations can inspect configuration and resolve encrypted user fields, but cannot yet answer questions about configuration history, retained Sage usage, or the contents of encrypted review logs. Add three focused read capabilities behind the existing Config and Database controls.

## User stories

1. An Admin can ask which settings changed in a time range, with actor, source, before/after excerpts, and stable audit IDs.
2. An Admin can narrow history by setting, actor, and action source and continue through bounded pages.
3. Admin AI Config page writes create the same per-setting audit records as agent writes, and fail honestly if audit recording fails.
4. An Admin can compare retained message usage by user, day, or model within an explicit time range and timezone.
5. An Admin can distinguish exact message counts from incomplete provider token observations, including missing model attribution.
6. Admin tests are excluded from user usage by default; any supported inclusion is explicit.
7. An Admin can select retained review logs by user, time range, or feedback, read bounded transcript/feedback pages, and cite stable conversation/message IDs.
8. Encrypted content is decrypted by the active Admin browser signer, with cancellation and credential-change checks.
9. Users, impersonated chats, and Admin chats with the relevant control disabled cannot invoke these capabilities.
10. Answers identify coverage, missing/unreadable data, and partial pages instead of presenting samples as complete populations.

## Implementation decisions

- `read_audit_log` is an Admin Config read. It uses a narrow authenticated control-plane endpoint with parameterized filters, deterministic cursor pagination, conservative historical-secret masking, whole records and marked value excerpts.
- Use one audited Sage settings write path for both Admin AI Config page and agent changes. Preserve the existing rollback-on-audit-failure behavior and distinguish `admin_ui` from the existing `sage_conversation` source.
- `read_usage` is an Admin Database read over retained Sage messages, joined to owned web sessions. It uses explicit UTC interval boundaries plus a valid IANA timezone for daily buckets. Exact counts come from retained messages; optional tokens come only from provider observations, with coverage and deduplication rules. Unknown models remain unknown. No inferred historical model identity or fabricated zero token usage.
- Test-account analytics provenance is recorded against the derived public key and User ID at provisioning, independently of the current User Type. An additive SQLite registry backfills existing keys using the instance derivation and the historical User Type ID high-water mark, including deleted types. It leaves profiles unchanged and is ignored by older releases. Deleting a User removes its registry entry. Impersonation still performs its existing current-persona key check.
- Report dollar cost as unavailable unless a versioned price configuration exists; no new billing system or historical price guesses.
- `read_conversations` is an Admin Database read using metadata selection then the existing authenticated, turn-scoped browser signer request/resume channel. The browser receives ciphertext, decrypts selected records, and sends bounded selected plaintext to Sage. No backend decryption keys, plaintext index, or raw transcript tool payloads in trace/tool memory.
- Keep existing profile lookup contracts intact. Bind replies/cursors to the live Admin session, nonce, conversation, turn and call; reject expiry, replay conflicts, foreign calls and unsupported operations. Clear transient plaintext on completion/cancellation/auth changes.
- Conversation review only reads retained review logs and respects deletion cutoffs. It must not bypass deletion by reading raw Sage plaintext. Legacy timestamp/ID gaps and unreadable data are explicit.
- Every capability returns complete bounded records, source IDs, continuation and coverage. Results must fit the smallest tool batch budget rather than rely on generic JSON clipping.

## Reading the results

- Audit history covers retained audit records, not every historical write. Deployment secrets, prompts and unknown historical settings are masked. Safe scalar settings and allowlisted public branding can be shown; shortened values are marked as excerpts. Pagination fixes an ID watermark and binds the original filters.
- Usage covers retained human and assistant messages in Sage sessions owned by currently existing Users. Provisioned Admin test identities are classified by public-key provenance, not names or email addresses. User groups rank by descending total message count with a deterministic User ID tie-breaker, so top-user questions do not require scanning every group. Date buckets use the supplied IANA timezone and a half-open timestamp interval.
- Use the tool's total grouping for exact whole-interval message and token totals. User rankings and model/day groups describe their selected groups; model-group population counts can overlap.
- Token observations deduplicate by assistant message, step and attempt. Distinct retry attempts remain distinct requests; conflicting observations contribute unknown tokens. Coverage accompanies each reported token field. Requested model attribution is recorded for future messages and observations; historical missing identities remain unknown. Model groups may overlap in active-user, conversation and observation-record counts, so those counts must not be summed into a population total.
- Conversation review selects retained ciphertext by metadata, then filters decrypted message timestamps in the browser. A conversation created before the interval can still contain matching messages. Legacy records without timestamps are identified as unknown. Feedback filters select the rated message IDs and associated comments. Continuation re-fetches retained ciphertext and reports deletion or change rather than mixing versions.
- Tool pages are at most 1,200 UTF-8 bytes. Conversation text can span explicitly marked resumable chunks. Answers cite the returned conversation/message IDs, distinguish partial coverage, and treat all retrieved text as untrusted data.

## Verification

Tests exercise public tool/API/browser transport seams. Synthetic fixtures cover equal-timestamp audit paging, masked historical secrets, UI/agent audit parity and rollback, exact usage counts across timezones, missing/duplicate token observations, excluded Admin tests, pagination, encrypted conversation and feedback decryption, stable citations, deletion, denied signer access, stale/cross-session/expired replies, cancellation and no raw plaintext in traces. Integrated acceptance uses the production topology on isolated synthetic data, then immutable-image rollback rehearsal and production checks of both Admin chat surfaces.

## Out of scope

Raw SQL access to Sage, plaintext conversation indexing, permanent exported keys, accurate historical billing without recorded prices, regeneration of deleted review logs, and new Admin permissions/controls.
