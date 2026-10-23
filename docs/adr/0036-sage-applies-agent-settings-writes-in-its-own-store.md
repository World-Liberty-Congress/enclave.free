# Sage applies Agent Settings writes in its own store

Status: Accepted. This refines ADR-0028 for Agent Settings only. Every other direct Admin Config write still goes through its own Enclave Control Plane endpoint.

Agent Settings belong to Sage. Conversations, the Admin AI Config page, and `read_agent_settings` all read Sage's store. `update_agent_settings` and the guided behavior rules and forbidden topics in `configure_instance` used to call control-plane endpoints instead. Those endpoints wrote a separate Python copy of `ai_config` that nothing reads at conversation time. The Tool reported success with saved values read back from that copy, so an Admin could be told a change was made when Sage never used it.

## Decision

- Sage validates and applies Agent Settings writes in its own store. It uses the same key lookup and value validation as the Admin AI Config page. One Tool call is one transaction.
- The Enclave Control Plane still owns the Audit Log. After writing, Sage records each changed setting through `POST /internal/agent/admin-config/record-agent-settings-change`, with the actor, `sage_conversation` action source, and Conversation identifier. If that record fails, Sage restores the previous values and reports that nothing changed. If the actor is no longer an approved Admin, it reports `unauthorized` instead. An Admin Conversation therefore never changes Agent Settings without audit provenance.
- A rollback restores only values that still hold what this change wrote. A newer edit made in between, for example from the Admin AI Config page, is never overwritten. If anything could not be restored, the Tool says so and asks the Admin to read the settings before retrying.
- `configure_instance` applies the guided Agent Settings before calling the control plane for Instance Settings, User Types, and Onboarding Questions. If the control plane rejects the setup, Sage restores the previous Agent Settings, so the whole setup is unapplied. If only the final audit record fails, Sage restores the Agent Settings and reports `partially_applied`. The committed control-plane areas still refresh in the client. An empty behavior-rule or forbidden-topic list leaves that setting unchanged, so re-running setup cannot silently clear an operator's rules.
- Guided behavior rules keep Sage's product-required rules. Sage restores those at startup anyway; keeping them at write time avoids a window where required Admin Config behavior is missing.
- The control-plane `update-agent-settings` endpoint is removed, and `configure-instance` no longer accepts Agent Settings. Leaving them in place would keep a second write path to a store nothing reads.

## Consequences

Agent-made Agent Settings changes take effect in Conversations immediately and appear on the Admin AI Config page. The Audit Log keeps the same per-setting entries, old and new values, and Tool outcome rows as before. The Tool outcome row now records `"store": "sage"`, and guided setup produces a second outcome row for its Agent Settings part.

The write and its audit record span two stores, so they are not one transaction. Restoring on audit failure closes that gap for Sage-originated writes, except when the restore itself fails or a newer edit supersedes it. In those cases the Tool reports what happened and asks the Admin to read the settings before retrying. Admin authorization is checked when the change is recorded, so an Admin whose access is revoked mid-Conversation has the write rolled back.

Admin AI Config page writes are still unaudited, as before this decision. The new audit endpoint is the place to add that later.

The Python `ai_config` tables, their seed data, and the legacy `/admin/ai-config` handlers that the Gateway routes to Sage remain as Prototype Compatibility Debt (ADR-0017). Nothing in a Conversation reads them.
