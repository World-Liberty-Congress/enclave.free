# Liberator production preparation

This branch prepares the organization-owned deployment without changing the personal-account demo. The GitHub destinations are [Enclave](https://github.com/World-Liberty-Congress/enclave.free) and [Sage](https://github.com/World-Liberty-Congress/sage). DigitalOcean provisioning awaits Liberator's Owner adding a payment method. Domain, organization provider credentials, and off-host backup storage remain external inputs.

## Release baseline

Enclave starts from `43703a9db18f2bbc07aefa94bb24b257d4c896ab`, the accepted Liberator integration. Its backend matches the running demo backend at `285d2e1ab474cf954737fea8d5db58de95c2af5a`, and its frontend matches the accepted branding at `b8f1625da5c1198af2dda52cf19aec083575e363`. The later integration commits change documentation only. Sage is pinned to `661b118343dc3d7567f0fb05431ade9175adc93f` on the organization's `production/liberator-baseline` branch; all 68 remote build-source files match this revision. All three Linux AMD64 application images have built locally; their source labels and local image IDs are recorded in the verification evidence. Registry publication and registry manifest digests remain pending.

The accepted production Tinfoil model is **DeepSeek V4.1 Flash** (`deepseek-v4-1-flash`). The operator reports GLM 5.3 Flash is no longer available. The production overlay requires an explicit `TINFOIL_MODEL` for both backend and Sage, and the example selects DeepSeek. The inspected demo's saved `LLM_MODEL` already equals DeepSeek. Verify the replayed `LLM_MODEL`, generated runtime files, and running Sage configuration agree before testing inference; retained deployment settings can override environment defaults. Keep the existing `nomic-embed-text` embedding model when reusing its vectors.

Dedicated organization keys named `liberator-production` are staged privately outside the repository for Tinfoil and Maple. Local checks through the pinned production proxies authenticate both keys, receive nonempty DeepSeek answers, and return finite 768-dimensional `nomic-embed-text` embeddings. Tinfoil's returned catalog lists DeepSeek and omits `glm-5-3-flash`. These use invented prompts, not organization content. The [provider evidence](production-provider-preparation-verification.json) records request results and the test client's host-header/user-agent requirements without secret values. Repeat the checks from the new Droplet and through the protected application, including Maple fallback. Mailgun has account access and a payment method, but production sending-domain/DNS/SMTP setup and delivery await the organization's domain.

`docker-compose.liberator-production.yml` pins infrastructure image digests from the inspected demo. It uses production authentication settings, real email, a fresh database password and content key, baked backend source, restart policies, and the accepted Liberator branding selector. Backend startup initializes SQLite without running the demo's Qdrant smoke-test seeding or development reload command. Public listeners stay on loopback; Caddy supplies public TLS after the domain is ready.

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

All originally running project containers stop while restic reads the snapshot, including databases and application writers. They restart in a `finally` block on success, error, or ordinary interruption; previously stopped containers stay stopped. This causes a maintenance interruption whose duration depends on storage and transfer volume. Measure it during the rehearsal. A host crash or forced kill can bypass cleanup; inspect and restart services using the private runtime manifest. A failed or incomplete backup is not reported as successful.

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

After reviewing the repository and retention preview, configure the equivalent restic `forget --tag enclave:liberator --keep-daily 14 --keep-weekly 4 --prune` job. This repository-scoped deletion is separate from backup creation; it is not enabled by the preparation branch.

## Isolated restoration

Restore an explicit successful snapshot into a new private directory:

```bash
python3 scripts/operations/backup.py restore \
  --snapshot SNAPSHOT_ID --target /srv/enclave-restore-drill
python3 scripts/operations/backup.py check
```

Restic verifies restored file contents. The restore tool refuses existing destinations and does not overwrite live application stores. Original absolute source paths appear beneath the isolated target. Locate the private `recovery/runtime.json` using the restored path recorded in snapshot metadata. It records mounts, images, commands, environment, and Compose layers.

An application restore drill still requires a separate Linux host, or an isolated Compose project with independent container names, ports, networks, and writable mounts. The base Compose files use fixed container names, so a second project on the same host needs explicit overrides. Load `custom-images.tar`, restore the recorded image tags from image IDs, recreate the five named data volumes and any anonymous persistent mounts, and copy the restored files into their corresponding targets preserving ownership. Bind uploads and configuration at their restored locations. Use the recorded Postgres major version and source keys; never point this restored copy at the demo's writable stores. Disable public access and real outgoing mail during the drill. Start the isolated stack and verify database integrity, Admin access, active document hashes/retrieval, Sage settings, and persistence. Record recovery elapsed time and all target checks. File-level restoration alone is not Technical Acceptance.

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
