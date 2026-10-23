# Internal Agent Contract v0

This document is the source of truth for the private `/internal/agent/*` and
`/internal/lifecycle/*` contracts between Sage and the Python control plane on
the current prototype.

These routes are not part of the public product API. Sage is the intended
caller, Python is the owner, and both sides must evolve request and response
schemas together. They are protected by the shared internal token even when
reachable through a local development gateway.

## Authentication

Every endpoint requires:

```http
X-Internal-Agent-Token: <INTERNAL_AGENT_TOKEN>
```

Clients should send `Accept: application/json`. `POST` endpoints require JSON
request bodies and should send `Content-Type: application/json`. Responses are
JSON, including FastAPI error payloads.

If `INTERNAL_AGENT_TOKEN` is unset in Python, protected routes return `503`.
If the header is missing or wrong, protected routes return `403`.

## Error Responses

The current implementation uses FastAPI's default error shape for auth,
validation, and explicit `HTTPException` failures:

```json
{
  "detail": "Invalid internal agent token"
}
```

Validation failures use FastAPI's structured `detail` array. Example:

```json
{
  "detail": [
    {
      "loc": ["body", "query"],
      "msg": "Field required",
      "type": "missing"
    }
  ]
}
```

Common statuses:

- `403`: missing or invalid `X-Internal-Agent-Token`
- `404`: missing user, admin, or user type records
- `422`: malformed JSON, missing required request fields, or invalid field types
- `503`: Python service has no `INTERNAL_AGENT_TOKEN` configured
- `5xx`: embedding, Qdrant, document-store, SQLite, or other infrastructure failure

## Active Endpoints

### `GET /internal/agent/health`

Returns internal-agent health.

Response:

```json
{
  "status": "healthy"
}
```

## Lifecycle Contract

Session Memory Deletion is now modeled as an operator-visible lifecycle
workflow under `/internal/lifecycle/*` rather than a bare public query-session
delete. The Enclave Control Plane owns deletion tombstones and Audit Log
evidence; Sage owns the Session Memory deletion target and must report
sanitized per-target lifecycle results.

### `POST /internal/lifecycle/session-memory/delete`

Deletes Sage-owned Session Memory for one Conversation. This endpoint is
internal-only and requires `X-Internal-Agent-Token`.

Request:

```json
{
  "conversation_id": "uuid"
}
```

Response:

```json
{
  "status": "deleted",
  "deletion": {
    "status": "succeeded",
    "retryable": false,
    "counts": {
      "succeeded": 1,
      "skipped": 0,
      "failed": 0
    },
    "results": []
  }
}
```

Field contract:

- `status`: outer endpoint outcome; currently `deleted` when Sage accepted the lifecycle delete request. Transport or auth failures use HTTP error status codes rather than this field.
- `deletion.status`: aggregate lifecycle result. Allowed values are `succeeded`, `partial_failure`, and `failed`.
- `deletion.retryable`: `true` when at least one failed target can be retried by the control plane; `false` when the result is complete or only contains non-retryable skips.
- `deletion.counts.succeeded`: number of lifecycle targets successfully deleted.
- `deletion.counts.skipped`: number of targets intentionally skipped, such as already-absent Session Memory or non-applicable targets.
- `deletion.counts.failed`: number of lifecycle targets that failed.
- `deletion.results`: per-target lifecycle evidence. It may be empty when Sage has no target-level detail to report. When populated, each result object includes `target_kind`, `target_id`, `action`, `status`, `retryable`, and `detail`. Failure details must use sanitized lifecycle error categories rather than raw backend errors or secrets.

The control plane sanitizes failed lifecycle details into stable categories
such as `target_unavailable`, `not_found`, `already_deleted`,
`unauthorized_internal_contract`, or `target_error` before storing tombstones or
Audit Log evidence. Tombstones must remain metadata-only: they may include a
Conversation id, Former Subject Reference, lifecycle class, source workflow,
retry count, status, and sanitized deletion result, but no Conversation Content
or deleted User PII.

Until secure erase and complete historical log/session retention are defined,
docs and UI must not imply secure erase or complete historical deletion.

### `GET /internal/agent/users/{user_id}`

Hydrates a user after Sage verifies a user session token.

Response:

```json
{
  "id": 123,
  "approved": true,
  "email": "user@example.com",
  "name": "User Name",
  "user_type_id": 1,
  "dev_mode": false
}
```

Missing users return `404`:

```json
{
  "detail": "User not found: 123"
}
```

### `GET /internal/agent/admins/by-pubkey/{pubkey}`

Hydrates an admin after Sage verifies an admin session token.

Response:

```json
{
  "id": 1,
  "pubkey": "<hex-pubkey>",
  "session_nonce": 0
}
```

Missing admins return `404`:

```json
{
  "detail": "Admin not found: <hex-pubkey>"
}
```

### `GET /internal/agent/user-types/{user_type_id}`

Returns user-type metadata for Sage config and admin responses.

Response:

```json
{
  "id": 1,
  "name": "Client",
  "description": "Optional description",
  "icon": "user",
  "display_order": 0,
  "created_at": "2026-04-12T00:00:00"
}
```

Missing user types return `404`:

```json
{
  "detail": "User type not found: 1"
}
```

### `GET /internal/agent/document-access`

Returns document IDs visible/defaulted for a user type.

Query:

- `user_type_id`: optional integer

Response:

```json
{
  "user_type_id": 1,
  "available_document_ids": ["job_123"],
  "default_document_ids": ["job_123"]
}
```

### `GET /internal/agent/user-profile-context/{user_id}`

Returns profile context that Sage may include in prompts.

Query:

- `user_type_id`: optional integer

Response:

```json
{
  "user_id": 123,
  "user_type_id": 1,
  "profile": {
    "Country": "Nicaragua"
  }
}
```

### `POST /internal/agent/document-search`

Runs Enclave document retrieval with Python-owned document access rules.

Request:

```json
{
  "query": "What documents apply?",
  "user": {
    "id": 123,
    "type": "user",
    "approved": true,
    "pubkey": null,
    "email": "user@example.com",
    "name": "User Name",
    "user_type_id": 1,
    "dev_mode": false
  },
  "top_k": 8,
  "job_ids": ["job_123"],
  "jurisdiction": "Nicaragua",
  "situation_details": "Optional context"
}
```

Response:

```json
{
  "sources": [],
  "context": "",
  "search_query": "What documents apply?",
  "top_k": 8
}
```

Qdrant, embedding, or document-store failures bubble as 5xx responses.
Malformed JSON, missing `query` or `user`, invalid field types, or invalid
`user` object shape return `422`. Validated request fields include `query`,
`user`, `top_k`, `job_ids`, `jurisdiction`, `situation_details`, and
`user.user_type_id`.

### `POST /internal/agent/resources/search`

Runs admin-curated Resource Directory lookup for Sage's `find_resources` Tool.
This is separate from document retrieval: resources are structured SQLite rows
entered by admins, not uploaded document chunks.

Request:

```json
{
  "query": "alpha@example.org",
  "kind": "organization",
  "tags": ["legal", "humanitarian"],
  "region": "Nicaragua",
  "language": "es",
  "limit": 10,
  "offset": 0
}
```

All filters are optional. `tags` uses AND semantics: every requested normalized
tag must be present. `kind` accepts the generic Resource kinds `person`,
`organization`, `product`, `service`, `method`, `reference`, or `other`.

Response:

```json
{
  "resources": [
    {
      "resource_id": "example-ni-detention-lawyer",
      "name": "Example Nicaragua Detention Counsel",
      "kind": "organization",
      "description": "Legal support for detention cases.",
      "tags": ["legal", "humanitarian"],
      "pointers": [
        {"type": "email", "label": "Intake", "value": "help@example.org"},
        {"type": "url", "value": "https://example.org"}
      ],
      "regions": [{"level": "country", "code": "NI"}],
      "languages": ["es"],
      "provenance": {
        "verified_at": "2026-06-01T00:00:00Z",
        "vetted_by": "Admin",
        "source_note": "Customer manual"
      }
    }
  ],
  "query": "alpha@example.org",
  "resolved_country_code": "NI",
  "total_count": 12,
  "returned_count": 10,
  "limit": 10,
  "offset": 0,
  "has_more": true,
  "next_offset": 10
}
```

