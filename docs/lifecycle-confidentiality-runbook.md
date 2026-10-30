# Lifecycle Confidentiality Runbook

This runbook supports Issue #57 and Issues #58-#67. It is the operator-facing verification path for Active Storage Lifecycle and Document/Retrieval confidentiality without repeating implementation details that should live in code or ADRs.

## What This Covers

- Lifecycle inventory and posture reporting for active product storage.
- Artifact Encryption Posture and Content Encryption Key checks.
- Encrypted uploaded Document artifacts.
- Encrypted Retrieval chunk text in SQLite.
- Minimized Qdrant payloads.
- Retrieval hydration from encrypted chunk storage.
- Document deletion, retention execution, tombstone retry evidence, and confidentiality migration.

The unsupported Deployment Surfaces remain outside these guarantees: runtime logs, WAL files, database backups, snapshots, provider traces, Docker logs, browser caches, and copied exports. This is not a Secure Erase program.

## Active Storage Lifecycle Boundaries

Scheduled Retention Execution in this milestone targets only the supported classes exposed in Data Lifecycle Status: stale Sage Session Memory, eligible User Memory, uploaded Document artifacts that are failed, superseded, abandoned, or orphaned, and compactable non-lifecycle Audit Log detail. It does not replace subject-request deletion workflows.

New Instances start with conservative scheduled retention defaults for expirable active-storage classes. Lifecycle Readiness is separate from those defaults: an Admin must review the current posture, and later lifecycle-relevant changes such as retention-policy updates or unsupported Deployment Surface category acknowledgements make the review stale. Stale Lifecycle Readiness is an Admin warning in v1 and does not block ordinary User Conversations.

The following active-storage records are not scheduled for deletion in this milestone: active User Profiles, current Document Library records, current Retrieval Index entries, Inference Verification Records, and Retention Run Records. Inference Verification Records remain indefinitely retained until a separate evidence-retention policy exists, because they are governance evidence for model-provider posture rather than ordinary content. Retention Run Records are also retained indefinitely in v1 as metadata-only lifecycle evidence; they do not share ordinary Conversation retention policy, and future deletion or compaction requires a separate evidence-retention policy.

Unsupported Deployment Surfaces are acknowledged by stable risk category: runtime logs, database internals, backups/snapshots, browser-held copies, copied exports, and provider traces. These acknowledgements record operator review only; they do not promote those surfaces into Lifecycle Data Classes or make product deletion controls apply to them.

Copied Exports and browser-held copies leave active product storage. Exported files should carry copied-export warnings, and browser local/session storage should be minimized and cleared on logout where it contains product auth/profile markers. Operators remain responsible for device, browser, and export handling policy outside the product.

Historical log/session retention is distinct from active Session Memory deletion. Active Session Memory deletion coordinates supported Sage-owned active Conversation state; runtime logs, provider traces, backups, snapshots, WAL files, browser-held copies, and copied exports require operator-owned retention policy. See `docs/deployment-surface-retention.md`.

Audit Log detail compaction is irreversible in active product storage: old sensitive old/new values are replaced while rows, actor/action metadata, timestamps, status, lifecycle evidence, and hash-chain verifiability remain reviewable. There is no v1 setting to retain full sensitive Audit Log detail indefinitely.

See `docs/adr/0006-retention-and-deletion-are-operator-controlled-but-incomplete.md` for the overall lifecycle boundary, `docs/adr/0007-audit-log-is-a-product-boundary-but-coverage-is-partial.md` for why governance evidence is preserved while old sensitive detail may be compacted, and `CONTEXT.md` for domain terms such as Active Storage Lifecycle, Deployment Surface, Copied Export, Lifecycle Readiness, and Retention Run Record.

## User deletion and repair

Deleting a User commits removal of their local profile, approval/access state,
and User Memory together with a Sage cleanup intent. `success: true` means the
account is removed; `deletion.status` describes completion across storage.
Sage removes every Conversation owned by that User, associated active memory,
and their external identity in one PostgreSQL transaction. Zero matches is
idempotent success. A failed or ambiguous remote result remains incomplete.

