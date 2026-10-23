# Admin insights delivery ledger

## Run

- Target: World-Liberty-Congress/enclave.free and its Sage fork.
- Baselines: application 2ad21aa7fe839b9e5956085c63ada7f3007d9c04 (`liberator/main`); Sage 1d94c033ad6aba694f85849ae8fba7c3eeb26c0f (tree of `production/liberator-baseline`).
- Branch: `feature/admin-insights` in both repositories.
- Goal: implement and verify audit, usage, and signer-backed conversation insights; release, deploy, and verify systematically end to end.
- Current status: specification and implementation.
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