Only `ready` resources are returned. Query matching checks normalized Resource
ID, name, and pointer values exactly before partial name/pointer matches and
description fallback; phone pointer equality compares digits and names tolerate
punctuation and spacing differences. Query relevance is ranked before regional
specificity, provenance verification, optional language match, display order,
and name. `total_count` describes the complete filtered ready set before
pagination; `has_more` and `next_offset` describe continuation. Invalid payload
shapes and obsolete aid-specific fields return `422`.

### `POST /internal/agent/session-logs`

Records a User Conversation for Admin review (ADR-0035). Sage calls this after
each exchange with every retained user and assistant message of the
Conversation, oldest first, and the Sage sequence number of the newest one. If
the update does not complete, Sage resends it about five minutes after the
Conversation's last activity.

Request:

```json
{
  "actor": { "id": 123, "type": "user", "approved": true, "user_type_id": 1 },
  "sage_session_id": "11111111-1111-1111-1111-111111111111",
  "through_sequence_id": 8812,
  "messages": [
    {
      "message_id": "22222222-2222-2222-2222-222222222222",
      "role": "user",
      "content": "Earlier question",
      "ts": "2026-10-23T12:00:00+00:00"
    },
    {
      "message_id": "33333333-3333-3333-3333-333333333333",
      "role": "assistant",
      "content": "Earlier answer",
      "ts": "2026-10-23T12:00:04+00:00"
    }
  ],
  "title": "User Conversation - Avery"
}
```

Response:

```json
{ "log_id": "4f0c2b9e8d7a4c1b9e3f5a6d7c8b9a01", "status": "completed", "turn_count": 2 }
```

The backend keeps one record per Sage session and User, takes the User Type from
the User's current record, and encrypts the transcript to the Admin pubkey. An
update whose `(through_sequence_id, message count)` is not above the stored
record's changes nothing, so retries and late updates are safe; a higher one
replaces the record. `turn_count` is the number of recorded messages. Admin
actors receive `403`; a missing admin key returns `409` with nothing stored.
Deleted Users and provisioned Test Users return `status: "skipped"` with an
empty `log_id`. Payloads without message IDs, or with roles other than `user`
and `assistant`, return `422`.

### `GET /internal/agent/admin-db-schema`

Returns the live structure of the existing admin SQL allowlist. Sage exposes
this as `db_schema` only for Admin turns with the Database (`db-query`) tool
set enabled. Without parameters the response contains `dialect`, `tables`
(name and description), `discovery`, `query_rules`,
`identity_guidance`, and `usage` (metric, definition, coverage, example
`top_users_sql` and `population_counts_sql`). Passing `?table=users` (or another
listed table) returns that table's name, description, column names/types, and foreign keys within the
allowlist. A disallowed or missing table returns `400`. Neither mode contains
row values, column defaults, or raw DDL. Each response fits Sage's 4,000-character
per-result budget so metric definitions and example SQL survive intact.

Schema discovery precedes natural-language SQL generation so the model does
not have to guess table names or probe forbidden SQLite metadata tables.
The existing query allowlist and read-only restrictions are unchanged.

For an unspecified usage ranking, the supported measure is **recorded turns**
from retained `session_logs`, counting both user and assistant messages. Each
`source='user'` log holds every message Sage retains for that Conversation. Join
`users.id = session_logs.subject_user_id` with `source='user'` in the join
condition; exclude Admin tests and preserve zero-activity users. Report the
metric and coverage, conversation counts, and last log update. This is not
an exact live Postgres message count or a per-message time-window report.
Approval does not imply activity. Names can be matched by ID from the
optional, potentially partial AdminSigner-DecryptedContext; SQL cannot
decrypt encrypted identities.

One-to-many joins must count distinct user IDs for population totals. Counting
joined rows would count a user once per conversation or profile answer.

Regression coverage lives in `backend/tests/test_admin_db_query_endpoint.py`
and Sage's `web_runtime` tests. For a live-model replay against the disposable
`liberator-admin-db-local` Compose project, run
`python3 scripts/tests/TOOLS/check_admin_database_questions.py --seed --output /tmp/admin-db-ranking.json`.
The harness refuses other container projects and non-local API hosts. Use
`--case inventory`, `structure`, `partial-identities`, `no-tools`, or `user` on subsequent
runs (without `--seed`) to check counts, identity coverage, and tool gating.

### `POST /internal/agent/admin-db-query`

Executes Python-owned safe read-only admin SQL.

Request:

```json
{
  "sql": "SELECT COUNT(*) AS count FROM users"
}
```

Response:

```json
{
  "success": true,
  "columns": ["count"],
  "rows": [{ "count": 0 }],
  "executionTimeMs": 1,
  "error": null
}
```

Invalid or unsafe SQL returns `success: false` with an `error` string rather
than throwing, unless an unexpected infrastructure failure occurs.

Example unsafe SQL response:

```json
{
  "success": false,
  "columns": [],
  "rows": [],
  "executionTimeMs": 0,
  "error": "Only SELECT queries are allowed. Use the CRUD endpoints for modifications."
}
```

### Admin Config Tool Endpoints (ADR-0028)

The `admin-config` Tool Set will be exposed to the model by Sage as concrete Tools.
Python provides private execution endpoints for the Enclave Control Plane facts
behind those Tools. For Admin Config Tool reads, Python does not classify the
Admin's message, choose a scope, or return prompt-ready context text; it returns
structured Tool data. This does not remove other active context endpoints such
as `GET /internal/agent/user-profile-context/{user_id}`.

All Admin Config Tool endpoints require the private `X-Internal-Agent-Token`
header plus an internal actor envelope, and only authorize approved admins:

```json
{
  "actor": {
    "id": 1,
    "type": "admin",
    "approved": true,
    "pubkey": "<hex-pubkey>"
  }
}
```

Authorization behavior:

- If Python has no configured `INTERNAL_AGENT_TOKEN`, protected routes return
  `503`. Missing or invalid `X-Internal-Agent-Token` returns `403` before the
  body is trusted. The token authenticates Sage or another internal service
  caller; it does not by itself identify the user/admin actor for these Tool
  endpoints.
- The `actor` envelope is required in every request body. Sage generates it from
  the already-authenticated Conversation actor; browsers do not call these
  private endpoints or provide this envelope directly.
- After the token check succeeds, Python cross-checks the `actor` envelope
  against the admin registry. `actor.type` must be `admin`, `actor.approved`
  must be true, `actor.pubkey` must identify an existing admin, and `actor.id`
  must match that admin row.
- If a future internal token ever carries actor claims, any mismatch between
  token claims and the body envelope must fail closed rather than allowing the
  body to escalate privileges.
- Non-admin or unapproved actors return `403`
- Partial read failures after auth may return available structured data plus
  `warnings`; authorization failures fail closed

Target private endpoints behind the Sage Tool contracts:

Sage also exposes `read_admin_setup_summary` as a model-callable Admin Config
Tool. It is a Sage-local aggregate over the lower-level Admin Config read
contracts below, not a separate Python endpoint. Broad setup, status, and
readiness questions should use it first.

| Endpoint                                                 | Sage Tool                   | Notes                                                                                                                 |
| -------------------------------------------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `POST /internal/agent/admin-config/instance-settings`    | `read_instance_settings`    | Instance branding, public behavior, visual identity, and other Instance Settings                                      |
| `POST /internal/agent/admin-config/deployment-settings`  | `read_deployment_settings`  | Deployment Settings with secret values masked by default                                                              |
| `POST /internal/agent/admin-config/deployment-readiness` | `read_deployment_readiness` | Desired/generated/running deployment readiness, stale runtime posture, restart-required status, and validation status |
| `POST /internal/agent/admin-config/agent-settings`       | `read_agent_settings`       | Agent Settings, prompt sections, runtime parameters, defaults, and effective per-user-type values when requested      |
| `POST /internal/agent/admin-config/user-types`           | `read_user_types`           | User Types and Onboarding Questions                                                                                   |
| `POST /internal/agent/admin-config/document-access`      | `read_document_access`      | Global and per-user-type Document Access/defaults                                                                     |
| `POST /internal/agent/admin-config/onboarding-status`    | `read_onboarding_status`    | Instance initiation and guided onboarding bootstrap status                                                            |

All seven endpoints use the same request body:

```json
{
  "actor": {
    "id": 1,
    "type": "admin",
    "approved": true,
    "pubkey": "<hex-pubkey>"
  }
}
```

No Admin Config Tool request accepts a free-form user query, `mode`,
`requested_scopes`, raw prompt text, or secret value reveal flag. If a future Tool
needs filters, add them as explicit typed fields on that Tool's request contract.