If a turn is active, Sage returns retryable `busy`. Let that turn finish, then
use **Admin Deployment → Data Lifecycle → deletion tombstones → Retry**.
Repeating the User DELETE as an Admin also retries the same incomplete intent.
The scheduled retention pass continues retrying Conversation-class intents;
User-class repair is manual. No new scheduler or worker is required.

This coordination assumes one Sage process. Turns, including streamed bodies
and Admin continuation of User-owned Conversations, hold admission through their
last write. User deletion marks the owner before attempting exclusive access;
the mark remains until process exit, including after failures. New requests are
rejected by Python once the profile is gone. A restart discards both the mark
and all previously authenticated in-flight requests. Deployments with multiple
Sage processes need shared coordination before relying on this guarantee.

The existing tombstone target column stores `user:<id>` for class
`sage_user_runtime`; actual Conversation targets remain UUIDs. No schema change
is needed. Deploy Sage before the backend. An older backend preserves these
intents but cannot repair them; return to the compatible backend to retry.
Audit remains best effort; the transactional intent is the durable repair record.
Completion is monotonic, so a late failed retry cannot reopen completed cleanup.

The guarantee covers state discoverable through current Sage ownership rows.
Historical orphan reconciliation is a separate operator task requiring deletion
evidence. Absence from SQLite alone is insufficient after mismatched restores.
Retained encrypted Session Logs are detached from the User and retain their own
policy. Admin-owned copies, backups, exports, provider copies, and other
Deployment Surfaces retain their separate policies.

## Scheduled Retention Automation

Use external cron, deployment automation, or another trusted external scheduler as the v1 scheduler path. The product exposes a narrow machine endpoint so automation does not reuse a human Admin browser session:

```bash
curl -X POST "$BACKEND_URL/admin/lifecycle/retention/scheduled/automation/run" \
  -H "Content-Type: application/json" \
  -H "X-Retention-Automation-Token: $RETENTION_AUTOMATION_TOKEN" \
  -d '{"retry_limit":3}'
```

The backend reads `RETENTION_AUTOMATION_TOKEN` from environment configuration. Generate a high-entropy token, store it in the deployment secret manager, give only the scheduler access, rotate it by replacing the environment value and redeploying, and revoke it by removing or replacing the value. Audit Log evidence should show `machine:scheduled-retention` rather than a human Admin key.

Every manual or machine-triggered Retention Execution should leave metadata-only Retention Run Records and tamper-evident Audit Log evidence. Retention Run Records store actor, trigger, policy snapshot, timestamps, aggregate status, counts, sanitized per-class results, tombstone references, and Audit Log linkage; they must not preserve Conversation Content, raw User Memory, uploaded Document content, or raw provider attestation material.

The policy snapshot must remain self-explanatory after settings change. It records enabled classes, retention windows, scheduled flags, trigger, retry limit, evaluated timestamp, and policy hash where available. It must not include sensitive target payloads.

Retention Execution evaluates enabled Lifecycle Data Classes independently. A class-level failure should report `partial_failure` when other classes still produce reviewable results. Run-level failure is reserved for authentication, policy loading, Retention Run Record creation, or Audit Log evidence failures. Scheduled retention failures must leave repairable evidence without hidden automatic retry loops; operators should repair the underlying cause and trigger the next run explicitly through deployment automation or Admin review.

Data Lifecycle Status reports Retention Scheduler Observation from Retention Run Records and the current Scheduled Retention Policy. Expected observation states are disabled, never observed, healthy, stale, or failing. Treat `never_observed`, `stale`, and `failing` as operator follow-up signals: confirm the external scheduler is installed, confirm it sends `X-Retention-Automation-Token`, inspect `/admin/lifecycle/retention-runs`, and verify the linked Audit Log evidence.

