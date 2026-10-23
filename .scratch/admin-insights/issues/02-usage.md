## Parent

Admin insights specification (link added in release PR).

## What to build

Let an Admin ask about retained Sage usage by user, day or model in an explicit time range/timezone using `read_usage`. Report exact message counts and coverage for observed provider tokens; never invent historical costs or model attribution.

## Acceptance criteria

- [ ] Admin Database-only tool; users and impersonation denied.
- [ ] User/day/model grouping, deterministic bounded pages, source range and explicit coverage.
- [ ] Admin test sessions excluded by default; deleted sessions/users handled consistently with source retention.
- [ ] Missing token usage is unknown, retries/observations not double-counted, unknown model is explicit; dollars unavailable without versioned prices.
- [ ] Fixture-backed tests verify time boundaries/timezones, grouping, paging, exclusions, sparse token observations and permissions.

## Blocked by

None. Shared registration edits are integrated by the parent.
