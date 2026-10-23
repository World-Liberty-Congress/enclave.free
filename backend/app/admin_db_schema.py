"""Discover the existing admin SQL surface without exposing row values or DDL."""

import database
from sql_safety import ALLOWED_TABLES


TABLE_DESCRIPTIONS = {
    "admins": "The instance's Admin identity, not end users.",
    "instance_settings": "Instance presentation and onboarding settings.",
    "users": "End-user roster; approved=1 means approved, not active.",
    "user_types": "User groups/personas, joined through users.user_type_id.",
    "user_field_definitions": "Onboarding field definitions; NULL user_type_id means a global field.",
    "user_field_values": "User answers to onboarding fields; join field_id to user_field_definitions.id.",
    "session_logs": (
        "Recorded conversations. source='user' is real user activity and holds every retained "
        "message of one conversation; source='admin_test' is Admin testing. subject_user_id joins "
        "users.id. turn_count counts recorded user and assistant messages. Transcript content is encrypted."
    ),
    "session_log_feedback": (
        "Review feedback on one assistant message, named by message_id; "
        "log_id joins session_logs.log_id, not session_logs.id."
    ),
}

TOP_USERS_SQL = """SELECT u.id AS user_id,
       COUNT(l.id) AS recorded_conversations,
       COALESCE(SUM(l.turn_count), 0) AS recorded_turns,
       MAX(l.updated_at) AS last_recorded_activity
FROM users AS u
LEFT JOIN session_logs AS l ON l.subject_user_id = u.id AND l.source = 'user'
GROUP BY u.id
ORDER BY recorded_turns DESC, u.id ASC
LIMIT 5"""

POPULATION_COUNTS_SQL = """SELECT COUNT(DISTINCT u.id) AS total_users,
       COUNT(DISTINCT CASE WHEN l.turn_count > 0 THEN u.id END) AS active_users
FROM users AS u
LEFT JOIN session_logs AS l ON l.subject_user_id = u.id AND l.source = 'user'"""


def describe_admin_database(table_name: str | None = None) -> dict:
    """Use the executor's allowlist and the live schema as the source of truth."""
    if table_name is not None and table_name not in ALLOWED_TABLES:
        raise ValueError("Table is not available to the admin database tool")
    conn = database.get_connection()
    cursor = conn.cursor()
    try:
        tables = []
        for table in sorted(ALLOWED_TABLES):
            # Identifiers come exclusively from our allowlist, never from a caller.
            cursor.execute(f'PRAGMA table_info("{table}")')
            columns = [{"name": row[1], "type": row[2]} for row in cursor.fetchall()]
            if not columns:
                continue
            cursor.execute(f'PRAGMA foreign_key_list("{table}")')
            foreign_keys = [
                {"column": row[3], "table": row[2], "target_column": row[4]}
                for row in cursor.fetchall() if row[2] in ALLOWED_TABLES
            ]
            tables.append({
                "name": table,
                "description": TABLE_DESCRIPTIONS.get(table, ""),
                "columns": columns,
                "foreign_keys": foreign_keys,
            })
    finally:
        cursor.close()

    if table_name is not None:
        for table in tables:
            if table["name"] == table_name:
                return table
        raise ValueError("Table is not present in this database")

    return {
        "dialect": "SQLite",
        "tables": [{"name": table["name"], "description": table["description"]} for table in tables],
        "discovery": "Call db_schema with table set to one listed name for its live columns and foreign keys.",
        "query_rules": [
            "Use one SELECT without a semicolon. Mutations, PRAGMA, sqlite_master, CTEs, "
            "and FROM/JOIN subqueries are not supported by db_query.",
            "Use only the listed tables and columns. Prefer explicit columns, aggregate queries, "
            "and LIMIT for row listings. Apply ORDER BY with a stable tie-break for rankings.",
            "Joins to logs or field values are one-to-many: COUNT(*) counts joined rows, not users. "
            "Use COUNT(DISTINCT users.id) for user counts across joins.",
            "This is the allowed SQLite surface, not every database in the deployment. "
            "Sage's live Postgres messages and web_sessions are not queryable here.",
        ],
        "identity_guidance": (
            "users.name and users.email are legacy compatibility columns and usually NULL. "
            "Names, emails, encrypted profile answers, and transcripts cannot be decrypted by SQL. "
            "Match user IDs to AdminSigner-DecryptedContext when supplied. It may be partial; "
            "otherwise report IDs and say names are unavailable. Never invent identities or "
            "treat the provided decrypted roster subset as the full database."
        ),
        "usage": {
            "metric": "recorded_turns",
            "definition": (
                "For an unspecified 'usage' ranking, use all-time recorded turns (user plus "
                "assistant), state that definition, and include recorded conversation counts. "
                "Exclude source='admin_test'. Include zero-activity users unless asked for active users. "
                "Approval and account creation dates are not usage metrics."
            ),
            "coverage": (
                "Counts cover retained session_logs records; each source='user' log holds every retained "
                "message of its conversation, while deleted conversations or logs are not counted. "
                "A log is one conversation, not one message. "
                "updated_at is the last log update, not an exact per-message timestamp. "
                "Do not divide turn_count by two to claim exact user-message counts. "
                "A time filter on log timestamps selects conversations, not messages in that interval."
            ),
            "top_users_sql": TOP_USERS_SQL,
            "population_counts_sql": POPULATION_COUNTS_SQL,
        },
    }
