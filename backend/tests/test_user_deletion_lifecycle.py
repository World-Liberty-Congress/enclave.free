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


class UserDeletionLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "enclave.db"
        self._orig_sqlite_path = os.environ.get("SQLITE_PATH")
        self._orig_secret_key = os.environ.get("SECRET_KEY")
        self._orig_uploads_dir = os.environ.get("UPLOADS_DIR")
        self._orig_sentence_transformers = sys.modules.get("sentence_transformers")
        sys.modules["sentence_transformers"] = types.SimpleNamespace(
            SentenceTransformer=DummySentenceTransformer
        )
        self.addCleanup(self._restore_sentence_transformers)
        os.environ["SQLITE_PATH"] = str(self.db_path)
        os.environ["SECRET_KEY"] = "test-secret"
        os.environ["UPLOADS_DIR"] = str(Path(self.tmp.name) / "uploads")

        import database
        import auth
        import deployment_config
        import lifecycle
        import main

        self.database = importlib.reload(database)
        self.auth = importlib.reload(auth)
        self.deployment_config = importlib.reload(deployment_config)
        self.lifecycle = importlib.reload(lifecycle)
        self.main = importlib.reload(main)
        self.database.init_schema()

        self.user_id = self.database.create_user(pubkey="a" * 64)
        self.memory_id = self.database.create_user_memory(
            subject_user_id=self.user_id,
            kind="preference",
            content="Prefers deletion receipts.",
            source_kind="conversation",
            source_conversation_id="query-session-1",
            author_actor="sage",
        )

        self.main.app.dependency_overrides[self.auth.require_admin_or_approved_user] = lambda: {
            "type": "admin",
            "id": 1,
            "pubkey": "admin-pubkey",
        }
        self.main.app.dependency_overrides[self.auth.require_admin_or_user] = lambda: {
            "type": "admin",
            "id": 1,
            "pubkey": "admin-pubkey",
        }
        self.main.app.dependency_overrides[self.auth.require_admin] = lambda: {
            "type": "admin",
            "id": 1,
            "pubkey": "admin-pubkey",
        }
        self.main.app.dependency_overrides[self.deployment_config.auth.require_admin] = lambda: {
            "type": "admin",
            "id": 1,
            "pubkey": "admin-pubkey",
        }
        self.main.app.dependency_overrides[self.lifecycle.auth.require_admin] = lambda: {
            "type": "admin",
            "id": 1,
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

    def audit_entries(self, table_name: str) -> list[dict]:
        response = self.client.get(f"/admin/deployment/audit-log?table_name={table_name}")
        self.assertEqual(response.status_code, 200)
        return response.json()["entries"]

    def test_user_deletion_removes_profile_and_memory_without_python_conversation_store(self) -> None:
        response = self.client.delete(f"/users/{self.user_id}")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["deletion"]["status"], "succeeded")
        actions = {result["action"]: result for result in body["deletion"]["results"]}
        self.assertEqual(actions["delete_user_memory"]["status"], "succeeded")
        self.assertEqual(actions["delete_user_memory"]["count"], 1)
        self.assertEqual(actions["delete_conversations"]["status"], "skipped")
        self.assertEqual(
            actions["delete_conversations"]["detail"],
            "Conversation session discovery is owned by Sage.",
        )
        self.assertEqual(actions["delete_user_profile"]["status"], "succeeded")
        self.assertEqual(actions["delete_user_approval"]["status"], "succeeded")

        self.assertIsNone(self.database.get_user(self.user_id))
        self.assertIsNone(self.database.get_user_memory(self.memory_id))
        users = self.client.get("/admin/users").json()["users"]
        self.assertNotIn(self.user_id, {user["id"] for user in users})

        entries = self.audit_entries("data_deletion")
        self.assertEqual(len(entries), 1)
        event = json.loads(entries[0]["new_value"])
        self.assertEqual(entries[0]["config_key"], f"user:{self.user_id}:delete")
        self.assertEqual(entries[0]["changed_by"], "admin-pubkey")
        self.assertEqual(event["workflow"], "delete_user")
        self.assertEqual(event["status"], "succeeded")
        self.assertEqual(event["counts"]["succeeded"], 3)
        self.assertEqual(event["counts"]["skipped"], 1)
        verify = self.client.get("/admin/deployment/audit-log/verify?table_name=data_deletion")
        self.assertEqual(verify.status_code, 200)
        self.assertTrue(verify.json()["valid"])

    def test_user_deletion_detaches_retained_session_logs(self) -> None:
        other_user_id = self.database.create_user(pubkey="b" * 64)
        with self.database.get_cursor() as cursor:
            for log_id, user_id in (("deleted-user-log", self.user_id), ("other-user-log", other_user_id)):
                cursor.execute(
                    "INSERT INTO session_logs (log_id, source, subject_user_id, transcript_ciphertext) "
                    "VALUES (?, 'user', ?, 'encrypted-transcript')",
                    (log_id, user_id),
                )
            cursor.execute(
                "INSERT INTO session_log_feedback (log_id, turn_index, rating) "
                "VALUES ('deleted-user-log', 0, 'up')"
            )

        for _ in range(2):
            response = self.client.delete(f"/users/{self.user_id}")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["success"])
        self.assertIsNone(self.database.get_user(self.user_id))
        self.assertIsNone(self.database.get_user_memory(self.memory_id))
        self.assertIsNotNone(self.database.get_user(other_user_id))
        with self.database.get_cursor() as cursor:
            rows = cursor.execute(
                "SELECT log_id, subject_user_id, transcript_ciphertext FROM session_logs ORDER BY log_id"
            ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [
                ("deleted-user-log", None, "encrypted-transcript"),
                ("other-user-log", other_user_id, "encrypted-transcript"),
            ])
            self.assertEqual(cursor.execute("SELECT COUNT(*) FROM session_log_feedback").fetchone()[0], 1)
            self.assertEqual(cursor.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_user_deletion_is_safe_to_repeat(self) -> None:
        first = self.client.delete(f"/users/{self.user_id}")
        self.assertEqual(first.status_code, 200)

        second = self.client.delete(f"/users/{self.user_id}")

        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["deletion"]["status"], "succeeded")
        self.assertEqual({result["status"] for result in body["deletion"]["results"]}, {"skipped"})

        entries = self.audit_entries("data_deletion")
        idempotent_entry = next(
            entry for entry in entries
            if json.loads(entry["new_value"])["counts"]["skipped"] == 4
        )
        idempotent_event = json.loads(idempotent_entry["new_value"])
        self.assertEqual(idempotent_entry["config_key"], f"user:{self.user_id}:delete")
        self.assertEqual(idempotent_event["workflow"], "delete_user")
        self.assertEqual(idempotent_event["counts"]["skipped"], 4)

    def test_user_self_deletion_without_pubkey_still_writes_audit_event(self) -> None:
        self.main.app.dependency_overrides[self.auth.require_admin_or_user] = lambda: {
            "type": "user",
            "id": self.user_id,
            "pubkey": None,
        }

        response = self.client.delete(f"/users/{self.user_id}")

        self.assertEqual(response.status_code, 200)
        entries = self.database.get_config_audit_log(limit=1, table_name="data_deletion")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["config_key"], f"user:{self.user_id}:delete")
        self.assertEqual(entries[0]["changed_by"], f"user:{self.user_id}")

    def test_user_deletion_reports_sage_owned_conversation_discovery(self) -> None:
        response = self.client.delete(f"/users/{self.user_id}")

        self.assertEqual(response.status_code, 200)
        actions = {result["action"]: result for result in response.json()["deletion"]["results"]}
        self.assertEqual(actions["delete_conversations"]["status"], "skipped")

    def test_user_deletion_requires_authentication(self) -> None:
        self.main.app.dependency_overrides.clear()

        response = self.client.delete(f"/users/{self.user_id}")

        self.assertIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
