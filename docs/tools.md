# Agent Runtime Tool Semantics

This document describes the accepted Tool behavior for the Sage hard-cut prototype after [ADR-0023](adr/0023-unified-model-driven-tool-loop.md), [ADR-0029](adr/0029-native-tool-calling-with-one-tool-round.md), and [ADR-0030](adr/0030-bounded-native-tool-loop.md).

## Core Rule

Sage owns a bounded native model-driven Tool loop for Conversations. The browser sends the user message, selected Tool Sets, and optional Tool constraints. Sage expands those selected Tool Sets into concrete native Tool contracts. The model may answer directly or select a Tool batch. Sage executes an authorized batch, injects all successful, failed, rejected, guarded, or timed-out Tool results, and returns the same enabled Tool contracts to the same model. The model may continue within a six-batch safety ceiling. If the model selects a seventh batch, Sage rejects it before execution, returns a correlated `tool_budget_exhausted` result for every unexecuted call, and makes one final request to the same model with Tools disabled. Sage emits Activity and Conversation Trace metadata throughout the loop.

Python no longer owns or exposes public Agent Runtime routes. Direct Python calls are unsupported because public Agent Runtime routes are absent from the Enclave Control Plane; public callers use the Gateway path so nginx dispatches requests to Sage. Python remains the Enclave Control Plane behind private/internal contracts for authorized facts and actions such as safe database reads, document search, user profile context, lifecycle operations, and product-level admin configuration reads and writes.

## Public Route Shape

The product mental model is one Conversation runtime. Existing public route names may remain while the hard cut is implemented:

| Gateway route      | Role                                                  |
| ------------------ | ----------------------------------------------------- |
| `/llm/chat`        | non-streaming Conversation transport                  |
| `/llm/chat/stream` | streaming Conversation transport                      |
| `/query`           | stateful Conversation API compatibility shape         |
| `/query/session/*` | Conversation session inspection, rename, and deletion |

Route names do not define separate tool systems. Document-grounded chat is Conversation plus the `knowledge-search` Tool Set. Curated referral/resource lookup is Conversation plus the `curated-resources` Tool Set. Admin chat is Conversation plus admin-authorized Tool Sets. Guided onboarding is Conversation plus guided UI prompts and the same `admin-config` Tool Set.

## Browser To Sage Request Contract

The browser sends Tool Set IDs to Sage in the Conversation request. Admin Conversations send the Admin's visible selections and normal Admin Conversation surfaces start with no Tool Sets selected. User Conversations use the operator-configured session defaults returned by Sage-owned `/session-defaults`; the default user composer does not expose Tool Set controls or a Knowledge document selector. Sage is responsible for expanding those IDs into concrete Tool contracts and enforcing the effective non-admin default policy server-side.

For `/llm/chat` and `/llm/chat/stream`, the request shape is:

```json
{
  "message": "What does the handbook say?",
  "session_id": "optional-session-id",
  "conversation_surface": "admin-onboarding",
  "tools": ["knowledge-search", "curated-resources", "web-search"],
  "job_ids": ["doc-handbook", "doc-faq"]
}
```

- `tools` is a list of Tool Set IDs selected by an Admin or resolved by Sage from User Conversation defaults.
- `conversation_surface` is optional. The browser sends `admin-onboarding` only for the guided Admin setup assistant. Sage uses it only for an authenticated Admin with the `admin-config` Tool Set enabled, adding the lightweight numbered-answer mapping, conversational confirmation, and atomic `configure_instance` guidance. Ordinary Admin and User Conversations omit it.
- Sage drops or rejects Tool Sets the actor is not authorized to use. For non-admin users, Sage ignores the client-submitted `tools` list for effective resolution and computes the Tool Set list from server-side `/session-defaults`, including the empty or omitted case where configured defaults still apply.
- `job_ids` is an optional Knowledge Search constraint: it is a list of selected Document Library `job_id` values, not an arbitrary prompt blob.
- Additional Knowledge filters must be added as explicit request fields before the browser can send them.
- Sage owns Conversation history. Model context and the Admin review record come from Sage's stored messages for `session_id`; Sage ignores a `conversation_history` field sent by older browsers (see ADR-0035).
- `client_decrypted_context` is optional **Admin Signer-Decrypted Context**, shaped like `{ "source": "admin-signer-user-roster", "users": [...] }`. The browser may attach it only for authenticated Admin turns with the `db-query` Tool Set enabled, and Sage treats it as signer-delegated plaintext for the current encrypted inference turn rather than Tool output or trace metadata.