Common response shape:

```json
{
  "version": 1,
  "tool": "read_deployment_readiness",
  "data": {},
  "warnings": [],
  "generated_at": "2026-06-15T12:00:00+00:00",
  "secret_policy": {
    "mode": "masked"
  }
}
```

Field contract:

- `version`: required integer response contract version; currently constant `1`
  for all Admin Config Tool responses. Future incompatible response-shape
  changes must increment this value and update Sage and Python together.
- `tool`: the Sage Tool contract the response satisfies
- `data`: structured Tool-specific data, not prompt-ready prose
- `warnings`: non-fatal read failures, reductions, or stale-data notes
- `generated_at`: ISO-8601 UTC timestamp for the read
- `secret_policy.mode`: `masked` for ordinary reads and writes;
  `explicit_secret` only for the explicit Admin-only `read_deployment_secret`
  Tool

Tool-specific `data` contracts:

- `read_instance_settings` returns:
  - `settings`: object containing the current non-secret Instance Settings values
    keyed by config name
  - `explicitly_set_keys`: array of setting keys intentionally set by the
    operator, when available
  - `fields`: array of field descriptors with `key`, `label`, `value`,
    `source`, `editable`, and optional `supported_values`
- `read_deployment_settings` returns:
  - `settings`: object keyed by Deployment Setting name. Each value includes
    `value` for non-secret settings, `configured` for secret settings,
    `secret`, `requires_restart`, `source`, and optional `generated_value`
  - `categories`: object grouping setting keys by deployment category, such as
    Model Provider, Email, Runtime, CORS, rate limiting, and storage
  - `warnings`: non-fatal read or validation notes specific to deployment
    settings
- `read_deployment_readiness` returns:
  - `status`: `ready`, `warnings`, or `blocked`
  - `summary`: object with integer `blockers`, `warnings`, `ready`, and `total`
    counts
  - `items`: array of readiness items. Each item includes `key`, `label`,
    `source`, `severity`, `status`, `summary`, `next_action`, and
    `conversation_blocking`
- `read_agent_settings` returns:
  - `global`: object containing global Agent Settings, prompt sections,
    parameters, defaults, and trace visibility settings such as
    `admin_trace_visibility` and `user_trace_visibility`
  - `per_user_type`: array of effective override objects keyed by
    `user_type_id`, each with `user_type_name`, `overrides`, and
    `effective_values`
  - `limits`: object describing any fan-out or prompt-budget reductions applied
- `read_user_types` returns:
  - `user_types`: array of user type objects with `id`, `name`, `description`,
    `icon`, `display_order`, and onboarding field definitions
  - `onboarding_questions`: array grouped by user type. Each question includes
    `id`, `user_type_id`, `name`, `label`, `field_type`, `required`,
    `placeholder`, `options`, and `include_in_chat`
  - `limits`: object describing any fan-out reductions applied
- `read_document_access` returns:
  - `global_default_document_ids`: array of Document Library `job_id` values
    active by default when no per-user-type override applies
  - `per_user_type`: array keyed by `user_type_id`, with
    `available_document_ids`, `default_document_ids`, and override metadata
  - `documents`: array of document summaries with `job_id`, `filename`,
    `status`, and access/default flags
- `read_onboarding_status` returns:
  - `initialized`: boolean indicating whether the instance has completed admin
    bootstrap
  - `admin_exists`: boolean indicating whether the singleton admin exists
  - `required_steps`: array of setup step objects with `key`, `label`, `status`,
    `summary`, and `next_action`
  - `guided_questions`: array of remaining guided onboarding question metadata,
    if any

Deployment Setting secret values stay masked (`********` or equivalent). Secret
metadata such as configured/unconfigured status may be returned. Raw secret
values are never returned from the default Admin Config Tool endpoints.

Admin Config writes are direct, product-level Tools. Sage does not stage
proposals or send arbitrary Admin API paths. Each Tool calls one fixed private
endpoint:

- `POST /internal/agent/admin-config/configure-instance`
- `POST /internal/agent/admin-config/update-instance-settings`
- `POST /internal/agent/admin-config/update-deployment-settings`
- `POST /internal/agent/admin-config/update-agent-settings`
- `POST /internal/agent/admin-config/manage-user-types`
- `POST /internal/agent/admin-config/manage-onboarding-questions`
- `POST /internal/agent/admin-config/update-document-access`

