# Operational Monitoring and Recovery

This runbook covers operator-owned monitoring and recovery verification for production-like Enclave deployments. It complements `POST /admin/deployment/config/validate`, `GET /admin/deployment/operational-readiness`, `docs/admin-key-recovery-runbook.md`, and `docs/security-data-protection-checklist.md`.

## Runtime Alerting

### Liberator availability checks

Install `scripts/operations/monitor.py` in `/usr/local/lib/enclave-operations/`,
and install `enclave-monitor.service` and `enclave-monitor.timer` in
`/etc/systemd/system/`. Install the matching `backup_failure_notification.py`
update before enabling the timer: the notifier allowlist must include
`enclave-monitor.service`. Install the updated backup notification drop-in too;
it includes both monitor units in the encrypted recovery configuration. Reuse the existing private backup repository and
notification environment files. Then run `systemctl daemon-reload` and
`systemctl enable --now enclave-monitor.timer` after deployment acceptance.

The monitor runs about every five minutes and checks:

- the newest complete `enclave:liberator` off-host snapshot is at most 30 hours old;
- the public API health endpoint returns a healthy response and the Sage runtime container is running and healthy;
- a fixed public search query returns at least one web result;
- an API maintenance marker has not remained for more than 15 minutes.

Expected API/search failures are skipped during maintenance; backup freshness
is still checked. Three consecutive failures of the same check fail the
monitor unit and invoke the existing content-free SMTP notifier. A successful
monitor notification suppresses further monitor emails for six hours; a
failed SMTP attempt is retried at the next failing check. Backup-job failure
notifications retain their existing behavior. Counters are private under
`/var/lib/enclave-monitor/`; long gaps reset a consecutive-failure sequence.

Use `monitor.py --check-only` with the backup environment loaded for a read-only
probe without changing counters or invoking a notification. Inspect
`systemctl status enclave-monitor.timer enclave-monitor.service` and
`journalctl -u enclave-monitor.service` for fixed check names and counts.
Never put repository credentials, user queries, prompts, or raw responses in
notification evidence. These checks run on the host; an external uptime
monitor is needed to detect loss of the entire host or its outbound network.

### Search and runtime maintenance

The reviewed SearXNG configuration uses an explicit provider allowlist:
Bing, Seznam, and Mwmbl. SearXNG queries them together, so available providers
can return results when another fails. Probe representative public queries
from the production host, including with Bing excluded, before promoting a
provider change. A provider returning zero matches for one query does not
prove that it is broken. Sage exposes a generic warning for partial provider
failures and treats no results with provider failures as a failed search.

Sage's web entry point handles SIGTERM/SIGINT, stops accepting requests and
allows active requests up to 90 seconds to finish. Compose gives it 120 seconds
before forced termination; scheduled backups use the same 120-second allowance
for application writers before stopping storage. An exceeded drain deadline returns a failed exit
instead of claiming a clean shutdown. Test the real process signal and request
drain behavior in isolation; do not stop production solely to exercise this.

Streamed token usage uses the latest reported fields, preserving previously
reported values when a later frame omits them. Missing counts remain unknown.
Historical records captured before the correction are not reconstructed or
estimated; they may still undercount output tokens.

Configure alerts outside the product runtime for:

- repeated auth failures from magic-link requests, session validation, and Admin authentication;
- unusual Admin actions such as off-hours configuration changes, lifecycle review changes, export activity, or database-inspection activity;
- destructive endpoint usage such as deletion, purge, migration, irreversible Audit Log detail compaction, and lifecycle execution endpoints.

Use Audit Log records, gateway access logs, and application logs as evidence sources. Alert rules should identify the actor where available, the route or action category, the affected object, timestamp, source IP or proxy identity where available, and the follow-up ticket or incident record.

## Backup and Restore Verification

Run a restore drill quarterly and before production upgrades or storage migrations. The drill must cover:

- SQLite: restore the database into an isolated environment, run migrations, and verify Admin login plus representative User Conversation and lifecycle status reads.
- deployment config: restore non-secret settings and secret references without copying raw secret values into evidence.
- uploads: restore the uploads directory and verify document listing, download, ingest metadata, and lifecycle deletion behavior.
- Retrieval Index: restore Qdrant from a snapshot or rebuild it from product storage, then verify retrieval hydrates chunk text from SQLite rather than vector payload text.

Record the restore drill date, operator, environment, backup source, targets covered, result, failures, and follow-up actions.

## Incident Response

For repeated auth failures, unusual Admin actions, or destructive endpoint usage:

1. Preserve Audit Log, gateway, and application log evidence.
2. Identify affected accounts, sessions, configuration keys, documents, exports, and lifecycle records.
3. Rotate affected secrets or sessions when compromise is plausible.
4. Review Deployment Surface retention for provider traces, runtime logs, backups, snapshots, copied exports, and browser-held copies.
5. Record the incident timeline, decision log, and recovery validation.

## Admin Key Recovery

Use `docs/admin-key-recovery-runbook.md` for admin key recovery. Include the recovery scenario, whether a tested backup exists, whether encrypted PII remains recoverable, and any irreversible access loss in the incident record.

## Drill Evidence

For each alert drill, restore drill, incident response drill, or admin key recovery drill, record drill evidence:

- date and operator;
- scenario;
- systems and data classes covered;
- expected signal or recovery result;
- actual result;
- follow-up action and owner;
- checklist evidence update.

Update `docs/security-data-protection-checklist.md` when a drill completes.
