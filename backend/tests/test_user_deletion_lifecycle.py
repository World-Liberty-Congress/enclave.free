from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx
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

        self._orig_internal_token = os.environ.get("INTERNAL_AGENT_TOKEN")
        os.environ["INTERNAL_AGENT_TOKEN"] = "synthetic-token"
        self.remote_calls = []
        self.remote_mode = "success"
        def remote(request):
            self.remote_calls.append(request)
            user_id = int(request.url.path.split("/")[-2])
            if self.remote_mode == "down":
                raise httpx.ConnectError("private remote text", request=request)
            if self.remote_mode == "lost_response":
                self.remote_committed = True
                raise httpx.ReadTimeout("lost receipt after commit", request=request)
            if self.remote_mode == "malformed":
                return httpx.Response(200, json={"private": "must not escape"})
            body = {
                "user_id": user_id, "deletion": {"status": "succeeded", "retryable": False},
                "conversations_deleted": 2, "identities_deleted": 1,
                "private": "must not escape",
            }
            if self.remote_mode == "wrong_user":
                body["user_id"] = user_id + 1
            elif self.remote_mode == "string_count":
                body["conversations_deleted"] = "2"
            elif self.remote_mode == "missing_count":
                body.pop("identities_deleted")
            elif self.remote_mode == "busy":
                return httpx.Response(409, json={"detail": "private error"})
            elif self.remote_mode == "old_route":
                return httpx.Response(404, json={"detail": "private error"})
            return httpx.Response(200, json=body)
        self.lifecycle._sage_client = httpx.AsyncClient(transport=httpx.MockTransport(remote))
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
        asyncio.run(self.lifecycle.close_sage_client())
        self._restore_env("INTERNAL_AGENT_TOKEN", self._orig_internal_token)
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
        self.assertEqual(actions["delete_user_runtime"]["status"], "succeeded")
        self.assertEqual(actions["delete_user_runtime"]["count"], 2)
        self.assertEqual(len(self.remote_calls), 1)
        self.assertNotIn("must not escape", json.dumps(body))
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
        self.assertEqual(event["counts"]["succeeded"], 4)
        self.assertEqual(event["counts"]["skipped"], 0)
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
                "INSERT INTO session_log_feedback (log_id, message_id, rating) "
                "VALUES ('deleted-user-log', 'legacy-1', 'up')"
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

    def test_user_deletion_records_completed_sage_intent(self) -> None:
        response = self.client.delete(f"/users/{self.user_id}")

        self.assertEqual(response.status_code, 200)
        actions = {result["action"]: result for result in response.json()["deletion"]["results"]}
        self.assertEqual(actions["delete_user_runtime"]["status"], "succeeded")
        intent = self.database.list_deletion_tombstones()[0]
        self.assertEqual(intent["conversation_id"], f"user:{self.user_id}")
        self.assertEqual(intent["status"], "completed")

    def test_remote_failure_is_partial_and_repeated_delete_repairs_same_intent(self) -> None:
        self.remote_mode = "down"
        body = self.client.delete(f"/users/{self.user_id}").json()
        self.assertTrue(body["success"])
        self.assertEqual(body["deletion"]["status"], "partial_failure")
        self.assertTrue(body["deletion"]["retryable"])
        self.assertNotIn("private remote text", json.dumps(body))
        self.assertIsNone(self.database.get_user(self.user_id))
        intent = self.database.list_deletion_tombstones()[0]
        self.assertEqual(intent["status"], "incomplete")
        self.remote_mode = "success"
        body = self.client.delete(f"/users/{self.user_id}").json()
        self.assertEqual(body["deletion"]["status"], "succeeded")
        self.assertEqual(len(self.database.list_deletion_tombstones()), 1)
        self.assertEqual(self.database.get_deletion_tombstone(intent["id"])["status"], "completed")

    def test_admin_retry_dispatches_user_and_keeps_conversation_retry(self) -> None:
        self.remote_mode = "down"
        self.client.delete(f"/users/{self.user_id}")
        intent = self.database.list_deletion_tombstones()[0]
        self.remote_mode = "success"
        response = self.client.post(f"/admin/lifecycle/deletion-tombstones/{intent['id']}/retry")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tombstone"]["status"], "completed")
        old = self.database.create_deletion_tombstone(
            lifecycle_data_class="sage_session_memory", conversation_id="legacy-conversation",
            former_subject_ref=None, status="incomplete", source="user", workflow="delete_conversation", deletion={},
        )
        with patch.object(self.lifecycle, "delete_session_memory_for_conversation", new_callable=AsyncMock) as delete:
            delete.return_value = self.main.summarize_deletion_results([])
            response = self.client.post(f"/admin/lifecycle/deletion-tombstones/{old}/retry")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tombstone"]["status"], "completed")

    def test_lost_success_receipt_is_repaired_idempotently(self) -> None:
        self.remote_committed = False
        self.remote_mode = "lost_response"
        body = self.client.delete(f"/users/{self.user_id}").json()
        self.assertTrue(self.remote_committed)
        self.assertEqual(body["deletion"]["status"], "partial_failure")
        intent = self.database.list_deletion_tombstones()[0]
        self.assertEqual(intent["status"], "incomplete")
        self.remote_mode = "success"
        response = self.client.post(f"/admin/lifecycle/deletion-tombstones/{intent['id']}/retry")
        self.assertEqual(response.json()["tombstone"]["status"], "completed")

    def test_malformed_receipt_and_completion_write_failure_remain_retryable(self) -> None:
        self.remote_mode = "malformed"
        body = self.client.delete(f"/users/{self.user_id}").json()
        self.assertEqual(body["deletion"]["status"], "partial_failure")
        self.assertNotIn("must not escape", json.dumps(body))
        self.remote_mode = "success"
        with patch.object(self.database, "update_deletion_tombstone_after_retry", side_effect=RuntimeError("disk failure")):
            body = self.client.delete(f"/users/{self.user_id}").json()
        self.assertEqual(body["deletion"]["status"], "partial_failure")
        self.assertEqual(self.database.list_deletion_tombstones()[0]["status"], "incomplete")

    def test_invalid_targets_counts_busy_and_old_route_never_complete(self) -> None:
        for index, mode in enumerate(("wrong_user", "string_count", "missing_count", "busy", "old_route")):
            with self.subTest(mode=mode):
                user_id = self.database.create_user(pubkey=str(index + 1) * 64)
                self.remote_mode = mode
                body = self.client.delete(f"/users/{user_id}").json()
                self.assertEqual(body["deletion"]["status"], "partial_failure")
                self.assertTrue(body["deletion"]["retryable"])
                self.assertNotIn("private", json.dumps(body))
        self.assertEqual(len(self.database.list_deletion_tombstones(status="incomplete")), 5)

    def test_intent_is_atomic_with_local_deletion_and_survives_before_dispatch(self) -> None:
        with patch.object(self.database, "_purge_user_memories_for_subject_user_tx", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.database.delete_user_lifecycle(self.user_id)
        self.assertIsNotNone(self.database.get_user(self.user_id))
        self.assertEqual(self.database.list_deletion_tombstones(), [])
        local = self.database.delete_user_lifecycle(self.user_id)
        self.assertIsNone(self.database.get_user(self.user_id))
        self.assertEqual(self.database.get_deletion_tombstone(local["tombstone_id"])["status"], "incomplete")
        self.assertEqual(self.remote_calls, [])
        response = self.client.post(f"/admin/lifecycle/deletion-tombstones/{local['tombstone_id']}/retry")
        self.assertEqual(response.json()["tombstone"]["status"], "completed")

    def test_completion_is_monotonic_and_startup_preserves_multiple_user_intents(self) -> None:
        other = self.database.create_user(pubkey="c" * 64)
        one = self.database.delete_user_lifecycle(self.user_id)["tombstone_id"]
        two = self.database.delete_user_lifecycle(other)["tombstone_id"]
        self.database.init_schema()
        self.assertEqual({t["id"] for t in self.database.list_deletion_tombstones()}, {one, two})
        succeeded = self.main.summarize_deletion_results([])
        self.database.update_deletion_tombstone_after_retry(one, status="completed", deletion=succeeded)
        self.database.update_deletion_tombstone_after_retry(one, status="incomplete", deletion={"status": "failed"})
        self.assertEqual(self.database.get_deletion_tombstone(one)["deletion"], succeeded)

    def test_user_deletion_requires_authentication(self) -> None:
        self.main.app.dependency_overrides.clear()

        response = self.client.delete(f"/users/{self.user_id}")

        self.assertIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