## Tool Sets

Tool Sets are conversation controls and permission bundles. They are visible controls for Admin Conversations only, and normal Admin Conversations do not enable any Tool Set by default. User Conversations always consume the server-resolved defaults without showing Tool controls by default.

| Tool Set ID         | Access                                        | Exposes                                                       |
| ------------------- | --------------------------------------------- | ------------------------------------------------------------- |
| `knowledge-search`  | users and admins, filtered by Document Access | `knowledge_search` over the Document Library                  |
| `curated-resources` | users and admins                              | `find_resources` over the admin-curated Resource Directory    |
| `web-search`        | users and admins when enabled                 | `web_search` through the configured SearXNG service           |
| `admin-config`      | admins only                                   | product-level admin configuration read and direct-write Tools |
| `db-query`          | admins only                                   | read-only database inspection Tools                           |

Enabled does not mean forced. Enabled means the model may call the Tool when it improves the answer. Tool descriptions and Agent Settings guide that choice; Sage does not use a hidden intent classifier to force or reject an otherwise valid model-selected Tool batch.

## Knowledge Search

`knowledge-search` is a first-class visible Tool Set, not a hidden retrieval mode. Selected Documents are constraints on `knowledge_search`, not silent Required Context for ordinary chat.

Its Tool description is a concise capability contract: it searches uploaded Documents, Documents may have different languages or titles than the user's question, and the model may select multiple Knowledge Search calls in a Tool batch when alternate queries would help. The contract does not contain WLC-specific vocabulary or deterministic query rewriting.

Sage passes allowed document constraints to the Knowledge Tool. Python enforces Document Access and hydrates retrieved chunks from product-owned storage after vector search. Retrieved chunks enter the Conversation as Tool results and Activity/Trace metadata under the transparent trace posture in ADR-0024.

Required Context remains a separate product-policy term for future mandatory context that must be included outside ordinary model discretion. It is not the default document-chat path.

## Curated Resources

`curated-resources` is a first-class visible Tool Set for the admin-curated Resource Directory. It is separate from `knowledge-search`: Resources are structured, priority referrals stored in SQLite by admins; Knowledge is uploaded document retrieval through embeddings and document access policy.

Sage exposes this Tool Set as `find_resources`. The Tool calls Python's private `/internal/agent/resources/search` contract and returns generic Admin-curated people, organizations, products, services, methods, references, and other Resources. Each result may include `kind`, `tags`, exact `pointers`, `regions`, `languages`, and `provenance`. The model-facing Tool accepts optional `exact_resource`, `region`, `language`, and continuation `offset` arguments and receives the best-ranked page of ten results plus `total_count`, `returned_count`, `has_more`, and `next_offset` metadata. Discovery and referrals use region and language when applicable. `exact_resource` is reserved for a name or exact contact or pointer when the User is asking for that Resource itself, not a place or subject mentioned as context. Python's private directory endpoint retains optional `query`, `kind`, and `tags` for non-model callers, but those guess-prone facets are not part of the native Conversation Tool schema.

The Resource Directory—not Sage orchestration—owns generic search quality. Exact normalized Resource IDs, names, and pointer values rank first. Name acronyms and partial name or pointer values rank next, followed by generic token overlap across identity, kind, tags, and description. Email-, phone-, and URL-shaped queries do not use the broader token fallback. Existing status, regional, language, verification, display-order, and name rules remain in force. Sage gives the model that first page plus structured counts so the model knows when additional matches exist. Sage does not detect contact intent, force a lookup, automatically fetch another page, police completeness wording, or expose Tool and pagination mechanics to the user.

## Admin Config

`admin-config` is an admin-only Tool Set. It exposes concrete product-level Tools rather than a prompt-ready scoped blob or a generic request dispatcher. Read Tools include:

- `read_instance_settings`
- `read_admin_setup_summary`
- `read_deployment_settings`
- `read_deployment_readiness`
- `read_agent_settings`
- `read_user_types`
- `read_document_access`
- `read_onboarding_status`

