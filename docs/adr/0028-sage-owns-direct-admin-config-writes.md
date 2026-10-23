# Sage Owns Direct Admin Config Writes

Refined by [ADR-0036](0036-sage-applies-agent-settings-writes-in-its-own-store.md): Sage writes Agent Settings in its own store and records them in the control-plane Audit Log.

Sage will receive direct Admin Config write Tools and may apply supported configuration after asking the Admin for natural-language Conversational Confirmation. Confirmation is trusted model behavior guided by a basic system prompt, not a runtime gate: Admin Config writes will not require proposal Tools, Executable Change Sets, Apply cards, confirmation tokens, intent classifiers, or forced Tool calls. This deliberately favors a simple, agent-owned Tool model over deterministic confirmation enforcement; the Tool boundary still enforces Admin authentication, supported operations, argument validation, secret handling, and Audit Log requirements.

Risk acceptance: the product owner explicitly accepts that Conversational Confirmation is advisory behavior rather than an independently enforceable authorization artifact. Compensating controls are approved-Admin authentication, fixed product-level Tool contracts, allowlisted operations and fields, complete-call validation, atomic writes, authoritative results, affected-area refreshes, and tamper-evident actor/action-source/Conversation audit provenance. These controls constrain what Sage may do and make outcomes reviewable; they intentionally do not prove that a confirmation utterance occurred.

This authority covers Instance Settings, Agent Settings, Deployment Settings, User Types, Onboarding Questions, and Document Access defaults. Destructive User and Document operations remain outside this direct-write boundary.

Curated Resource Directory entries and help-type management also remain outside this boundary because they are managed content rather than Admin Config. Their existing admin UI remains available; a future agent-owned resource Tool requires a separate design.

Configuration-definition deletion, such as removing a User Type or Onboarding Question, remains inside the direct-write boundary after Conversational Confirmation and normal control-plane validation.

Sage may save a Deployment Setting that requires restart and must report that status clearly, but restarting runtime services is a separate deployment operation and is not included in this authority.

The direct write surface should be a small set of product-level Tools grouped by configuration area, such as Instance Settings, Agent Settings, and Deployment Settings. A giant raw request/JSON Tool and one-Tool-per-setting designs are both rejected as unnecessary contract clutter.

Each product-level Tool maps to its own private Enclave Control Plane endpoint with purpose-built arguments and validation. A generic endpoint that accepts arbitrary paths and JSON is not part of the design.

Guided first-time setup uses one high-level direct `configure_instance` Tool for the confirmed setup. Smaller product-area Tools handle later edits.

The complete direct surface is `configure_instance`, `update_instance_settings`, `update_deployment_settings`, `update_agent_settings`, `manage_user_types`, `manage_onboarding_questions`, `update_document_access`, and the explicit Admin-only `read_deployment_secret`. Existing Admin Config read Tools remain available.

These Tools are always available in the dedicated Admin Configuration Assistant. General Admin Conversations receive them only when the Config Tool Set is enabled. Apply-like language is sent to Sage normally rather than intercepted by the client.

The basic system prompt should tell Sage to briefly summarize one coherent Admin Config task, ask once for Conversational Confirmation, and then use every needed direct write Tool without reconfirming each call.

If the intended scope changes materially after confirmation, Sage should ask again. Materiality remains trusted model judgment rather than runtime-enforced confirmation state.

Sage may correct and retry rejected Tool arguments without reconfirming when the intended change is unchanged.

The transition is a hard cut. Proposal Tools, executable change-set transport payloads, Apply cards, pending and superseded proposal state, proposal prose parsing, and client-side apply-language interception should be removed rather than retained as compatibility paths.

Direct write Tools return authoritative success or failure results. Sage should answer naturally from those results under a basic honesty instruction; the runtime should not substitute a hardcoded success sentence or add a response-rewriting layer.

A successful Tool result should include normalized saved values, validation status, restart-required status when relevant, and enough authoritative detail for Sage to answer without a follow-up read call.

After a successful direct write, both Admin Conversation UI surfaces should refetch the affected configuration area so visible settings stay current. The client does not perform or repeat the write; it only refreshes its view from the Enclave Control Plane.

Each direct write Tool call is atomic: it validates the full requested change before writing, then applies all changes in that call or none.

Separate Tool calls do not form a cross-Tool transaction. If a later call fails, earlier successful calls remain applied and Sage should report the partial result and offer to repair the failed part.

Audit Log entries identify the Admin as the authority and Sage/Admin Conversation as the action source. They record changed configuration names and outcomes, never secret values.

Audit Log storage should add an explicit action-source field so `sage_conversation` is distinguishable from ordinary admin UI writes. Historical rows whose source cannot be established should remain explicitly unknown rather than being relabeled.

Sage-originated entries should include the originating Conversation identifier for traceability without copying Conversation Content into the Audit Log.

Sage may write an explicitly supplied secret Deployment Setting. It may repeat the secret value in the assistant answer only when the Admin explicitly asks to see it, and should not echo secrets automatically after writes.

An explicitly requested secret in an assistant answer is normal Conversation Content and may persist in Session Memory and conversation exports. Activity, Conversation Trace, and Audit Log details should avoid duplicating secret values unnecessarily.

The product owner explicitly accepts this encrypted Conversation-content disclosure posture without a separate step-up challenge. Only an authenticated approved Admin Conversation can receive the privileged read Tool; ordinary reads remain masked, the request must name a readable secret and carry the real Conversation identifier, the read is audited without the value, and Activity, Conversation Trace, Tool results shown outside the answer, and Audit Log metadata remain redacted. Conversation exports retain their existing Admin-scoped access boundary.

Admin Config Tool Activity should briefly show the Tool name, outcome, and changed configuration names while always omitting secret values. Sage's natural answer remains the human-facing explanation.

An Admin-only Admin Config read Tool may retrieve a previously stored secret when the Admin explicitly asks Sage to show it. Ordinary configuration reads should continue to return only non-secret values and secret status metadata.

## Considered Options

- Keep the proposal and Apply-card architecture. Rejected because it adds a parallel state machine and brittle contracts around a Tool the agent should naturally own.
- Runtime-enforce proof of conversational confirmation. Rejected because the product deliberately trusts Sage to ask and interpret confirmation without a confirmation token or classifier.
- Extend direct writes to every admin action. Rejected for this decision because destructive User and Document operations need separate treatment.