See `docs/adr/0015-external-retention-scheduler-with-product-owned-run-records.md` for the decision to keep scheduling deployment-owned while making run evidence product-owned.

Manual Admin controls remain available through `/admin/lifecycle/retention/preview`, `/admin/lifecycle/retention/run`, and `/admin/lifecycle/retention/scheduled/run`.

## Regression Commands

Run the focused backend regression suite:

```bash
python3 -m unittest \
  backend.tests.test_ingest_batch_replacement \
  backend.tests.test_store_minimized_payload \
  backend.tests.test_query_retrieval_hydration \
  backend.tests.test_lifecycle_status \
  backend.tests.test_lifecycle_copy_guardrails
```

Run the frontend lifecycle/admin config check:

```bash
cd frontend
npm test -- --run src/pages/AdminDeploymentConfig.test.tsx
```

Smoke the live backend when the stack is running:

```bash
docker compose -f docker-compose.infra.yml -f docker-compose.app.yml ps --format 'table {{.Name}}\t{{.Ports}}'
lsof -nP -iTCP:18000 -sTCP:LISTEN
curl http://localhost:18000/test
curl http://localhost:18000/llm/test
docker exec enclave-api-gateway wget -qO- http://127.0.0.1:18000/test
docker exec enclave-api-gateway wget -qO- http://127.0.0.1:18000/llm/test
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:18000/admin/lifecycle/status
```

The host curls must reach the Compose `enclave-api-gateway` container. If `lsof` shows another local process bound to `127.0.0.1:18000`, stop it before trusting `localhost:18000` smoke responses.

## Confidentiality Checks

For new Document ingestion, verify uploaded artifacts are not plaintext and start with the encrypted artifact marker. In Qdrant, inspect representative Retrieval payloads and confirm they contain retrieval metadata such as `chunk_id`, `job_id`, `source_file`, and `content_ref`, but not raw `text` or `fact_text`.

Use the Retrieval query path to verify hydration still works after payload minimization. Query sources should remain useful through metadata, while model context is assembled from encrypted product-owned chunk storage.

## Migration Checks

Preview before executing:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$BACKEND_URL/admin/lifecycle/confidentiality-migration/preview"
```

Execute when the preview is acceptable:

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$BACKEND_URL/admin/lifecycle/confidentiality-migration/execute"
```

Confirm per-document results show succeeded, skipped, or failed targets honestly. Migration may encrypt eligible uploaded document artifacts. It does not inspect, repair, or rewrite legacy Qdrant Retrieval payload text, and it does not claim Secure Erase.

Legacy Retrieval payload repair support has been removed. The current Retrieval posture is minimized Qdrant payloads plus encrypted chunk hydration from product-owned storage. `support_removal_ready: true` now means the preview no longer depends on Qdrant repair actions.

## Cleanup Split

Safe documentation and terminology cleanup can remove stale wording that implies the Confidentiality Migration is only planned, provided the docs continue to point operators at the preview and execute endpoints. Data-affecting cleanup remains a separate migration slice: removing legacy plaintext user/profile storage assumptions requires its own operator-reviewed plan and validation evidence. Do not bundle that migration with wording-only lifecycle cleanup.

## Manual UI Verification

Open the Admin Deployment Config lifecycle view and confirm:

- Lifecycle scope says active product storage.
- Unsupported Deployment Surfaces are visible and can be acknowledged.
- Unsupported Deployment Surface categories include runtime logs, database internals, backups/snapshots, browser-held copies, copied exports, and provider traces with practical operator guidance.
- Lifecycle Readiness shows reviewed, needs-review, or stale posture and does not claim to block User Conversations.
- Secure Erase is marked unsupported.
- Content Encryption Key and Artifact Encryption Posture are shown.
- Uploaded Document Artifacts and Retrieval Index report encrypted, mixed, plaintext by operator choice, or not configured honestly.
- Retention preview/run controls and scheduled retention controls still work for human Admins.

Keep this runbook linked from security docs instead of copying detailed implementation notes into multiple places.
