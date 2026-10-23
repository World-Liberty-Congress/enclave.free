from __future__ import annotations

import importlib
import os
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from coincurve import PrivateKey


APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class InternalSessionLogsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "enclave.db"
        self._orig_sqlite_path = os.environ.get("SQLITE_PATH")
        self._orig_internal_token = os.environ.get("INTERNAL_AGENT_TOKEN")
        self._orig_store_module = sys.modules.get("store")
        os.environ["SQLITE_PATH"] = str(self.db_path)
        os.environ["INTERNAL_AGENT_TOKEN"] = "test-internal-token"
        store_stub = types.ModuleType("store")
        store_stub.embed_texts = lambda texts: [[0.0] for _ in texts]
        store_stub.COLLECTION_NAME = "test"
        store_stub.QDRANT_HOST = "localhost"
        store_stub.QDRANT_PORT = 6333
        sys.modules["store"] = store_stub

        import database
        import internal_agent
        import session_logs

        self.database = importlib.reload(database)
        self.session_logs = importlib.reload(session_logs)
        self.internal_agent = importlib.reload(internal_agent)
        self.database.init_schema()

        app = FastAPI()
        app.include_router(self.internal_agent.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self.database._connection is not None:
            self.database._connection.close()
            self.database._connection = None
        if self._orig_sqlite_path is None:
            os.environ.pop("SQLITE_PATH", None)
        else:
            os.environ["SQLITE_PATH"] = self._orig_sqlite_path
        if self._orig_internal_token is None:
            os.environ.pop("INTERNAL_AGENT_TOKEN", None)
        else:
            os.environ["INTERNAL_AGENT_TOKEN"] = self._orig_internal_token
        if self._orig_store_module is None:
            sys.modules.pop("store", None)
        else:
            sys.modules["store"] = self._orig_store_module
        self.tmp.cleanup()

    def _headers(self) -> dict[str, str]:
        return {"x-internal-agent-token": "test-internal-token"}

    @staticmethod
    def _messages(*contents: str) -> list[dict[str, str]]:
        return [
            {
                "message_id": f"msg-{index}",
                "sequence_id": index + 1,
                "role": "user" if index % 2 == 0 else "assistant",
                "content": content,
            }
            for index, content in enumerate(contents)
        ]

    def _session_log_count(self) -> int:
        with self.database.get_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM session_logs")
            return int(cursor.fetchone()["count"])

    def test_internal_user_session_logs_reject_non_user_actor(self) -> None:
        response = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                "actor": {"id": 1, "type": "admin", "approved": True},
                "sage_session_id": "sage-session-hello",
                "through_sequence_id": 2,
                "messages": self._messages("hello", "hi"),
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._session_log_count(), 0)

    def test_internal_failed_transcript_save_removes_metadata_row(self) -> None:
        user_id = self.database.create_user()

        response = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                "actor": {"id": user_id, "type": "user", "approved": True},
                "sage_session_id": "sage-session-hello",
                "through_sequence_id": 2,
                "messages": self._messages("hello", "hi"),
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("No admin configured", response.json()["detail"])
        self.assertEqual(self._session_log_count(), 0)

    def test_reserved_looking_email_does_not_suppress_normal_user_log(self) -> None:
        admin_key = PrivateKey()
        self.database.add_admin(admin_key.public_key.format(compressed=True)[1:].hex())
        user_id = self.database.create_user(
            pubkey=PrivateKey().public_key.format(compressed=True)[1:].hex(),
            email="test-user+ordinary@enclave.test",
            name="Ordinary User",
        )

        response = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                "actor": {
                    "id": user_id,
                    "type": "user",
                    "approved": True,
                    "email": "test-user+ordinary@enclave.test",
                },
                "sage_session_id": "sage-session-hello",
                "through_sequence_id": 2,
                "messages": self._messages("hello", "hi"),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["status"], "skipped")
        self.assertEqual(self._session_log_count(), 1)

    def test_instance_derived_test_user_skips_ambient_log(self) -> None:
        import impersonation

        admin_key = PrivateKey()
        admin_pubkey = admin_key.public_key.format(compressed=True)[1:].hex()
        self.database.add_admin(admin_pubkey)
        user_type_id = self.database.create_user_type("Student")
        user_id = self.database.create_user(
            pubkey=impersonation.derive_test_user_pubkey(
                admin_pubkey,
                user_type_id,
            ),
            email=f"test-user+type{user_type_id}@enclave.test",
            name="Test User",
            user_type_id=user_type_id,
        )

        response = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                "actor": {
                    "id": user_id,
                    "type": "user",
                    "approved": True,
                    "email": f"test-user+type{user_type_id}@enclave.test",
                    "user_type_id": user_type_id,
                },
                "sage_session_id": "sage-session-hello",
                "through_sequence_id": 2,
                "messages": self._messages("hello", "hi"),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "skipped")
        self.assertEqual(response.json()["log_id"], "")
        self.assertEqual(self._session_log_count(), 0)

    def test_internal_user_session_log_updates_existing_sage_session_record(self) -> None:
        admin_key = PrivateKey()
        self.database.add_admin(admin_key.public_key.format(compressed=True)[1:].hex())
        user_id = self.database.create_user()

        first = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                "actor": {"id": user_id, "type": "user", "approved": True},
                "sage_session_id": "sage-session-1",
                "through_sequence_id": 2,
                "messages": self._messages("first question", "first answer"),
            },
        )
        second = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                "actor": {"id": user_id, "type": "user", "approved": True},
                "sage_session_id": "sage-session-1",
                "through_sequence_id": 4,
                "messages": self._messages(
                    "first question",
                    "first answer",
                    "second question",
                    "second answer",
                ),
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self._session_log_count(), 1)
        self.assertEqual(second.json()["log_id"], first.json()["log_id"])
        self.assertEqual(second.json()["turn_count"], 4)
        stale = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                "actor": {"id": user_id, "type": "user", "approved": True},
                "sage_session_id": "sage-session-1",
                "through_sequence_id": 2,
                "messages": self._messages("first question", "first answer"),
            },
        )
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.json()["log_id"], first.json()["log_id"])
        self.assertEqual(stale.json()["turn_count"], 4)
        with self.database.get_cursor() as cursor:
            cursor.execute("SELECT transcript_ciphertext, turn_count FROM session_logs")
            row = cursor.fetchone()
            ciphertext = row["transcript_ciphertext"]
            turn_count = row["turn_count"]
        self.assertIsNotNone(ciphertext)
        self.assertNotIn("second answer", ciphertext)
        self.assertEqual(turn_count, 4)

    def test_internal_user_session_log_reuse_is_concurrent_idempotent(self) -> None:
        admin_key = PrivateKey()
        self.database.add_admin(admin_key.public_key.format(compressed=True)[1:].hex())
        user_id = self.database.create_user()
        barrier = threading.Barrier(2)

        history = self._messages("q1", "a1", "q2", "a2")

        def post_session(message_count: int):
            barrier.wait(timeout=5)
            return self.client.post(
                "/internal/agent/session-logs",
                headers=self._headers(),
                json={
                    "actor": {"id": user_id, "type": "user", "approved": True},
                    "sage_session_id": "sage-session-concurrent",
                    "through_sequence_id": message_count,
                    "messages": history[:message_count],
                },
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(post_session, [2, 4]))

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(self._session_log_count(), 1)
        self.assertEqual(responses[0].json()["log_id"], responses[1].json()["log_id"])
        with self.database.get_cursor() as cursor:
            cursor.execute("SELECT turn_count FROM session_logs")
            self.assertEqual(cursor.fetchone()["turn_count"], 4)

    def test_internal_user_session_log_reuse_is_scoped_to_subject_user(self) -> None:
        admin_key = PrivateKey()
        self.database.add_admin(admin_key.public_key.format(compressed=True)[1:].hex())
        first_user_id = self.database.create_user()
        second_user_id = self.database.create_user()

        first = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                "actor": {"id": first_user_id, "type": "user", "approved": True},
                "sage_session_id": "shared-sage-session",
                "through_sequence_id": 2,
                "messages": self._messages("first user question", "first user answer"),
            },
        )
        second = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                "actor": {"id": second_user_id, "type": "user", "approved": True},
                "sage_session_id": "shared-sage-session",
                "through_sequence_id": 4,
                "messages": self._messages("second user question", "second user answer"),
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(second.json()["log_id"], first.json()["log_id"])
        self.assertEqual(self._session_log_count(), 2)

    def test_internal_user_session_log_rejects_snapshots_without_message_ids(self) -> None:
        admin_key = PrivateKey()
        self.database.add_admin(admin_key.public_key.format(compressed=True)[1:].hex())
        user_id = self.database.create_user()

        for payload in (
            {"sage_session_id": "s", "turns": [{"role": "user", "content": "hello"}]},
            {"sage_session_id": "s", "messages": [{"role": "user", "content": "hello"}]},
            {"messages": self._messages("hello", "hi"), "through_sequence_id": 2},
            {"sage_session_id": "s", "messages": self._messages("hello", "hi")},
            {
                "sage_session_id": "s",
                "through_sequence_id": 1,
                "messages": [{"message_id": "m", "role": "user", "content": "no sequence"}],
            },
            {"sage_session_id": "s", "messages": [], "through_sequence_id": 0},
            {
                "sage_session_id": "s",
                "through_sequence_id": 1,
                "messages": [
                    {"message_id": "m", "sequence_id": 1, "role": "tool", "content": "internal tool context"},
                ],
            },
        ):
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/internal/agent/session-logs",
                    headers=self._headers(),
                    json={"actor": {"id": user_id, "type": "user", "approved": True}, **payload},
                )
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self._session_log_count(), 0)

    def test_deleted_user_conversation_is_skipped_not_recorded(self) -> None:
        admin_key = PrivateKey()
        self.database.add_admin(admin_key.public_key.format(compressed=True)[1:].hex())

        response = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                "actor": {"id": 9999, "type": "user", "approved": True},
                "sage_session_id": "deleted-user-session",
                "through_sequence_id": 2,
                "messages": self._messages("hello", "hi"),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "skipped")
        self.assertEqual(self._session_log_count(), 0)

    def test_record_takes_the_user_type_from_the_users_table(self) -> None:
        admin_key = PrivateKey()
        self.database.add_admin(admin_key.public_key.format(compressed=True)[1:].hex())
        user_type_id = self.database.create_user_type("Student")
        user_id = self.database.create_user(user_type_id=user_type_id)

        response = self.client.post(
            "/internal/agent/session-logs",
            headers=self._headers(),
            json={
                # A retried update may carry a stale or deleted user type.
                "actor": {"id": user_id, "type": "user", "approved": True, "user_type_id": 999},
                "sage_session_id": "typed-session",
                "through_sequence_id": 2,
                "messages": self._messages("hello", "hi"),
            },
        )

        self.assertEqual(response.status_code, 200)
        with self.database.get_cursor() as cursor:
            cursor.execute("SELECT user_type_id FROM session_logs")
            self.assertEqual(cursor.fetchone()["user_type_id"], user_type_id)

    def test_update_with_only_deleted_messages_is_skipped(self) -> None:
        admin_key = PrivateKey()
        self.database.add_admin(admin_key.public_key.format(compressed=True)[1:].hex())
        user_id = self.database.create_user()
        payload = {
            "actor": {"id": user_id, "type": "user", "approved": True},
            "sage_session_id": "deleted-record-session",
            "through_sequence_id": 2,
            "messages": self._messages("hello", "hi"),
        }
        first = self.client.post("/internal/agent/session-logs", headers=self._headers(), json=payload)
        self.assertTrue(self.session_logs.delete_session_log(first.json()["log_id"]))

        retried = self.client.post("/internal/agent/session-logs", headers=self._headers(), json=payload)

        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json(), {"log_id": "", "status": "skipped", "turn_count": 0})
        self.assertEqual(self._session_log_count(), 0)


if __name__ == "__main__":
    unittest.main()
