from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class DummySentenceTransformer:
    def __init__(self, *_args, **_kwargs) -> None:
        pass


class AdminDbQueryEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "enclave.db"
        self._orig_sqlite_path = os.environ.get("SQLITE_PATH")
        self._orig_secret_key = os.environ.get("SECRET_KEY")
        self._orig_uploads_dir = os.environ.get("UPLOADS_DIR")
        self._orig_internal_agent_token = os.environ.get("INTERNAL_AGENT_TOKEN")
        self._orig_sentence_transformers = sys.modules.get("sentence_transformers")
        sys.modules["sentence_transformers"] = types.SimpleNamespace(
            SentenceTransformer=DummySentenceTransformer
        )
        os.environ["SQLITE_PATH"] = str(self.db_path)
        os.environ["SECRET_KEY"] = "test-secret"
        os.environ["UPLOADS_DIR"] = str(Path(self.tmp.name) / "uploads")
        os.environ["INTERNAL_AGENT_TOKEN"] = "test-internal-token"

        import auth
        import database
        import internal_agent
        import main

        self.auth = importlib.reload(auth)
        self.database = importlib.reload(database)
        self.internal_agent = importlib.reload(internal_agent)
        self.main = importlib.reload(main)
        self.database.init_schema()
        self.main.app.dependency_overrides[self.auth.require_admin] = lambda: {
            "type": "admin",
            "pubkey": "admin-pubkey",
        }
        self.client = TestClient(self.main.app)

    def tearDown(self) -> None:
        self.main.app.dependency_overrides.clear()
        if self.database._connection is not None:
            self.database._connection.close()
            self.database._connection = None
        self._restore_env("SQLITE_PATH", self._orig_sqlite_path)
        self._restore_env("SECRET_KEY", self._orig_secret_key)
        self._restore_env("UPLOADS_DIR", self._orig_uploads_dir)
        self._restore_env("INTERNAL_AGENT_TOKEN", self._orig_internal_agent_token)
        self._restore_sentence_transformers()
        self.tmp.cleanup()

    def _restore_sentence_transformers(self) -> None:
        if self._orig_sentence_transformers is None:
            sys.modules.pop("sentence_transformers", None)
        else:
            sys.modules["sentence_transformers"] = self._orig_sentence_transformers

    @staticmethod
    def _restore_env(name: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def test_admin_query_rejects_disallowed_tables_at_endpoint(self) -> None:
        response = self.client.post(
            "/admin/db/query",
            json={"sql": "SELECT * FROM user_memories"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"], "Query references disallowed table(s): user_memories")

    def test_admin_query_allows_allowlisted_read_only_tables(self) -> None:
        response = self.client.post(
            "/admin/db/query",
            json={"sql": "SELECT id, email FROM users ORDER BY id"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["columns"], ["id", "email"])
        self.assertEqual(body["rows"], [])

    def test_internal_admin_query_rejects_multi_statement_select(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-db-query",
            headers={"X-Internal-Agent-Token": "test-internal-token"},
            json={"sql": "SELECT id, email FROM users; DROP TABLE users"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"], "Only a single SELECT statement is allowed.")

    def test_internal_admin_query_rejects_select_with_forbidden_keyword(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-db-query",
            headers={"X-Internal-Agent-Token": "test-internal-token"},
            json={"sql": "SELECT id, email FROM users UPDATE users"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"], "Query contains forbidden keyword")

    def test_internal_admin_query_allows_single_read_only_select(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-db-query",
            headers={"X-Internal-Agent-Token": "test-internal-token"},
            json={"sql": "SELECT id, email FROM users ORDER BY id"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["columns"], ["id", "email"])
        self.assertEqual(body["rows"], [])

    def test_internal_admin_schema_requires_internal_authentication(self) -> None:
        response = self.client.get("/internal/agent/admin-db-schema")
        self.assertEqual(response.status_code, 403)

    def test_internal_admin_schema_discovers_only_allowed_structure(self) -> None:
        from sql_safety import ALLOWED_TABLES

        user_id = self.database.create_user()
        with self.database.get_cursor() as cursor:
            cursor.execute("UPDATE users SET email = ? WHERE id = ?",
                           ("SCHEMA_MUST_NOT_RETURN_ROW_VALUES", user_id))
        response = self.client.get(
            "/internal/agent/admin-db-schema",
            headers={"X-Internal-Agent-Token": "test-internal-token"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        tables = {table["name"]: table for table in body["tables"]}
        self.assertIn("select name only", body["identity_guidance"])
        self.assertEqual(set(tables), ALLOWED_TABLES)
        # Sage bounds each native tool result to 4,000 characters. A larger
        # discovery result silently shortens strings, including SQL and semantics.
        self.assertLessEqual(len(json.dumps(body, separators=(",", ":"))), 3900)
        for name in tables:
            detail = self.client.get(
                "/internal/agent/admin-db-schema", params={"table": name},
                headers={"X-Internal-Agent-Token": "test-internal-token"},
            )
            self.assertEqual(detail.status_code, 200)
            self.assertNotIn("SCHEMA_MUST_NOT_RETURN_ROW_VALUES", detail.text)
            tables[name] = detail.json()
            self.assertLessEqual(len(json.dumps(detail.json(), separators=(",", ":"))), 3900)
        self.assertIn("turn_count", {c["name"] for c in tables["session_logs"]["columns"]})
        self.assertIn(
            {"column": "subject_user_id", "table": "users", "target_column": "id"},
            tables["session_logs"]["foreign_keys"],
        )
        self.assertNotIn("SCHEMA_MUST_NOT_RETURN_ROW_VALUES", response.text)
        self.assertNotIn("deployment_config", tables)
        self.assertEqual(body["usage"]["metric"], "recorded_turns")

    def test_internal_admin_schema_rejects_tables_outside_allowlist(self) -> None:
        for table in ["deployment_config", "sqlite_master", 'users\") ; SELECT 1']:
            response = self.client.get(
                "/internal/agent/admin-db-schema", params={"table": table},
                headers={"X-Internal-Agent-Token": "test-internal-token"},
            )
            self.assertEqual(response.status_code, 400)

    def test_discovered_usage_query_ranks_users_without_counting_admin_tests(self) -> None:
        user_ids = [self.database.create_user() for _ in range(6)]
        with self.database.get_cursor() as cursor:
            for index, (user_index, source, turns) in enumerate([
                (0, "user", 2), (0, "user", 5), (1, "user", 4),
                (2, "admin_test", 100), (3, "user", 7),
            ]):
                cursor.execute(
                    "INSERT INTO session_logs "
                    "(log_id, source, subject_user_id, turn_count, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (f"fixture-{index}", source, user_ids[user_index], turns,
                     f"2026-10-{index + 10:02d} 12:00:00"),
                )
        schema = self.client.get(
            "/internal/agent/admin-db-schema",
            headers={"X-Internal-Agent-Token": "test-internal-token"},
        )
        self.assertEqual(schema.status_code, 200)
        response = self.client.post(
            "/internal/agent/admin-db-query",
            headers={"X-Internal-Agent-Token": "test-internal-token"},
            json={"sql": schema.json()["usage"]["top_users_sql"]},
        )
        body = response.json()
        self.assertTrue(body["success"], body.get("error"))
        rows = body["rows"]
        self.assertEqual([r["user_id"] for r in rows], [user_ids[i] for i in [0, 3, 1, 2, 4]])
        self.assertEqual([r["recorded_turns"] for r in rows], [7, 7, 4, 0, 0])
        self.assertEqual([r["recorded_conversations"] for r in rows], [2, 1, 1, 0, 0])
        self.assertEqual(rows[0]["last_recorded_activity"], "2026-10-11 12:00:00")
        self.assertIsNone(rows[3]["last_recorded_activity"])
        population = self.client.post(
            "/internal/agent/admin-db-query",
            headers={"X-Internal-Agent-Token": "test-internal-token"},
            json={"sql": schema.json()["usage"]["population_counts_sql"]},
        ).json()
        self.assertTrue(population["success"], population.get("error"))
        self.assertEqual(population["rows"], [{"total_users": 6, "active_users": 3}])

    def test_database_export_creates_copied_export_audit_evidence(self) -> None:
        response = self.client.get("/admin/database/export")

        self.assertEqual(response.status_code, 200)
        entries = self.database.get_config_audit_log(limit=1, table_name="data_deletion")
        self.assertTrue(entries)
        self.assertEqual(entries[0]["config_key"], "copied_export:sqlite_database")
        self.assertEqual(entries[0]["changed_by"], "admin-pubkey")
        event = json.loads(entries[0]["new_value"])
        self.assertEqual(event["workflow"], "copied_export")
        self.assertEqual(event["target"], "sqlite_database")
        self.assertEqual(event["lifecycle_posture"], "outside_active_storage_lifecycle")

    def test_user_roster_export_records_copied_export_audit_evidence(self) -> None:
        response = self.client.post(
            "/admin/users/roster-export",
            json={
                "filename": "enclave_users_20260703T183000Z.xlsx",
                "user_count": 3,
                "pending_count": 1,
                "includes_decrypted_browser_values": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        entries = self.database.get_config_audit_log(limit=1, table_name="data_deletion")
        self.assertTrue(entries)
        self.assertEqual(entries[0]["config_key"], "copied_export:user_roster")
        self.assertEqual(entries[0]["changed_by"], "admin-pubkey")
        event = json.loads(entries[0]["new_value"])
        self.assertEqual(event["workflow"], "copied_export")
        self.assertEqual(event["target"], "user_roster")
        self.assertEqual(event["lifecycle_posture"], "outside_active_storage_lifecycle")
        self.assertEqual(event["filename"], "enclave_users_20260703T183000Z.xlsx")
        self.assertEqual(event["user_count"], 3)
        self.assertEqual(event["pending_count"], 1)
        self.assertTrue(event["includes_decrypted_browser_values"])
        self.assertFalse(event["plaintext_contents_received_by_backend"])

    def test_user_roster_export_fails_when_audit_write_fails(self) -> None:
        original_log_config_audit_event = self.database.log_config_audit_event

        def failing_log_config_audit_event(**_kwargs) -> None:
            raise RuntimeError("audit unavailable")

        self.database.log_config_audit_event = failing_log_config_audit_event
        try:
            client = TestClient(self.main.app, raise_server_exceptions=False)
            response = client.post(
                "/admin/users/roster-export",
                json={
                    "filename": "enclave_users_20260703T183000Z.xlsx",
                    "user_count": 3,
                    "pending_count": 1,
                    "includes_decrypted_browser_values": True,
                },
            )
        finally:
            self.database.log_config_audit_event = original_log_config_audit_event

        self.assertEqual(response.status_code, 500)


if __name__ == "__main__":
    unittest.main()
