# Liberator production preparation

This branch prepares the organization-owned deployment. The GitHub destinations are [Enclave](https://github.com/World-Liberty-Congress/enclave.free) and [Sage](https://github.com/World-Liberty-Congress/sage). Liberator's DigitalOcean billing is ready: the demo-equivalent $48/month server and $5/month private backup bucket are created in NYC3. Docker, Compose, SSH and firewall setup are verified. Organization Tinfoil, Maple and Mailgun credentials are staged privately. Website DNS, HTTPS and a real Mailgun delivery test now pass for `liberator.global`. Production is not open.

## Release baseline

Enclave starts from `43703a9db18f2bbc07aefa94bb24b257d4c896ab`, the accepted Liberator integration. Its backend matches the running demo backend at `285d2e1ab474cf954737fea8d5db58de95c2af5a`, and its frontend matches the accepted branding at `b8f1625da5c1198af2dda52cf19aec083575e363`. The later integration commits change documentation only. Sage is pinned to `661b118343dc3d7567f0fb05431ade9175adc93f` on the organization's `production/liberator-baseline` branch; all 68 remote build-source files match this revision. All three Linux AMD64 application images have built locally; their source labels and local image IDs are recorded in the verification evidence. The accepted delivery uses versioned Docker archives in organization-owned encrypted Spaces; GitHub package publication is optional. The staged release and recovery checks are recorded below.

The accepted production Tinfoil model is **DeepSeek V4.1 Flash** (`deepseek-v4-1-flash`). The operator reports GLM 5.3 Flash is no longer available. The production overlay requires an explicit `TINFOIL_MODEL` for both backend and Sage, and the example selects DeepSeek. The inspected demo's saved `LLM_MODEL` already equals DeepSeek. Verify the replayed `LLM_MODEL`, generated runtime files, and running Sage configuration agree before testing inference; retained deployment settings can override environment defaults. Keep the existing `nomic-embed-text` embedding model when reusing its vectors.

Dedicated organization keys named `liberator-production` are staged privately outside the repository for Tinfoil and Maple. Local checks through the pinned production proxies authenticate both keys, receive nonempty DeepSeek answers, and return finite 768-dimensional `nomic-embed-text` embeddings. Tinfoil's returned catalog lists DeepSeek and omits `glm-5-3-flash`. These use invented prompts, not organization content. The [provider evidence](production-provider-preparation-verification.json) records request results and the test client's host-header/user-agent requirements without secret values. The checks also pass from the new Droplet through separate pinned proxies using invented prompts; [host provider evidence](production-host-provider-verification.json) records the results. Protected application inference, including Maple fallback, remains to verify. The organization's Mailgun sending domain, SMTP authentication and delivery now pass as recorded below; application email sign-in remains acceptance work.

`docker-compose.liberator-production.yml` pins infrastructure image digests from the inspected demo. It uses production authentication settings, real email, a fresh database password and content key, baked backend source, restart policies, and the accepted Liberator branding selector. Backend startup initializes SQLite without running the demo's Qdrant smoke-test seeding or development reload command. Public listeners stay on loopback; Caddy supplies public TLS after the domain is ready. Uploaded files bind from `LIBERATOR_UPLOADS_DIR` (default `/srv/liberator/uploads`) outside the versioned checkout, so selecting another release keeps the same knowledge artifacts. Named volumes retain the same `liberator` Compose project name across releases.

The frontend application source is preserved. Dependency patch updates address the findings from its installation audit: [source-map-js 1.2.2](https://github.com/7rulnik/source-map-js/releases/tag/v1.2.2), [Vitest 4.1.11](https://github.com/vitest-dev/vitest/releases/tag/v4.1.11), and a supported Undici version newer than the [7.29.1 security release](https://github.com/nodejs/undici/releases/tag/v7.29.1). Clean installation and npm audit now report zero findings; build and test verification use the updated lockfile.

The pre-commit integration test clears inherited `GIT_*` variables when spawning commands in its temporary Git repository. Without this isolation, a real commit hook could direct the fixture's staging and user-config commands into the parent worktree. The actual hook and formatting behavior stay intact.

Create a private environment file using `.env.liberator.example` as the list of required settings. Generate fresh random authentication, internal-service, database, and content keys. `POSTGRES_PASSWORD` must be URL-safe; random hexadecimal works. Never deploy the example placeholders. Record the actual committed release revision and a unique image tag. Build with:

```bash
docker compose --project-name liberator --env-file /etc/enclave/production.env \
  -f docker-compose.infra.yml -f docker-compose.app.yml \
  -f docker-compose.liberator-production.yml --profile maple build
```

Do not bring up the public deployment until account credentials, DNS, and migration checks pass. Confirm real protected inference, SMTP delivery, cookie/CORS behavior, and service restarts before Technical Acceptance.

## Consistent encrypted backup

The previous `scripts/backup.sh` and `scripts/restore.sh` targeted legacy Neo4j and were unsuitable for this migration. They now delegate to the current Python commands; obsolete positional arguments fail instead of producing an incomplete legacy backup. Use `scripts/operations/backup.py` on the Linux Docker host with Python 3 and restic 0.19.1 or newer installed. The workflow uses restic's documented [encrypted snapshot backup](https://restic.readthedocs.io/en/stable/040_backup.html) and [verified isolated restore](https://restic.readthedocs.io/en/stable/050_restore.html) operations.

The tool discovers every bind mount and named volume in the selected Compose project, plus its Compose files and explicitly supplied private configuration. It includes SQLite, Postgres, Qdrant, uploads, Sage workspace, embedding cache, and mounted logs/configuration. It saves custom application images and complete container metadata for recovery. Runtime metadata contains credentials and must remain inside the encrypted repository or a private restored directory.

All originally running project containers stop while restic reads the snapshot, including databases and application writers. They restart in a `finally` block on success, error, or ordinary interruption; previously stopped containers stay stopped. Recovery starts supporting services first and waits for their health, then starts backend/Sage/frontend, then the gateway. A flat `docker start` does not honor Compose dependencies: the first live backup exposed Sage and gateway startup failures, which were recovered manually. Regression coverage now checks dependency readiness, upload failure, stopped-service preservation, timeout and failed recovery. Recovery failures still attempt remaining services and return failure rather than successful backup status. Provider readiness checks can fail during a provider outage; inspect actual host health when recovery reports failure.

This causes a maintenance interruption whose duration depends on storage and transfer volume. Measure it during the rehearsal. A host crash or forced kill can bypass cleanup; inspect and restart services using the private runtime manifest. A failed or incomplete backup is not reported as successful. The encrypted manifest also records inspected image metadata, including registry digests, for exact infrastructure recovery.

Create an organization-owned off-host repository, save the restic password separately, and initialize it once. Set the values from `scripts/operations/backup.env.example` in a private root-owned file. The developer saves recovery keys in Proton Pass and manually hands them to the organization over Signal. Losing the repository password prevents restoration even if the bucket is available.

With the private backup environment loaded, take a backup:

```bash
python3 scripts/operations/backup.py backup --project enclave-free \
  --config-path /opt/enclave-free/.env \
  --config-path /etc/caddy/Caddyfile
```

Inspect the actual source environment locations and any generated runtime env before choosing `--config-path`; repeat it for every required private configuration file. The tool fails on missing paths instead of silently skipping them. It requires an initialized off-host repository and a private password file. Custom image export needs temporary disk capacity; verify free space first.

Only complete snapshots receive the final `enclave:PROJECT` tag. Partial snapshots retain a candidate tag and must not displace successful daily copies. Install the example service/timer only after adapting checkout/project/config paths and completing a restore drill. The timer runs daily around 04:00 UTC; `systemctl status enclave-backup.service`, its journal, and successful snapshot metadata provide backup health evidence. An unattended alert recipient and notification transport still need to be selected.

The accepted retention is 14 daily and 4 weekly copies. Preview it first:

```bash
python3 scripts/operations/backup.py retention-preview --project liberator
```

The accepted policy can be applied with `backup.py retention-apply --project liberator`. It selects complete `enclave:liberator` snapshots, groups by tags across release paths/hosts, prunes unreferenced data, and checks repository metadata. Candidate snapshots, release archives and other project tags are excluded. Calendar-week copies can overlap daily copies; this policy does not promise 18 distinct snapshots. The [restic retention documentation](https://restic.readthedocs.io/en/stable/060_forget.html) describes those natural time boundaries and grouping. A real encrypted test repository with 35 daily snapshots verifies daily/weekly retention, a non-mutating preview, tag exclusions and a full post-prune data check.

The installed unit loads `/etc/enclave-backup/production-backup.env`, runs `/usr/local/lib/enclave-operations/backup.py`, and includes production env, Caddy config/certificate state, bucket credentials and the private restic password file in the encrypted snapshot. `ExecStartPost` applies retention only after backup and application recovery succeed. Separate disposable systemd units verify that post commands run after success and are skipped after failure. The production timer is installed and verified but remains **disabled and inactive** until production paths, a complete production backup and acceptance checks exist. [Backup preparation evidence](production-backup-operations-verification.json) records this state.

Before enabling it, run one manual backup, verify application recovery and isolated restoration, preview retention, and confirm the operator can inspect service failures and snapshot metadata. Then enable `enclave-backup.timer`; monitor `systemctl status enclave-backup.service`, `journalctl -u enclave-backup.service`, and `restic snapshots --tag enclave:liberator --latest 1`. An alert recipient/transport and ongoing operational responsibility remain handoff decisions.

## Isolated restoration

Restore an explicit successful snapshot into a new private directory:

```bash
python3 scripts/operations/backup.py restore \
  --snapshot SNAPSHOT_ID --target /srv/enclave-restore-drill
python3 scripts/operations/backup.py check
```

Restic verifies restored file contents. The restore tool refuses existing destinations and does not overwrite live application stores. Original absolute source paths appear beneath the isolated target. Locate the private `recovery/runtime.json` using the restored path recorded in snapshot metadata. It records mounts, images, commands, environment, and Compose layers.

An application restore drill still requires a separate Linux host, or an isolated Compose project with independent container names, ports, networks, and writable mounts. The base Compose files use fixed container names, so a second project on the same host needs explicit overrides. Load `custom-images.tar`, restore the recorded image tags from image IDs, recreate the five named data volumes and any anonymous persistent mounts, and copy the restored files into their corresponding targets preserving ownership. Bind uploads and configuration at their restored locations. Use the recorded Postgres major version and source keys; never point this restored copy at the demo's writable stores. Disable public access and real outgoing mail during the drill. Start the isolated stack and verify database integrity, Admin access, active document hashes/retrieval, Sage settings, and persistence. Record recovery elapsed time and all target checks. File-level restoration alone is not Technical Acceptance.

## Live recovery and retained-data rehearsal — 2026-10-12

The encrypted demo snapshot covers 24 storage/configuration sources and three custom application images. The new server passed a full repository read check and verified private file restoration. Exact infrastructure image provenance for this first snapshot is stored in a separate linked encrypted snapshot; subsequent backups include it in `runtime.json`.

The restored stack uses copied writable stores on an internal Docker network, with only loopback test bindings and no outbound mail/inference. Its frontend, backend, Sage, gateway, Postgres, Qdrant, Valkey and SearxNG are healthy; frontend and gateway checks return HTTP200 through private bridge addresses. Tinfoil startup requires remote attestation and therefore exits under this intentional isolation; provider health checks are disabled in the drill. SQLite integrity/foreign keys pass. The fixed restart code successfully stops and recovers the nine eligible containers in 86.9 seconds, including their graceful shutdown. These are application-core recovery checks; protected Admin access and live inference/email remain acceptance work.

Real retained-state replay preserves all 30 active artifacts, 964 chunks and 910 vectors, accounts for three absent inactive artifacts, and excludes all SQLite identities/history. Fresh clean Postgres receives exactly 9 Sage configuration rows and 1 override with zero agents, sessions and messages. Its state and Qdrant's vectors persist across restart, vector payloads/values match, and stored-vector retrieval passes. The actual recovered application decrypts every retained file/chunk and verifies its plaintext hash, as well as the replacement encrypted provider setting. Destination URLs/SMTP/provider values and encryption/authentication keys in this rehearsal are explicit fixtures; do not deploy this output as production. The final reviewed freeze and actual destination values remain required.

Results are recorded in [infrastructure evidence](production-infrastructure-verification.json), [application recovery evidence](production-application-restore-verification.json), [real carryover evidence](production-real-carryover-verification.json), and [clean-store evidence](production-clean-store-replay-verification.json). Daily scheduling and retention are not yet enabled.

## Filtered configuration and knowledge replay

`scripts/operations/carryover.py` requires Python 3 and PyCryptodome (also required by the backend). It uses the pinned schema and an explicit table boundary. It carries Instance Settings, User Types, Onboarding Questions, both Enclave and Sage Agent Settings and their User Type overrides, resources, geographic reference data, document metadata/defaults, retrieval chunks, and the knowledge vectors. It never copies Admin identity, users/profiles, conversations, memories, test personas, session feedback, old audit logs, old inference-verification records, or lifecycle history. Unknown SQLite tables or unmatched vector payloads stop export for review.

The live inspection found 30 readable active artifacts, 33 ingest jobs, 964 chunks, and 910 active knowledge vectors. Three absent artifacts belong to inactive/replaced jobs; preserve their metadata and all 54 historical chunks and explicitly report the missing historical files. Missing active files or vectors fail export. The demo has no configured content key; its inspected active files and chunks are plaintext. Replay encrypts files and chunks with the fresh production key, checking plaintext SHA-256 hashes before and after encryption. Encrypted source material is deliberately decrypted using `SOURCE_CONTENT_ENCRYPTION_KEY`; unsupported legacy envelopes fail for repair.

Before excluding Admin Conversations, review them for organization instructions that were never saved as configuration. Save any required instructions into the corresponding Instance or Agent Settings before the final freeze. The export deliberately omits conversations and cannot recover unsaved instructions from them.

Export only from a frozen source or an isolated restored copy. SQLite reads alone cannot guarantee consistency across Postgres, Qdrant, and uploaded files. First take the verified full backup; then stop configuration/knowledge changes and in-flight ingests for the export. Qdrant and Postgres can remain available read-only while application writers are stopped.

Set `MIGRATION_BUNDLE_KEY_FILE` to a private file containing 32 random bytes encoded as 64 hex characters. This key encrypts the entire transfer bundle, including settings and document content; it is separate from the production content key. Keep plaintext temporary work private. With source storage paths mapped correctly:

```bash
python3 scripts/operations/carryover.py export \
  --sqlite /PATH/TO/FROZEN/enclave.db --uploads /PATH/TO/FROZEN/uploads \
  --sage-container ISOLATED_SOURCE_POSTGRES \
  --qdrant-url http://ISOLATED_SOURCE_QDRANT:6333 \
  --output /PRIVATE/PATH/carryover.encrypted --source-is-frozen
```

The source stores upload paths under `/uploads` by default. `--stored-uploads-prefix` changes that mapping. The Postgres adapter selects only its two Agent Settings tables. An already captured private JSON file with `ai_config` and `ai_config_user_type_overrides` can be supplied with `--sage-config-json` instead. Do not export whole conversation databases as a workaround.

Create a fresh, initialized Enclave schema database with no identities, audit, or verification history. Initialization uses the pinned application's schema, not the demo database. Configure fresh `CONTENT_ENCRYPTION_KEY` and `SECRET_KEY` in the replay process. Supply a private JSON object with explicit destination values for every secret and deployment-specific setting named in the encrypted manifest: public domain/origins, internal endpoints, storage paths, SMTP identity/credentials, provider keys, and certificate/monitoring locations. Optional unused settings may be explicitly empty. Never transfer personal-account credentials. Production content key in this object must match the replay environment. Review reachout recipients and other retained organization settings before Public Opening.

```bash
python3 scripts/operations/carryover.py replay \
  --bundle /PRIVATE/PATH/carryover.encrypted \
  --schema-db /PRIVATE/PATH/fresh-schema.db \
  --destination-values /PRIVATE/PATH/production-values.json \
  --output-dir /PRIVATE/PATH/new-replay
python3 scripts/operations/carryover.py verify --output-dir /PRIVATE/PATH/new-replay
```

Replay writes a new private directory containing the clean Enclave SQLite database, encrypted uploads, Sage-only replay SQL/configuration, vector data, and manifest. It compares counts, foreign keys, SQLite integrity, artifact/chunk hashes, and excluded-state counts. A `REPLAY_COMPLETE.json` marker appears only after offline verification. A failed replay directory must be treated as incomplete; restart into a new directory after resolving the failure.

With an isolated clean destination's databases available and application writers stopped, apply the remaining stores:

```bash
python3 scripts/operations/carryover.py apply-sage \
  --output-dir /PRIVATE/PATH/new-replay --postgres-container CLEAN_DESTINATION_POSTGRES
python3 scripts/operations/carryover.py apply-index \
  --output-dir /PRIVATE/PATH/new-replay --qdrant-url http://CLEAN_DESTINATION_QDRANT:6333
```

Sage replay refuses an existing identity/conversation database and compares retained settings after applying them. Qdrant replay requires an absent or empty matching knowledge collection, retains point IDs and payloads, and compares vectors within float32 normalization tolerance. A failed partial vector application remains incomplete; use a new isolated destination for another rehearsal. Install staged SQLite/uploads into the clean destination with application ownership, start the stack, bootstrap the new organization Admin identity, and verify full product behavior. No Sage workspace or user identity is copied by this transfer; the full backup separately preserves the demo for recovery.

## Verification commands and limits

```bash
python3 -m unittest discover -s scripts/operations -p test_operations.py -v
```

For the opt-in storage rehearsal, create disposable containers named `liberator-migration-qdrant-fixture` (Qdrant v1.12.6 on loopback port 16333) and `liberator-migration-postgres-fixture` (the pinned pgvector/Postgres 17 image on loopback port 15432, with `POSTGRES_USER=postgres`, `POSTGRES_DB=postgres`, and a test-only password). Start with empty databases. Run:

```bash
ENCLAVE_RUN_STORAGE_REHEARSAL=1 python3 -m unittest discover \
  -s scripts/operations -p test_storage_rehearsal.py -v
```

This checks real Qdrant vector replay and real Postgres configuration SQL, plus encrypted restic storage/restoration and failure handling with a simulated Docker lifecycle. These fixtures contain invented data. The rebuilt backend also passes schema/crypto and CPU-runtime checks; a private minimal backend/Sage/Postgres runtime starts with zero Sage agents, sessions, or messages. Its backend health is degraded because Qdrant and Valkey are deliberately absent from that fixture. Passing these checks does not prove a complete demo restore or a production migration. Record the real-data rehearsal, complete restore drill, provider verification, Admin/email login, TLS, rollback, and zero-user handoff separately. Preparation results and outstanding checks are recorded in [verification evidence](production-preparation-verification.json).

## Release delivery and Admin recovery

Application images use Linux AMD64, fixed release tags and source-revision labels. The prepared versioned release contains Enclave source, pinned Sage source, the three application images and a SHA-256 manifest. Verify archive sizes/hashes before loading; extract Enclave into a new `/opt/liberator/releases/RELEASE` directory and Sage into its `runtime/sage`. Compare all loaded image configurations and filesystem-layer digests with the manifest, along with architecture and source labels. Record the observed target image IDs. In this verified transfer, builder and target image IDs differ while their complete configurations and filesystem-layer digests match. Docker uses distinct [classic and containerd image stores](https://docs.docker.com/engine/storage/containerd/); portable content checks are the acceptance evidence for this transfer. Deployment configuration can have a later revision than application images when backend/frontend/Sage build inputs are unchanged; record both revisions. Use the fixed `liberator` project name, `/srv/liberator/uploads`, a private completed environment and the production overlay. Start with `--no-build --pull never` only after all application/infrastructure image IDs are locally present. Keep the previous release and image IDs for code rollback; restore data from a verified backup when schema changes require it. Code rollback alone does not reverse a database migration.

The delivery artifact also needs an encrypted off-host snapshot and a verified isolated restore, so recovery does not depend on the current server. Release `release-cd26fb4` is staged at `/srv/liberator/releases/release-cd26fb4`, with source extracted at `/opt/liberator/releases/release-cd26fb4`. All three application images and seven pinned infrastructure images are present. [Release verification](production-release-verification.json) records the archive checks, image content comparison and encrypted recovery drill. The production application is now running privately, as recorded in the final replay evidence below. GitHub package publication is optional for this archive-based delivery; the current operator login lacks package-write scope. The organization owns the source repositories and artifact storage.

A fresh production Nostr Admin key is generated and signature/encoding checks pass. Its private `nsec` is staged only in the developer's private handoff directory, outside Git and the server. No instance has been bootstrapped with it. The operator requested computer use to save it in Proton Pass; the saved Personal-vault note is verified. Signal delivery is authorized and awaits the exact recipient. Complete that handoff before production Admin setup. Application backups preserve the Admin public key, so keep the private Admin recovery key separately from the server and its content-encryption keys.

## Domain, HTTPS and Mailgun — 2026-10-12

Namecheap manages the organization's `liberator.global` domain using BasicDNS. The apex A record points to `167.99.112.149`; `www` is a CNAME to the apex. Both authoritative nameservers and Cloudflare/Google public resolvers return the expected records. Caddy 2.11.7 is installed from its official repository, and UFW permits TCP 22, 80 and 443. Both hostnames have valid Let's Encrypt certificates and redirect HTTP to HTTPS. The active configuration returns a preparation message with HTTP 503, `X-Robots-Tag: noindex, nofollow` and `Cache-Control: no-store`. No production application is running.

[Closed configuration](../deploy/Caddyfile.liberator.closed) preserves this state. [Production configuration](../deploy/Caddyfile.liberator.production) is validated but inactive: it redirects `www` to the apex and routes `/api/*` to the loopback gateway and other requests to the frontend. Promote it only after the final replay, protected product verification and the organization's opening decision. Until then, keep the preparation configuration active. The private environment now contains the real public URL and organization SMTP values; it is staged at `/srv/enclave-migration/production.env.prepared`, not installed as the active `/etc/enclave/production.env`.

The WLC Mailgun account has a US sending domain, `mg.liberator.global`, with automatic sender security. Namecheap contains its SPF TXT record, both rotating DKIM CNAME records and the `email.mg` tracking CNAME, plus `_dmarc.mg` TXT `v=DMARC1; p=none;`. All five records match on both authoritative servers and both public resolvers. Mailgun reports SPF and tracking verified, the first signing key active and the standby signing key verified. This is a sending-only subdomain; Mailgun's optional incoming MX records are omitted, and the apex's existing Namecheap email forwarding is preserved. Mailgun documents [which DNS records support sending and receiving](https://documentation.mailgun.com/docs/mailgun/user-manual/domains/domains-verify).

A dedicated SMTP user, `liberator-production@mg.liberator.global`, has a fresh password saved outside Git. The new server authenticates to `smtp.mailgun.org:2525` over certificate-verified TLS 1.3. Exactly one user-authorized test email was sent from `Liberator <noreply@mg.liberator.global>` to the nominated Proton recipient. Mailgun accepted it and reports the matching message ID delivered to Proton's MX over certificate-verified TLS, with SMTP 250. The recipient confirms Inbox placement, and native Safari inspection verifies the message in Inbox with the expected sender, subject and test body. Proton reports no trackers found. The test disables tracking. This establishes SMTP/domain delivery; application magic-link generation, public origins, cookies and sign-in are still untested.

[Domain and mail evidence](production-domain-mailgun-verification.json) records the checks without credentials or the recipient address. An encrypted `liberator:setup` snapshot in the organization's Spaces production repository preserves Caddy configuration/certificate state, the private prepared environment and backup recovery credentials. A full repository read check and isolated verified restore pass, with all 16 files byte-equal in 9.7 seconds. This is setup recovery, not a complete production application restore. The scheduled application backup timer remains disabled and inactive.

## Final source export and private production import — 2026-10-12

The final Admin Conversation review covers all 45 Admin sessions, 205 messages and 88 Admin prompts. No required unsaved global instruction was identified: the direct, compassionate, practical style is already saved in Sage. The latest saved automatic approval setting remains enabled. Conversation content was excluded, and the temporary local review capture was removed after recording this sanitized finding.

A fresh encrypted full demo backup and filtered export were taken at the same frozen data point. The backup records the original running containers; supporting services restart for read-only export while application writers remain stopped, then all ten source services recover in a final recovery step. The measured writer outage was 105.14 seconds. The encrypted bundle's SHA-256 matches on the demo, operator machine and organization server; temporary organization backup/bundle credentials were removed from the demo. This transfer preserves 34 Instance Settings, 3 User Types, 23 Onboarding Questions, 13 Enclave Agent Settings, 9 Sage settings and 1 Sage override, 23 resources, 33 document/default records, 197 countries, 964 chunks, 30 files and 910 vectors. Three missing inactive historical artifacts remain explicitly accounted for.

The actual production environment is installed privately at `/etc/enclave/production.env`, and release `release-cd26fb4` starts with the fixed `liberator` project and organization provider/SMTP credentials. Clean replay encrypts all active files and chunks using the fresh content key, and replaces every deployment-specific setting with the actual production value. The running application decrypts all 30 files and 964 chunks, matches their plaintext hashes and replacement secrets, and passes SQLite integrity/foreign-key checks. All ten services are healthy. SQLite has zero users/Admins; Sage has zero agents/sessions/messages. Production's own new startup audit and successful DeepSeek provider-verification record are retained; no demo history was imported.

Initialize fresh Sage stores through the pinned application's own migrations before applying retained Sage settings. A schema-only SQL dump omits its migration ledger; the first empty schema was replaced and initialized correctly before acceptance. Retained Sage settings and all vector identities/payloads/values match after application. A temporary secret-cookie gate verified the HTTPS production origin, then was revoked; the active edge again matches the closed template and public requests receive HTTP 503. Admin bootstrap, protected product acceptance, application email sign-in and technical handoff remain pending. The organization controls Public Opening and the demo remains healthy. See [final production replay evidence](production-final-replay-verification.json).

The first complete encrypted production snapshot contains all 17 discovered storage/configuration sources and three custom images. Verified file restoration and retention preview pass (42.49 seconds including repository checks). The isolated application core starts in 135.2 seconds with no published ports; its gateway/frontend, database integrity, all retained metadata and fresh secrets, all document/chunk hashes, Sage settings and exact knowledge vectors match. Stored-vector retrieval also passes. Outgoing mail/inference are blocked, Tinfoil is deliberately left stopped, and its isolated provider health checks are disabled; protected Admin/email/inference acceptance remains separate. All ten temporary recovery containers and both restored directories were removed after verification. The fresh final demo backup also passes verified file restoration; that temporary copy was removed.

The first-deployment rollback check restores the closed edge, stops application writers, confirms public HTTP 503, and recovers the same accepted release with unchanged data volumes in 80.15 seconds. Live data and all ten service health checks pass again afterward. This verifies aborting private activation and recovering the accepted release; it does not test downgrading a future release across schema changes. The daily backup timer remains disabled until remaining acceptance and handoff checks pass.
