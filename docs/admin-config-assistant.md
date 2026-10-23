# Admin Configuration Assistant

This document describes the direct Admin Config workflow shared by:

- the admin-only configuration assistant sidebar,
- authenticated Admin chat at `/chat`, and
- guided Admin onboarding.

## Product Behavior

Sage owns supported Admin Config reads and writes. Before changing configuration, Sage should briefly summarize one coherent intended change and ask the Admin for natural-language Conversational Confirmation. After confirmation, Sage calls the needed direct write Tools and reports their authoritative results naturally.

Confirmation is trusted model behavior from the basic system prompt. There is no proposal Tool, executable change-set payload, Apply card, pending-change state, confirmation token, intent classifier, or browser-side write.

The dedicated Admin Configuration Assistant always has the Config Tool Set enabled. General Admin chat receives Config Tools only while its Config control is enabled. User Conversations never receive Admin Config Tools. Messages such as “yes,” “do it,” and “apply them” are ordinary Conversation turns sent to Sage.

## Authority Boundary

Direct write authority covers:

- Instance Settings
- Deployment Settings
- Agent Settings
- User Types
- Onboarding Questions
- Document Access defaults

It does not cover destructive User or Document operations, runtime restarts, Curated Resource management, or arbitrary Admin API calls.

Sage may save a Deployment Setting that requires a restart, but it reports the requirement instead of restarting services.

## Tool Surface

Read Tools:

- `read_instance_settings`
- `read_admin_setup_summary`
- `read_deployment_settings`
- `read_deployment_readiness`
- `read_agent_settings`
- `read_user_types`
- `read_document_access`
- `read_onboarding_status`

Direct-write Tools:

- `configure_instance`
- `update_instance_settings`
- `update_deployment_settings`
- `update_agent_settings`
- `manage_user_types`
- `manage_onboarding_questions`
- `update_document_access`