The privileged read endpoint is:

- `POST /internal/agent/admin-config/read-deployment-secret`

The authoritative typed request models and common response model live in
`backend/app/internal_agent.py`: `InternalUpdateInstanceSettingsRequest`,
`InternalConfigureInstanceRequest`, `InternalUpdateDeploymentSettingsRequest`,
`InternalUpdateAgentSettingsRequest`, `InternalManageUserTypesRequest`,
`InternalManageOnboardingQuestionsRequest`,
`InternalUpdateDocumentAccessRequest`, `InternalReadDeploymentSecretRequest`,
and `InternalAdminConfigToolResponse`. Sage and Python must update these models
and the matching Sage Tool schema together.

| Tool | Tool-specific request fields after `actor` and `conversation_id` | Validation and authoritative `affected_areas` |
| --- | --- | --- |
| `configure_instance` | typed Instance Settings, up to five typed User Types, up to ten typed Onboarding Questions, behavior rules, forbidden topics | validates the complete guided setup and returns `instance_settings`, `agent_settings`, `user_types`, and `onboarding_questions` |
| `update_instance_settings` | typed `settings` object | validates supported values as one batch and returns `instance_settings` |
| `update_deployment_settings` | string-valued `settings` map; secret inputs are ordinary string values and results are masked | validates allowed keys, value types, URLs, and restart metadata and returns `deployment_settings` |
| `update_agent_settings` | optional `user_type_id`, string-valued `updates`, and `revert_keys` | validates known Agent Settings and inheritance rules and returns `agent_settings` |
| `manage_user_types` | `operation`, optional `user_type_id`, and typed name/description/icon/order fields | validates lifecycle and uniqueness; returns `user_types`, plus cascading `onboarding_questions`, `agent_settings`, and `document_access` for deletion |
| `manage_onboarding_questions` | `operation`, optional `question_id`, and typed question fields | validates field types, encryption/include-in-chat, assignment, and uniqueness and returns `onboarding_questions` |
| `update_document_access` | optional `user_type_id`, typed `updates`, and `revert_job_ids` | validates Documents, scope, and inheritance and returns `document_access` |
| `read_deployment_secret` | secret Deployment Setting `key` | accepts only an explicitly requested readable secret, returns `secret_policy.mode: explicit_secret`, and has no `affected_areas` because it does not mutate configuration |

All write responses use `InternalAdminConfigToolResponse.data` with
`outcome`, `validation`, authoritative normalized saved state,
`changed_names`, and `affected_areas`; Deployment Settings also return
`restart_required` and `restart_required_keys`. Tool-specific identifiers,
reverted keys, and deleted IDs are included where relevant.

Every direct write and explicit secret-read request carries the authenticated
Admin actor and the real Sage Conversation identifier. Ordinary read requests
need only the actor envelope. Write endpoints validate the complete Tool call
and commit all changes in that call or none. They return normalized saved
state, changed configuration names, validation outcome, affected areas, and
restart status where relevant.

Write results mask secret values. Ordinary reads expose only secret status.
`read-deployment-secret` may return a stored secret only for an explicit Admin
request; Activity, Conversation Trace, and Audit Log metadata must still omit
the value.

The Enclave Control Plane records `sage_conversation` provenance and the
Conversation identifier in the Audit Log without copying Conversation Content.
The browser may refetch returned affected areas, but it never executes or
repeats the write.

### Removed Admin Context Endpoints

`POST /internal/agent/scoped-config-context` and public
`POST /admin/scoped-config-context` are removed from the active internal agent
contract. This removal applies to Admin Config scoped prompt-context generation,
not to active non-admin-config context endpoints such as user profile context.
They must not be preserved as compatibility shims or fallback Admin Config
context sources.

## Removed Endpoints

Obsolete compatibility-only endpoints should be absent from Python. Do not add new
Sage dependencies on removed endpoints without promoting them here as active
contract endpoints.

## Change Rules

- Keep endpoint paths stable for this prototype branch.
- Additive response fields are allowed if Sage ignores unknown fields.
- Removing or renaming fields requires coordinated Sage and Python changes in
  the same pull request.
- Auth semantics and the internal token header name are not versioned; changing
  either requires a new contract version.
