# SQL Safety Posture

The product-supported SQL surface is read-only admin inspection.

## Supported Paths

- `POST /admin/db/query` accepts `SELECT` statements only.
- `GET /admin/db/tables` and `GET /admin/db/tables/{table_name}` expose only the shared allowlisted admin inspection tables.
- The Sage `db-query` Tool Set exposes the executable tool for approved Admin turns when the Tool Set is enabled. Sage may translate natural-language database questions into a single read-only SQLite `SELECT`; executed SQL still uses the same shared allowlist and rejects mutation keywords before execution.
- Browser-backed `read_users` and `search_users` supplement live Admin Database turns for encrypted identity/profile interpretation. They use a separate [projected-read contract](contracts/admin-user-lookup.md) and do not change SQL validation or redaction.

Direct database mutation through the Database Explorer is not a supported product path. Admin data changes should use audited product flows such as User Type administration, User approval, Document governance, retention, and Data Deletion workflows.

## Guardrails

- Mutation keywords such as `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `ATTACH`, `DETACH`, and `PRAGMA` are rejected.
- Referenced tables are checked against `backend/app/sql_safety.py`.
- Table browsing uses a fixed shared allowlist before any table-name interpolation.
- Raw table names are not accepted outside allowlisted admin inspection tables.

## Audit Log Boundary

Read-only inspection is an admin-only operational capability. Mutating product workflows create Audit Log evidence; direct SQL mutation is intentionally blocked rather than audited as a supported workflow.
