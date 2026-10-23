# Admin insights delivery ledger

## Run

- Target: World-Liberty-Congress/enclave.free and its Sage fork.
- Baselines: application 2ad21aa7fe839b9e5956085c63ada7f3007d9c04 (`liberator/main`); Sage 1d94c033ad6aba694f85849ae8fba7c3eeb26c0f (tree of `production/liberator-baseline`).
- Branch: `feature/admin-insights` in both repositories.
- Goal: implement and verify audit, usage, and signer-backed conversation insights; release, deploy, and verify systematically end to end.
- Current status: final verification and release preparation.
- Skill setup exists. Upstream tracker/default staging instructions are adapted to the user's explicitly selected Liberator fork and existing release branches.
- Prior design approval and latest explicit deployment instruction cover scope, test seams, the three capability slices, and production release. No new approval gate is inferred from the feature skill's default stop-at-PR workflow.
- Terminology follows CONTEXT.md and the existing Admin user-lookup contract. No new hard-to-reverse architecture is needed.

## Artifacts and work

- Specification: `docs/contracts/admin-insights.md` (issue links pending).
- Audit: filtered history + shared audited UI/agent settings writes.
- Usage: retained-message analytics + token coverage.
- Conversation review: metadata selection + browser-signer bounded text pages.
- All three are AFK implementation slices. They share tool registration and chat transport integration; parent coordinates these shared edits and final commits.
- Each ticket uses a full Codex delegated worker session. Workers own distinct modules and coordinate shared integration in patch notes; parent performs common-file integration and records per-ticket commits. This avoids concurrent writes to runtime registration and entrypoints in the shared checkout.
- Review requires standards/spec and auth/data-contract review, local CodeRabbit, and PR review or documented independent fallback.
- Production baseline: user-lookup-68b26e7. Preserve existing env, Caddy, other services and data. Use immutable images, pre-deploy backups with restore proof, and rollback rehearsal before rollout.

## Verification commands

- Frontend: npm test; npx tsc --noEmit; npm run build.
- Sage: cargo fmt --all -- --check; cargo check -p sage-core --bin enclave_web; cargo test -p sage-core --lib (Homebrew libpq linker path).
- Python: focused unittest suites and the production runtime test topology; distinguish existing FastAPI route-introspection baseline issue.
- Acceptance: isolated Compose topology with synthetic fixtures, browser signer round trips, both Admin surfaces; immutable-image deploy and rollback rehearsal; production gateway/service and feature checks.

## Open questions and parked work

None. Billing estimates are explicitly unavailable without versioned prices. Historical completeness is reported, never invented.

## Tracker fallback

GitHub returned `the 'World-Liberty-Congress/enclave.free' repository has disabled issues`. The agreed spec remains in the contract above; ready-for-agent tickets are stored locally in `.scratch/admin-insights/issues/01-audit.md`, `02-usage.md`, and `03-conversations.md`. No upstream issues are created for fork-specific work.

## Implementation and release preparation evidence

- Full worker task IDs and ownership are recorded in `.cache/admin-insights/tasks.json`.
- Parent wired provisional common entrypoints and added future requested-model attribution to assistant metadata and per-attempt observations. Two focused Sage observation tests pass.
- Parent added the narrow usage owner classifier API (live Admin + internal token, 200 IDs, public-key test provenance, no PII). Five endpoint tests pass after expected 404 failures.
- Production preflight: ten healthy services on user-lookup-68b26e7; successful enabled daily backup; 120GiB server free. No production mutations have occurred.
- Local previous lookup Compose stack restarted against the latest immutable production images. Existing synthetic roster retained. New fixture:150 user/assistant messages across five users,200 Admin messages excluded,39 duplicated provider-observation records expected to total585 tokens after deduplication,5 encrypted logs with feedback.
- Collaborative preview open succeeds but snapshot times out twice (`Preview automation snapshot timed out after 15000ms.`). Local browser acceptance uses established Playwright harness; production uses the already-authorized Zen session through cua-driver. Local signer login passes with no browser errors.
- Release scripts adapted under `.cache/admin-insights/release`; production fixture/cleanup rehearsed locally. Native Sage/cache path is `/Users/plebdev/Desktop/Projects/enclave-free/sage/target`.