Direct-write Tools are:

- `configure_instance`
- `update_instance_settings`
- `update_deployment_settings`
- `update_agent_settings`
- `manage_user_types`
- `manage_onboarding_questions`
- `update_document_access`

The privileged read Tool is:

- `read_deployment_secret` for an explicit Admin secret-read request

Reads happen within Admin Conversation authority. Broad setup, status, and readiness questions should use `read_admin_setup_summary` first because it compacts readiness, missing setup, and next actions. Before a write, Sage should briefly summarize one coherent intended change and ask once for natural Conversational Confirmation. After confirmation, Sage chooses and calls the needed direct Tools. This is prompt-guided model behavior, not a confirmation token, proposal contract, Apply card, or runtime intent classifier.

Every direct Tool maps to a fixed private Enclave Control Plane endpoint with purpose-built arguments. The model cannot choose an endpoint path or submit raw request JSON. Each Tool call validates and commits atomically; separate Tool calls are not one transaction. Tool results return authoritative normalized state, changed names, validation status, affected areas, and restart requirements where relevant so Sage can report the real outcome naturally.

Tool arguments use native JSON values throughout the Sage runtime. Structured settings are objects, collections are arrays, and scalar fields are strings, numbers, or booleans; callers do not JSON-encode objects or arrays into strings. Backend validation details, including structured HTTP 422 field locations and messages, are returned to Sage so it can report the rejected call accurately. The model may use the bounded loop to correct a call from authoritative validation feedback; Sage does not rewrite the arguments itself.

`configure_instance` is the high-level atomic Tool for guided first-time setup. The smaller area Tools handle later edits to Instance Settings, Deployment Settings, Agent Settings, User Types, Onboarding Questions, and Document Access defaults. Destructive User or Document operations, service restarts, and Curated Resource management are outside this authority.

Admin Config reads return non-secret values and secret status metadata by default. Sage may write a secret explicitly supplied by the Admin. `read_deployment_secret` may retrieve a stored secret only for an explicit Admin request. Activity, Conversation Trace, and Audit Log metadata omit secret values even when the natural encrypted Conversation answer intentionally contains one.

Theme requests in Admin Conversations mean Instance visual identity settings, such as default theme and `primary_color`. They should use the Instance Settings Tool, not frontend CSS or source-code edits.

## Database

`db-query` is an admin-only Tool Set for read-only inspection. When an approved Admin enables it, Sage exposes the executable Database Query Tool to the model for the turn. Sage may translate natural-language database questions into a single read-only SQLite `SELECT` when live database facts would improve the answer. Python remains the safe SQL executor and must enforce read-only validation, blocked keywords, allowed tables, authorization, truncation, and redaction. Direct database mutation is not a supported product path.

Admin Database turns may also include **Admin Signer-Decrypted Context** built by the browser from admin-authorized encrypted User values. This context lets Sage combine safe query results with plaintext identity/profile values under encrypted inference. It must not be accepted for non-Admin turns, non-Database turns, Python database execution, Activity, or Conversation Trace.

## Web Search

`web-search` uses the internal SearXNG service at `http://searxng:8080/search?format=json`. It is intended for current or external information and should not replace Enclave Document Library Retrieval.

## Frontend Duties

The frontend chooses visible Admin Tool Sets and Tool constraints. For non-admin User Conversations, it consumes `/session-defaults` and sends the configured default Tool Set IDs and Knowledge Source scope without showing Tool controls by default. It must not prefetch admin configuration context for chat, run hidden document retrieval outside configured defaults, or send `client_executed_tools` as a compatibility path. It may build Admin Signer-Decrypted Context only after an authenticated Admin submits a Database-enabled turn.

Normal Admin Conversation composers should make Knowledge, Resources, Web, Config, and Database explicit opt-in controls with none selected by default. Admin configuration conversations are the exception: `admin-config` is default-active and visible there, while all other Tool Sets remain opt-in. Knowledge document scope belongs under the Knowledge Tool Set control. User composers show no Tool Set controls by default; Config and Database must only render for server-validated admins.

## Sage Duties

The model selects native Tool calls from concise Tool descriptions, not through a separate typed planner or deterministic intent classifier. Deterministic Sage code still decides:

- which Tool Sets and Tools are available for the actor
- the six-batch loop bound, with at most eight calls in each batch; a seventh selected batch is rejected before execution, receives correlated bounded failures, and is followed by one final same-model request with Tools disabled
- a finite timeout for every Tool attempt, with retries enabled only for eligible read-only calls
- Tool-result context budgets of 4,000 characters per result and 12,000 characters across the batch
- Tool result injection
- Activity, Trace Delta, and Conversation Trace assembly
- authorization, argument validation, protocol validity, and secret-safe trace assembly

Each logical model request has one generic same-model recovery budget of three identical attempts. Alongside eligible transport, upstream, and protocol failures, complete provider silence for 30 seconds is a Pre-Response Provider Stall: Sage abandons that attempt and may retry the identical request within the shared ceiling. Any answer, reasoning, Tool-call, or other provider stream event makes that attempt ineligible for another model-request retry. Recovery cannot replay an executed Tool batch, and the existing 180-second attempt timeout remains for requests that have begun responding. The 30-second threshold and three-attempt ceiling are internal runtime policy rather than a new configuration surface.

When the provider attaches opaque Provider Continuity State to an assistant Tool-call message, Sage returns it unchanged to the same model with the correlated Tool results during the current bounded native loop. Sage does not interpret, stream, log, persist, export, or carry that state into another Conversation turn. It does not alter thinking mode, tune reasoning effort, add model-specific reasoning prompts, or synthesize state the provider did not supply. A fixed aggregate protocol-size bound rejects oversized continuity state without inspecting, logging, or independently truncating it.

A safe-to-retry Tool execution may retry once for a transient transport failure. Empty or weak results, invalid arguments, and state-changing calls without idempotency are not automatic retry signals, though the model may choose a different authorized call within the bounded native loop.

Final model prose streams directly after protocol validation. Sage does not scan, quarantine, rewrite, or replace prose based on process narration, Tool-name syntax, repetition, or completeness claims. When separate native model requests both emit prose and neither side supplies whitespace, Conversation Streaming Transport inserts a paragraph separator at that model-turn boundary; provider chunks within one request remain unchanged. Real credential and secret protections remain enforced at their authority and trace boundaries.

## Native Round Observability

Tool Selection Observations contain only enabled Tool names, selected Tool names, selection count, step, attempt, and outcome. Every selected call emits attempted evidence and one terminal `succeeded`, `failed`, `rejected`, `guarded`, or `timed_out` outcome. These observations do not copy prompts, Tool arguments or results, contact values, credentials, secrets, or reasoning into operational logs. Hidden provider reasoning may establish the content-free first-event boundary and may be preserved as ephemeral Provider Continuity State, but it is not published through Activity, Conversation Trace, persistence, exports, or logs.

Timing records only stages Sage can measure: each model request, combined provider first-event wait, each Tool execution, Resource Directory or Retrieval work, retry delay when one occurs, and total turn duration. Provider first-event wait begins at request start and can include transport and provider processing; it is not labeled as cluster scheduling, queue time, or inference-only latency. A Pre-Response Provider Stall records its threshold, abandoned attempt, retry, and outcome without Conversation Content.

Where supported, each streaming model request asks the provider for aggregate usage. A provider that explicitly rejects the optional usage request extension before inference is retried once without that extension, and the adapter remembers not to request it again. Prompt, completion, total, cached, and reasoning counts are recorded only when the provider returns valid numeric values; malformed or absent fields remain absent and never fail the answer. These Model Usage Observations attach to the existing per-request timing trace, follow the Conversation Trace retention and deletion lifecycle, remain outside normal answer content, and do not create a separate billing or analytics subsystem. This operational Conversation metadata is available to Activity and Conversation Trace and is not Audit Log evidence.

For Admin Config, Sage also carries the real Conversation identifier to the Enclave Control Plane and returns affected-area refresh hints. The browser refetches those areas after success; it never repeats the write.

## Python Duties

Python serves authorized Enclave Control Plane facts/actions through private `/internal/agent/*` contracts protected by `INTERNAL_AGENT_TOKEN`. It enforces data ownership, authorization, redaction, read-only validation, and lifecycle boundaries for the data it owns. It must not classify a Conversation turn into a scoped prompt context before the model sees available Tools.
