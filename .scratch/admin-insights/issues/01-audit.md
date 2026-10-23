## Parent

Admin insights specification (link added in release PR).

## What to build

Let an Admin ask which settings changed and who changed them through a filtered, bounded `read_audit_log` tool. Make Admin AI Config page writes use the same audited mutation semantics as agent writes.

## Acceptance criteria

- [ ] Admin Config-only discovery/execution; ordinary/impersonated users denied.
- [ ] Stable audit IDs, time/actor/source/key filters, deterministic bounded pagination, whole records, safe excerpts and historical-secret masking.
- [ ] UI and agent writes recorded with distinct action sources; existing rollback-on-audit-failure and concurrent-change protection retained.
- [ ] Focused public-interface tests cover paging, masking, denied access, write success/failure and no secret leakage.

## Blocked by

None. Shared registration edits are integrated by the parent.