## Integration acceptance

- Config HTTP write and restore produced `admin_ui` per-setting audit entries. Native audit tests cover audit failure, conditional rollback, concurrent edits, override inheritance and revoked Admins.
- Both Admin chat surfaces returned the five expected names and per-user counts with585 deduplicated observed tokens. One later full-chat response summed the correct rows to155 instead of150; a total grouping and stronger browser acceptance assertions address this before release.14 focused usage tests also verify ranking ties, timezone boundaries, model gaps, test-user provenance, token conflicts and bounded database timeouts.
- Real encrypted review fixtures exposed a transcript-envelope mismatch (`{turns:[...]}`); the browser reader and encryption fixtures were corrected before release. Both surfaces then quoted the exact assistant message and feedback, cited the stable IDs, and reported zero unreadable/missing/unknown-time records.
- Audit pages can contain fewer records than requested because of the byte budget. Tool and turn guidance explicitly require continuation to the requested count and distinguish action summaries from per-setting changes. Actor public keys remain complete.
- Token field completeness is distinct from assistant-message coverage. Guidance reports `assistant_with_usage / assistant_messages`; complete fields within recorded observations do not imply complete provider usage.
- Full backend discovery in the production runtime:494 tests,492 passed,1 skipped,1 unchanged FastAPI `_IncludedRouter.path` test error. The same error was independently reproduced against the deployed baseline image/source. The lightweight local venv additionally lacks transformer/docling dependencies; those dependency tests passed in the production image.
- First consolidated Sage workspace/all-features run:329 tests passed, including real isolated PostgreSQL audit and usage tests. Clippy with warnings denied passed. Final source formatting and changed-test reconciliation are pending.
- Consolidated frontend:82 files and520 tests passed; actual encrypted-envelope regression tests included. Component console errors were intentional failure-path fixtures. Sage source was formatted and committed for final independent review.

## Final review corrections

- Conversation precision/deletion/unreadable-record corrections committed in application330d42f and Sage dcffb38; focused54frontend,7Python,5native tests passed.
- Audit filtering, ordering and cursor positions now use exact integer UTC microseconds; inclusion/exclusion within a single millisecond is covered.
- Migrated Admin test accounts now retain analytics provenance through an additive SQLite registry. Provisioning proves the derived public key; upgrade backfill includes deleted historical User Types without changing user records. Existing impersonation authorization remains unchanged. Four new regression cases cover migration, upgrade, replacement/deletion and invalid registration.
- Application29f1cd9 contains the audit and provenance fixes. Full production-runtime backend discovery after these changes:501 tests,499 passed,1 skipped,1 unchanged baseline FastAPI route-inspection error. Provenance/impersonation/conversation focused suite:22 passed.
- Local CodeRabbit initial application and Sage reviews raised zero issues. Application fix review raised one documentation wording issue about token coverage; clarified exact messages versus incomplete observed tokens. Independent standards/spec follow-up reviews are underway.

- Final spec follow-up: no findings; test migration exclusion closed. Standards follow-up found a conversation timestamp grammar mismatch that could abort a browser stream. Sage now rejects lowercase separators, excessive fractional precision and leap seconds before dispatch, matching browser validation; both sides share regression fixtures.
- Usage total grouping returns one exact aggregate (including empty scopes), with total_messages on every row. Sixteen focused Postgres tests and fresh standards/spec reviews passed. The synthetic aggregate is150 messages (75human/75assistant),585 observed tokens,39 of75 assistant records covered.
- Final Sage workspace/all-features run:333 tests passed (265library,68tool tests), including real PostgreSQL cases; strict Clippy passed. Frontend final shared conversation/profile/stream regressions:63 passed; typecheck passed. Strengthened browser usage acceptance passed on both Admin chat surfaces with150 messages and explicit partial token coverage.