Read Tools answer scoped questions. Without arguments, a read returns a compact
overview. `user_type` (name or id), `document`, `category`, and `keys` narrow it
to the slice being asked about and resolve inherited values. Lists are paged
through `offset`. Every result fits the per-result Tool budget, so asking about
one User Type, Document, or setting works at any instance size. The
[Internal Agent Contract](internal-agent-contract.md#model-facing-read-views)
defines each view.

Privileged read Tool:

- `read_deployment_secret` for an explicit Admin request

`configure_instance` applies one coherent guided first-time setup. Sage writes its behavior rules and forbidden topics to its own Agent Settings first. The control plane then applies Instance Settings, User Types, and Onboarding Questions; if it rejects the setup, Sage restores the previous rules. An empty rule or topic list leaves the current list unchanged. The smaller product-area Tools handle later edits.

Each Tool has product-level arguments and a fixed private Enclave Control Plane endpoint, except Agent Settings. Sage writes those in its own store and records them in the control-plane Audit Log; if the record fails, it rolls them back ([ADR-0036](adr/0036-sage-applies-agent-settings-writes-in-its-own-store.md)). The model cannot choose a request path or submit raw Admin API JSON. Each write Tool call validates before writing and applies all of its changes or none. Separate write Tool calls are not one transaction: an earlier success remains applied if a later call fails, and Sage should report that partial outcome honestly.

Structured Tool arguments stay native end to end: settings are objects and collections such as User Types, Onboarding Questions, rules, and Document updates are arrays. They are never passed as JSON encoded inside strings. When the Enclave Control Plane rejects a call, Sage receives its structured validation field locations and messages so it can make a focused correction.

## Runtime Flow

1. The browser sends the Admin message, selected Tool Set IDs, session identifier, and optional Knowledge constraints to the Gateway. Guided setup also sends `conversation_surface: "admin-onboarding"`; other Conversation surfaces omit it.
2. The Gateway routes the public Conversation request to Sage. Python exposes no public `/llm/chat` handler.
3. Sage verifies Admin authority, loads Session Memory, and exposes the authorized Tool contracts. For the guided Admin surface with Config enabled, it adds only the exact numbered-answer mapping, a conversational confirmation reminder, and guidance to use the atomic `configure_instance` Tool after confirmation.
4. The model chooses read or direct-write Tools.
5. Sage calls fixed `/internal/agent/admin-config/*` contracts with the real Conversation identifier. For Agent Settings, it writes its own store and calls `record-agent-settings-change` for audit provenance.
6. The Enclave Control Plane validates and atomically applies each write, records audit provenance, and returns authoritative normalized state.
7. Sage emits secret-safe Activity and Trace metadata, then answers naturally.
8. Sage returns `admin_config_affected_areas`; the browser refetches matching settings views and never repeats the write.

There is no admin configuration `tool_context` prefetch and no `client_executed_tools` compatibility path.

## Configuration Semantics

Theme requests mean Instance visual identity settings, including `default_theme`, `primary_color`, `chat_bubble_style`, `chat_bubble_shadow`, `surface_style`, `status_icon_set`, and `typography_preset`. They are not frontend CSS token or source-code edits.

User Type and Onboarding Question Tools preserve the existing domain validation, stable identity, ordering, encryption, include-in-chat, and assignment rules.

Document Access Tools update global, batch, or per-User-Type defaults and may remove an override to restore inheritance. They do not ingest, replace, or delete Documents.

Deployment Settings return restart-required state when relevant. Saving desired state does not mutate a running service.

## Secrets

The Admin private key remains in the NIP-07 signer and is never exposed to Sage.

Ordinary Admin Config reads return non-secret values and secret status metadata. Sage may write a secret explicitly supplied by the Admin. `read_deployment_secret` may retrieve a stored secret only when the Admin explicitly asks to see it.

An explicitly requested secret may appear in the encrypted Conversation answer and persist as Conversation Content. Activity, Conversation Trace, and Audit Log metadata always omit secret values. Write results mask secret values and Sage should not echo them automatically.

The existing “Share secret env vars” UI remains an explicit per-session mechanism for other Config-context behavior. It is not required for the direct secret-read Tool and does not create browser-side write authority.

## Audit and Activity

Every Sage write records:

- the Admin actor,
- action source `sage_conversation`,
- the real Conversation identifier,
- the Tool operation,
- changed configuration names, and
- success or failure outcome.

Audit rows do not copy Conversation Content or secret values. Historical rows with no reliable source remain `unknown`.

Activity shows the Tool name, outcome, and changed names. Trace arguments and results are sanitized and never duplicate secret values.

## Resilience

Session Memory compaction, prompt budgeting, Tool-output limits, provider-error classification, and Start New Conversation recovery continue to work on both Admin surfaces.

A Tool argument correction for the same confirmed intent may be retried without asking again. If the intended scope changes materially, Sage should ask for fresh confirmation. Both decisions remain model judgment, not runtime-enforced state.

Known provider failures after answer text begins are surfaced without a wasteful non-streaming retry. A direct Tool result is authoritative; the runtime does not replace Sage’s answer with a hardcoded success sentence.

## Quick Verification

The hard cut should leave no active proposal or browser Apply architecture:

```bash
rg -n "admin_change_set|propose_config_change_set|propose_admin_config_bootstrap|AdminChangeConfirmationState|adminApplyIntent" \
  frontend/src runtime/sage/crates/sage-core/src
```

Expected matches are limited to explicit migration strings or negative regression assertions.

Focused deterministic verification:

```bash
python3.11 -m unittest \
  backend.tests.test_admin_config_tool_contract \
  backend.tests.test_config_audit_provenance

cd runtime/sage
LIBRARY_PATH=/opt/homebrew/opt/libpq/lib cargo test -p sage-core --lib

cd ../../frontend
npm test -- --run src/components/admin/AdminConfigAssistant.test.tsx src/pages/ChatPage.test.tsx
```

The final live smoke uses the configured real model and Apple Containers on this developer machine. It should prove a two-turn confirmation Conversation: no first-turn mutation, a direct Tool call after confirmation, persisted state, correct audit provenance, secret-safe Activity/Trace, affected-area refresh metadata, and no `admin_change_set` or Apply metadata.
