from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from coincurve import PrivateKey


APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class SessionLogsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "enclave.db"
        self._orig_sqlite_path = os.environ.get("SQLITE_PATH")
        os.environ["SQLITE_PATH"] = str(self.db_path)

        import database
        import encryption
        import session_logs

        self.database = importlib.reload(database)
        self.encryption = importlib.reload(encryption)
        self.session_logs = importlib.reload(session_logs)
        self.database.init_schema()

        admin_key = PrivateKey()
        self.admin_private_key = admin_key.secret
        self.admin_pubkey = admin_key.public_key.format(compressed=True)[1:].hex()
        self.database.add_admin(self.admin_pubkey)

    def tearDown(self) -> None:
        if self.database._connection is not None:
            self.database._connection.close()
            self.database._connection = None
        if self._orig_sqlite_path is None:
            os.environ.pop("SQLITE_PATH", None)
        else:
            os.environ["SQLITE_PATH"] = self._orig_sqlite_path
        self.tmp.cleanup()

    def transcript_files(self) -> list[Path]:
        transcript_dir = Path(self.tmp.name) / "session_logs"
        return sorted(transcript_dir.glob("*.json")) if transcript_dir.exists() else []

    def clear_admins(self) -> None:
        with self.database.get_cursor() as cursor:
            cursor.execute("DELETE FROM admins")

    def decrypt_transcript(self, log_id: str) -> list[dict]:
        detail = self.session_logs.get_session_log(log_id)
        plaintext = self.encryption.nip04_decrypt(
            detail["transcript_ciphertext"],
            detail["transcript_ephemeral_pubkey"],
            self.admin_private_key,
        )
        return json.loads(plaintext)["turns"]

    def message_ids(self, log_id: str, role: str | None = None) -> list[str]:
        detail = self.session_logs.get_session_log(log_id)
        return [
            message["message_id"]
            for message in detail["transcript_messages"]
            if role is None or message["role"] == role
        ]

    @staticmethod
    def sage_history(exchanges: int, *, prefix: str = "m") -> list[dict]:
        history = []
        for number in range(1, exchanges + 1):
            history.append({
                "message_id": f"{prefix}-{number}-q",
                "role": "user",
                "content": f"Question {number}",
            })
            history.append({
                "message_id": f"{prefix}-{number}-a",
                "role": "assistant",
                "content": f"Answer {number}",
            })
        return history

    def record(
        self,
        messages: list[dict],
        *,
        user_id: int,
        session: str = "sage-session",
        through: int | None = None,
    ) -> dict:
        """Record as Sage would; by default the newest message's Sage sequence
        number is the history length."""
        return self.session_logs.record_user_conversation(
            sage_session_id=session,
            subject_user_id=user_id,
            messages=messages,
            through_sequence_id=len(messages) if through is None else through,
            title="User Conversation",
        )

    def attach_legacy_transcript_file(self, log_id: str) -> Path:
        """Simulate a record written by a release that also kept an encrypted file."""
        path = Path(self.tmp.name) / "session_logs" / f"{log_id}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text("legacy-ciphertext", encoding="utf-8")
        with self.database.get_cursor() as cursor:
            cursor.execute(
                "UPDATE session_logs SET transcript_path = ? WHERE log_id = ?",
                (str(path), log_id),
            )
        return path

    @staticmethod
    def model_usage_trace() -> dict:
        return {
            "visibility": "detailed",
            "reasoning": {"summary": "Sage answered."},
            "trace_deltas": [
                {
                    "id": "model-usage-2-1",
                    "kind": "timing",
                    "title": "Model usage",
                    "content": "Provider-reported model usage observed.",
                    "status": "succeeded",
                    "metadata": {
                        "phase": "model_request",
                        "step": 2,
                        "attempt": 1,
                        "prompt_tokens": 101,
                        "completion_tokens": 29,
                        "total_tokens": 130,
                    },
                }
            ],
            "tools": [],
            "retrieval": [],
            "activity_steps": [],
            "suppressed": False,
        }

    def test_session_log_save_payload_preserves_trace_and_tool_metadata(self) -> None:
        from models import SessionLogSaveTranscript

        payload = SessionLogSaveTranscript(
            turns=[
                {"role": "user", "content": "Find resources"},
                {
                    "role": "assistant",
                    "content": "I found vetted resources.",
                    "tools_used": [
                        {
                            "tool_id": "curated-resources",
                            "tool_name": "Curated Resources",
                            "output_summary": "Found 2 vetted resources.",
                        }
                    ],
                    "trace": {
                        "visibility": "detailed",
                        "reasoning": {
                            "summary": "Sage used enabled tools before answering."
                        },
                        "tools": [
                            {
                                "id": "curated-resources",
                                "name": "Curated Resources",
                                "status": "succeeded",
                                "output_summary": "Found 2 vetted resources.",
                            }
                        ],
                        "trace_deltas": self.model_usage_trace()["trace_deltas"],
                    },
                },
            ]
        )

        log = self.session_logs.create_session_log(title="Trace metadata test")
        saved = self.session_logs.save_transcript(
            log["log_id"],
            payload.model_dump()["turns"],
            created_by="admin",
        )
        self.assertTrue(saved["has_transcript"])

        detail = self.session_logs.get_session_log(log["log_id"])
        self.assertIsNotNone(detail)
        decrypted_transcript = self.encryption.nip04_decrypt(
            detail["transcript_ciphertext"],
            detail["transcript_ephemeral_pubkey"],
            self.admin_private_key,
        )
        persisted = json.loads(decrypted_transcript)
        assistant_turn = persisted["turns"][1]
        self.assertEqual(
            assistant_turn["tools_used"][0]["tool_id"], "curated-resources"
        )
        self.assertEqual(
            assistant_turn["trace"]["tools"][0]["name"], "Curated Resources"
        )
        self.assertEqual(len(persisted["turns"]), 2)
        self.assertEqual(persisted["turns"][0]["role"], "user")
        self.assertEqual(persisted["turns"][0]["content"], "Find resources")
        self.assertEqual(persisted["turns"][1]["role"], "assistant")
        self.assertEqual(
            persisted["turns"][1]["tools_used"][0]["output_summary"],
            "Found 2 vetted resources.",
        )
        self.assertEqual(
            persisted["turns"][1]["trace"]["reasoning"]["summary"],
            "Sage used enabled tools before answering.",
        )
        usage = persisted["turns"][1]["trace"]["trace_deltas"][0]["metadata"]
        self.assertEqual(usage["prompt_tokens"], 101)
        self.assertEqual(usage["completion_tokens"], 29)
        self.assertEqual(usage["total_tokens"], 130)

    def test_saving_missing_session_log_leaves_no_transcript_artifact(self) -> None:
        turns = [
            {"role": "user", "content": "please help me test this instance"},
            {"role": "assistant", "content": "I can help with that."},
        ]

        with self.assertRaises(KeyError):
            self.session_logs.save_transcript("missing-log-id", turns, created_by="admin")

        self.assertEqual(self.transcript_files(), [])

    def test_no_admin_config_fails_closed_for_transcripts_and_feedback_comments(self) -> None:
        self.clear_admins()
        no_admin_log = self.session_logs.create_session_log(title="No admin transcript")

        with self.assertRaises(ValueError):
            self.session_logs.save_transcript(
                no_admin_log["log_id"],
                [
                    {"role": "user", "content": "please help me test this instance"},
                    {"role": "assistant", "content": "I can help with that."},
                ],
            )

        self.assertEqual(self.transcript_files(), [])
        no_admin_metadata = self.session_logs.get_session_log_metadata(no_admin_log["log_id"])
        self.assertIsNotNone(no_admin_metadata)
        self.assertFalse(no_admin_metadata["has_transcript"])

        self.database.add_admin(self.admin_pubkey)
        feedback_log = self.session_logs.create_session_log(title="Feedback without admin")
        self.session_logs.save_transcript(
            feedback_log["log_id"],
            [
                {"role": "user", "content": "please help me test this instance"},
                {"role": "assistant", "content": "I can help with that."},
            ],
        )
        self.clear_admins()

        assistant_id = self.message_ids(feedback_log["log_id"], "assistant")[0]
        with self.assertRaises(ValueError):
            self.session_logs.set_message_feedback(
                feedback_log["log_id"],
                assistant_id,
                "up",
                comment="This plaintext note must not be stored without an admin key.",
            )

        self.assertEqual(self.session_logs.list_feedback(feedback_log["log_id"]), [])

    def test_sage_session_lookup_is_scoped_to_subject_user(self) -> None:
        first_user_id = self.database.create_user()
        second_user_id = self.database.create_user()

        first_log = self.session_logs.create_session_log(
            source="user",
            title="First user",
            subject_user_id=first_user_id,
            sage_session_id="shared-sage-session",
        )
        second_log = self.session_logs.create_session_log(
            source="user",
            title="Second user",
            subject_user_id=second_user_id,
            sage_session_id="shared-sage-session",
        )

        first_lookup = self.session_logs.get_session_log_metadata_by_sage_session_id(
            source="user",
            sage_session_id="shared-sage-session",
            subject_user_id=first_user_id,
        )
        second_lookup = self.session_logs.get_session_log_metadata_by_sage_session_id(
            source="user",
            sage_session_id="shared-sage-session",
            subject_user_id=second_user_id,
        )

        self.assertEqual(first_lookup["log_id"], first_log["log_id"])
        self.assertEqual(second_lookup["log_id"], second_log["log_id"])
        with self.assertRaises(self.database.sqlite3.IntegrityError):
            self.session_logs.create_session_log(
                source="user",
                title="Duplicate first user",
                subject_user_id=first_user_id,
                sage_session_id="shared-sage-session",
            )

    def test_sage_session_index_migration_dedupes_existing_rows(self) -> None:
        with self.database.get_cursor() as cursor:
            cursor.execute("DROP INDEX idx_session_logs_source_sage_session_subject")
        user_id = self.database.create_user()
        older_log = self.session_logs.create_session_log(
            source="user",
            title="Older duplicate",
            subject_user_id=user_id,
            sage_session_id="duplicate-sage-session",
        )
        newer_log = self.session_logs.create_session_log(
            source="user",
            title="Newer duplicate",
            subject_user_id=user_id,
            sage_session_id="duplicate-sage-session",
        )
        with self.database.get_cursor() as cursor:
            cursor.execute(
                "UPDATE session_logs SET updated_at = ? WHERE log_id = ?",
                ("2026-01-01T00:00:00Z", older_log["log_id"]),
            )
            cursor.execute(
                "UPDATE session_logs SET updated_at = ? WHERE log_id = ?",
                ("2026-01-02T00:00:00Z", newer_log["log_id"]),
            )

        self.database._migrate_add_session_log_source_sage_session_index()

        with self.database.get_cursor() as cursor:
            cursor.execute(
                """SELECT log_id
                   FROM session_logs
                   WHERE source = 'user'
                     AND sage_session_id = 'duplicate-sage-session'
                     AND subject_user_id = ?""",
                (user_id,),
            )
            rows = cursor.fetchall()

        self.assertEqual([row["log_id"] for row in rows], [newer_log["log_id"]])

    def test_feedback_rejects_user_turns(self) -> None:
        log = self.session_logs.create_session_log(title="Synthetic User test")
        self.session_logs.save_transcript(
            log["log_id"],
            [
                {"role": "user", "content": "please help me test this instance"},
                {"role": "assistant", "content": "I can help with that."},
            ],
            created_by="admin",
        )

        user_message_id = self.message_ids(log["log_id"], "user")[0]
        with self.assertRaises(ValueError):
            self.session_logs.set_message_feedback(
                log["log_id"],
                user_message_id,
                "down",
                comment="This should not be ratable because it was the user turn.",
                created_by="admin",
            )

    def test_feedback_accepts_assistant_turns_with_encrypted_comments(self) -> None:
        transcript_sentinel = "assistant turn feedback transcript sentinel"
        comment_sentinel = "this assistant answer missed the key safety constraint"
        log = self.session_logs.create_session_log(title="Synthetic User test")
        self.session_logs.save_transcript(
            log["log_id"],
            [
                {"role": "user", "content": "please help me test this instance"},
                {
                    "role": "assistant",
                    "content": transcript_sentinel,
                    "trace": self.model_usage_trace(),
                },
            ],
            created_by="admin",
        )

        assistant_id = self.message_ids(log["log_id"], "assistant")[0]
        feedback = self.session_logs.set_message_feedback(
            log["log_id"],
            assistant_id,
            "down",
            comment=comment_sentinel,
            created_by="admin",
        )

        self.assertEqual(feedback["message_id"], assistant_id)
        self.assertEqual(feedback["turn_index"], 1)
        self.assertEqual(feedback["rating"], "down")
        self.assertIsNotNone(feedback["comment_ciphertext"])
        self.assertNotIn(comment_sentinel, feedback["comment_ciphertext"])
        detail = self.session_logs.get_session_log(log["log_id"])
        self.assertIsNotNone(detail)
        self.assertNotIn(transcript_sentinel, detail["transcript_ciphertext"])
        with self.database.get_cursor() as cursor:
            cursor.execute(
                "SELECT transcript_ciphertext FROM session_logs WHERE log_id = ?",
                (log["log_id"],),
            )
            stored = cursor.fetchone()["transcript_ciphertext"]
        self.assertEqual(stored, detail["transcript_ciphertext"])
        self.assertNotIn(transcript_sentinel, stored)
        decrypted_transcript = self.encryption.nip04_decrypt(
            detail["transcript_ciphertext"],
            detail["transcript_ephemeral_pubkey"],
            self.admin_private_key,
        )
        self.assertEqual(json.loads(decrypted_transcript)["turns"][1]["content"], transcript_sentinel)

    def test_feedback_rejects_unknown_message_ids(self) -> None:
        log = self.session_logs.create_session_log(title="Synthetic User test")
        self.session_logs.save_transcript(
            log["log_id"],
            [
                {"role": "user", "content": "please help me test this instance"},
                {"role": "assistant", "content": "I can help with that."},
            ],
            created_by="admin",
        )

        for message_id in ("1", "legacy-1", "not-in-this-transcript"):
            with self.subTest(message_id=message_id):
                with self.assertRaises(ValueError):
                    self.session_logs.set_message_feedback(log["log_id"], message_id, "up")

    def test_session_log_export_returns_zip_and_audits_copied_export(self) -> None:
        transcript_sentinel = "export transcript plaintext sentinel"
        comment_sentinel = "Useful answer"
        log = self.session_logs.create_session_log(title="Synthetic User export")
        self.session_logs.save_transcript(
            log["log_id"],
            [
                {"role": "user", "content": "Please test export."},
                {
                    "role": "assistant",
                    "content": transcript_sentinel,
                    "trace": self.model_usage_trace(),
                },
            ],
            created_by="admin-pubkey",
        )
        assistant_id = self.message_ids(log["log_id"], "assistant")[0]
        self.session_logs.set_message_feedback(
            log["log_id"],
            assistant_id,
            "up",
            comment=comment_sentinel,
            created_by="admin-pubkey",
        )

        filename, payload = self.session_logs.export_session_log_zip(
            log["log_id"],
            changed_by="admin-pubkey",
        )

        self.assertTrue(filename.startswith("test_feedback_"))
        self.assertTrue(filename.endswith(".zip"))
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ["feedback.json", "metadata.json", "transcript.nip04.txt"],
            )
            metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
            feedback = json.loads(archive.read("feedback.json").decode("utf-8"))
            ciphertext = archive.read("transcript.nip04.txt").decode("utf-8")

        self.assertEqual(metadata["log_id"], log["log_id"])
        self.assertEqual(metadata["title"], "Synthetic User export")
        self.assertTrue(metadata["copied_export"])
        self.assertIsNotNone(metadata["transcript_ephemeral_pubkey"])
        self.assertEqual(metadata["encrypted_to_pubkey"], self.admin_pubkey)
        self.assertEqual(feedback[0]["message_id"], assistant_id)
        self.assertEqual(feedback[0]["turn_index"], 1)
        self.assertEqual(feedback[0]["rating"], "up")
        self.assertEqual(
            [message["role"] for message in metadata["transcript_messages"]],
            ["user", "assistant"],
        )
        self.assertNotIn(comment_sentinel, json.dumps(feedback))
        self.assertNotIn(transcript_sentinel, ciphertext)
        exported_transcript = json.loads(
            self.encryption.nip04_decrypt(
                ciphertext,
                metadata["transcript_ephemeral_pubkey"],
                self.admin_private_key,
            )
        )
        exported_usage = exported_transcript["turns"][1]["trace"]["trace_deltas"][0][
            "metadata"
        ]
        self.assertEqual(exported_usage["prompt_tokens"], 101)
        self.assertEqual(exported_usage["completion_tokens"], 29)
        self.assertEqual(exported_usage["total_tokens"], 130)

        entries = self.database.get_config_audit_log(limit=1, table_name="data_deletion")
        self.assertEqual(entries[0]["config_key"], "copied_export:test_feedback_session")
        event = json.loads(entries[0]["new_value"])
        self.assertEqual(event["workflow"], "copied_export")
        self.assertEqual(event["target"], "test_feedback_session")
        self.assertEqual(event["log_id"], log["log_id"])

    def test_plaintext_browser_export_is_audited_without_plaintext(self) -> None:
        log = self.session_logs.create_session_log(title="Readable export")
        filename = f"beta-session-log-{log['log_id']}.json"

        self.session_logs.record_plaintext_export(
            log["log_id"],
            filename=filename,
            changed_by="admin-pubkey",
        )

        entries = self.database.get_config_audit_log(
            limit=1,
            table_name="data_deletion",
        )
        self.assertEqual(entries[0]["config_key"], "copied_export:test_feedback_session")
        self.assertEqual(entries[0]["changed_by"], "admin-pubkey")
        event = json.loads(entries[0]["new_value"])
        self.assertEqual(event["log_id"], log["log_id"])
        self.assertEqual(event["filename"], filename)
        self.assertTrue(event["includes_decrypted_browser_values"])
        self.assertFalse(event["plaintext_contents_received_by_backend"])

    def test_delete_keeps_metadata_when_transcript_file_removal_fails(self) -> None:
        log = self.session_logs.create_session_log(title="Synthetic User delete")
        self.session_logs.save_transcript(
            log["log_id"],
            [
                {"role": "user", "content": "Please test delete."},
                {"role": "assistant", "content": "Delete test answer."},
            ],
            created_by="admin",
        )
        self.attach_legacy_transcript_file(log["log_id"])

        with patch.object(self.session_logs.os, "remove", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                self.session_logs.delete_session_log(log["log_id"])

        self.assertIsNotNone(self.session_logs.get_session_log_metadata(log["log_id"]))

    def test_delete_removes_transcript_containing_model_usage_observation(self) -> None:
        log = self.session_logs.create_session_log(title="Model usage lifecycle")
        self.session_logs.save_transcript(
            log["log_id"],
            [
                {"role": "user", "content": "Please test lifecycle."},
                {
                    "role": "assistant",
                    "content": "Lifecycle answer.",
                    "trace": self.model_usage_trace(),
                },
            ],
            created_by="admin",
        )
        self.assertEqual(self.transcript_files(), [])
        legacy_file = self.attach_legacy_transcript_file(log["log_id"])

        self.assertTrue(self.session_logs.delete_session_log(log["log_id"]))

        self.assertIsNone(self.session_logs.get_session_log_metadata(log["log_id"]))
        self.assertFalse(legacy_file.exists())

    def test_save_transcript_keeps_supplied_message_ids_and_fills_missing_ones(self) -> None:
        log = self.session_logs.create_session_log(title="Message IDs")
        self.session_logs.save_transcript(
            log["log_id"],
            [
                {"message_id": "user-1", "role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
            ],
        )

        ids = self.message_ids(log["log_id"])
        self.assertEqual(ids[0], "user-1")
        self.assertRegex(ids[1], r"^[0-9a-f]{32}$")
        self.assertEqual(
            [turn["message_id"] for turn in self.decrypt_transcript(log["log_id"])],
            ids,
        )

        for turns in (
            [{"message_id": "same", "role": "user", "content": "a"},
             {"message_id": "same", "role": "assistant", "content": "b"}],
            [{"message_id": "../escape", "role": "user", "content": "a"}],
        ):
            with self.subTest(turns=turns):
                with self.assertRaises(ValueError):
                    self.session_logs.save_transcript(log["log_id"], turns)
        self.assertEqual(self.message_ids(log["log_id"])[0], "user-1")

    def test_long_user_conversation_records_every_message(self) -> None:
        user_id = self.database.create_user()
        history = self.sage_history(50)

        for exchange in range(1, 51):
            saved = self.record(history[: exchange * 2], user_id=user_id)

        self.assertEqual(saved["turn_count"], 100)
        self.assertEqual(saved["source"], "user")
        turns = self.decrypt_transcript(saved["log_id"])
        self.assertEqual(len(turns), 100)
        self.assertEqual(turns[0]["content"], "Question 1")
        self.assertEqual(turns[-1]["content"], "Answer 50")
        self.assertEqual(turns[1]["message_id"], "m-1-a")
        with self.database.get_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM session_logs")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_feedback_stays_on_its_answer_as_the_conversation_grows(self) -> None:
        user_id = self.database.create_user()
        history = self.sage_history(55)
        log = self.record(history[:2], user_id=user_id)
        self.session_logs.set_message_feedback(
            log["log_id"], "m-1-a", "down", comment="Missed the deadline question."
        )

        for exchange in range(2, 56):
            self.record(history[: exchange * 2], user_id=user_id)

        feedback = self.session_logs.get_session_log(log["log_id"])["feedback"]
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["message_id"], "m-1-a")
        self.assertEqual(feedback[0]["turn_index"], 1)
        self.assertEqual(feedback[0]["rating"], "down")
        turns = self.decrypt_transcript(log["log_id"])
        self.assertEqual(turns[feedback[0]["turn_index"]]["content"], "Answer 1")

        self.session_logs.set_message_feedback(log["log_id"], "m-30-a", "up")
        by_message = {
            item["message_id"]: item
            for item in self.session_logs.get_session_log(log["log_id"])["feedback"]
        }
        self.assertEqual(by_message["m-1-a"]["rating"], "down")
        self.assertEqual(by_message["m-30-a"]["turn_index"], 59)

    def test_repeated_and_out_of_order_updates_never_shrink_the_record(self) -> None:
        user_id = self.database.create_user()
        history = self.sage_history(3)
        self.record(history[:4], user_id=user_id)
        latest = self.record(history, user_id=user_id)
        ciphertext = self.session_logs.get_session_log(latest["log_id"])["transcript_ciphertext"]

        repeated = self.record(history, user_id=user_id)
        late = self.record(history[:4], user_id=user_id)
        later_than_late = self.record(history[:2], user_id=user_id)

        for result in (repeated, late, later_than_late):
            self.assertEqual(result["log_id"], latest["log_id"])
            self.assertEqual(result["turn_count"], 6)
        detail = self.session_logs.get_session_log(latest["log_id"])
        self.assertEqual(detail["transcript_ciphertext"], ciphertext)

    def test_a_stale_writer_cannot_replace_a_newer_record(self) -> None:
        user_id = self.database.create_user()
        history = self.sage_history(3)
        log = self.record(history[:2], user_id=user_id)
        self.record(history, user_id=user_id)

        written = self.session_logs._write_transcript(
            log["log_id"],
            history[:4],
            through_sequence_id=4,
            expected_version=(2, 2),
        )

        self.assertFalse(written)
        self.assertEqual(self.session_logs.get_session_log_metadata(log["log_id"])["turn_count"], 6)

    def test_concurrent_record_update_retries_against_the_latest_record(self) -> None:
        user_id = self.database.create_user()
        history = self.sage_history(3)
        log = self.record(history[:2], user_id=user_id)
        original_write = self.session_logs._write_transcript
        interleaved = []

        def write_after_competitor(*args, **kwargs):
            if not interleaved:
                interleaved.append(True)
                original_write(log["log_id"], history[:4], through_sequence_id=4)
            return original_write(*args, **kwargs)

        with patch.object(self.session_logs, "_write_transcript", side_effect=write_after_competitor):
            saved = self.record(history, user_id=user_id)

        self.assertEqual(saved["turn_count"], 6)
        self.assertEqual(self.message_ids(log["log_id"])[-1], "m-3-a")

    def test_continuation_after_memory_deletion_archives_the_record_and_starts_a_new_one(self) -> None:
        user_id = self.database.create_user()
        log = self.record(self.sage_history(2), user_id=user_id)
        self.session_logs.set_message_feedback(log["log_id"], "m-2-a", "down")
        ciphertext = self.session_logs.get_session_log(log["log_id"])["transcript_ciphertext"]

        # Sage memory for the Conversation was deleted and the user kept chatting;
        # Sage numbers the new messages after every earlier one.
        continued = self.record(self.sage_history(1, prefix="after"), user_id=user_id, through=100)

        self.assertNotEqual(continued["log_id"], log["log_id"])
        self.assertEqual(continued["turn_count"], 2)
        self.assertEqual(continued["sage_session_id"], "sage-session")
        self.assertEqual(self.message_ids(continued["log_id"]), ["after-1-q", "after-1-a"])
        archived = self.session_logs.get_session_log(log["log_id"])
        self.assertEqual(archived["status"], "archived")
        self.assertIsNone(archived["sage_session_id"])
        self.assertEqual(archived["subject_user_id"], user_id)
        self.assertEqual(archived["turn_count"], 4)
        self.assertEqual(archived["transcript_ciphertext"], ciphertext)
        self.assertEqual(archived["feedback"][0]["message_id"], "m-2-a")
        self.assertEqual(archived["feedback"][0]["turn_index"], 3)

        # An update read before the deletion arrives late; the new record keeps
        # the newer history and nothing else changes.
        late = self.record(self.sage_history(2), user_id=user_id)
        self.assertEqual(late["log_id"], continued["log_id"])
        self.assertEqual(self.message_ids(continued["log_id"]), ["after-1-q", "after-1-a"])
        with self.database.get_cursor() as cursor:
            cursor.execute("SELECT SUM(turn_count) FROM session_logs WHERE source = 'user'")
            self.assertEqual(cursor.fetchone()[0], 6)

    def test_same_newest_message_with_more_history_is_newer(self) -> None:
        # Sage can commit messages out of sequence order, so a read may see the
        # newest message before an earlier one; the fuller read still wins.
        user_id = self.database.create_user()
        history = self.sage_history(2)
        first = self.record(history[:2] + history[3:], user_id=user_id, through=4)
        saved = self.record(history, user_id=user_id, through=4)
        self.assertEqual(saved["turn_count"], 4)
        self.assertEqual(saved["log_id"], first["log_id"])
        with self.database.get_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM session_logs")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_user_conversation_record_requires_message_ids_and_an_admin(self) -> None:
        user_id = self.database.create_user()
        with self.assertRaises(ValueError):
            self.record([{"role": "user", "content": "No ID"}], user_id=user_id)
        with self.assertRaises(ValueError):
            self.record([], user_id=user_id)

        self.clear_admins()
        with self.assertRaises(ValueError):
            self.record(self.sage_history(1), user_id=user_id)
        with self.database.get_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM session_logs")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_legacy_record_and_feedback_upgrade_to_message_ids(self) -> None:
        user_id = self.database.create_user()
        log = self.session_logs.create_session_log(
            source="user",
            subject_user_id=user_id,
            sage_session_id="legacy-session",
        )
        self.session_logs.save_transcript(
            log["log_id"],
            [
                {"role": "user", "content": "Old question"},
                {"role": "assistant", "content": "Old answer"},
            ],
        )
        # Rewind to the previous release's shape: metadata without message IDs
        # and feedback keyed by position.
        with self.database.get_cursor() as cursor:
            cursor.execute(
                "UPDATE session_logs SET turn_metadata_json = ? WHERE log_id = ?",
                (
                    json.dumps([
                        {"turn_index": 0, "role": "user"},
                        {"turn_index": 1, "role": "assistant"},
                    ]),
                    log["log_id"],
                ),
            )
            cursor.execute("DROP TABLE session_log_feedback")
            cursor.execute("""
                CREATE TABLE session_log_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    log_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    rating TEXT NOT NULL CHECK(rating IN ('up', 'down')),
                    comment_ciphertext TEXT,
                    comment_ephemeral_pubkey TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(log_id, turn_index),
                    FOREIGN KEY (log_id) REFERENCES session_logs(log_id) ON DELETE CASCADE
                )
            """)
            cursor.execute(
                "INSERT INTO session_log_feedback (log_id, turn_index, rating) VALUES (?, 1, 'up')",
                (log["log_id"],),
            )

        self.database._migrate_session_log_feedback_message_ids()
        self.database._migrate_session_log_feedback_message_ids()

        detail = self.session_logs.get_session_log(log["log_id"])
        self.assertEqual(
            detail["transcript_messages"],
            [
                {"message_id": "legacy-0", "role": "user"},
                {"message_id": "legacy-1", "role": "assistant"},
            ],
        )
        self.assertEqual(detail["feedback"][0]["message_id"], "legacy-1")
        self.assertEqual(detail["feedback"][0]["turn_index"], 1)
        self.session_logs.set_message_feedback(log["log_id"], "legacy-1", "down")
        self.assertEqual(self.session_logs.list_feedback(log["log_id"])[0]["rating"], "down")

        upgraded = self.record(
            self.sage_history(2),
            user_id=user_id,
            session="legacy-session",
        )

        self.assertEqual(upgraded["log_id"], log["log_id"])
        self.assertEqual(upgraded["turn_count"], 4)
        self.assertEqual(self.message_ids(log["log_id"])[0], "m-1-q")
        feedback = self.session_logs.list_feedback(log["log_id"])
        self.assertEqual(feedback[0]["message_id"], "legacy-1")
        self.assertIsNone(feedback[0]["turn_index"])

    def test_interrupted_feedback_migration_leaves_the_old_table_intact(self) -> None:
        log = self.session_logs.create_session_log(title="Interrupted migration")
        with self.database.get_cursor() as cursor:
            cursor.execute("DROP TABLE session_log_feedback")
            cursor.execute("""
                CREATE TABLE session_log_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    log_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    rating TEXT NOT NULL CHECK(rating IN ('up', 'down')),
                    comment_ciphertext TEXT,
                    comment_ephemeral_pubkey TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(log_id, turn_index),
                    FOREIGN KEY (log_id) REFERENCES session_logs(log_id) ON DELETE CASCADE
                )
            """)
            cursor.execute(
                "INSERT INTO session_log_feedback (log_id, turn_index, rating) VALUES (?, 3, 'up')",
                (log["log_id"],),
            )

        with patch.object(self.database, "_SESSION_LOG_FEEDBACK_TABLE_SQL", "CREATE TABLE broken ("):
            with self.assertRaises(self.database.sqlite3.Error):
                self.database._migrate_session_log_feedback_message_ids()

        connection = self.database.get_connection()
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("session_log_feedback_old", tables)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(session_log_feedback)")}
        self.assertIn("turn_index", columns)

        self.database._migrate_session_log_feedback_message_ids()
        rows = connection.execute("SELECT log_id, message_id, rating FROM session_log_feedback").fetchall()
        self.assertEqual([tuple(row) for row in rows], [(log["log_id"], "legacy-3", "up")])

    def test_feedback_migration_names_messages_by_their_recorded_ids(self) -> None:
        # A rollback stores feedback by position again; redeploying must put each
        # rating back on the message recorded at that position.
        user_id = self.database.create_user()
        log = self.record(self.sage_history(3), user_id=user_id)
        legacy = self.session_logs.create_session_log(title="Snapshot without IDs")
        self.session_logs.save_transcript(
            legacy["log_id"],
            [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        )
        with self.database.get_cursor() as cursor:
            cursor.execute(
                "UPDATE session_logs SET turn_metadata_json = ? WHERE log_id = ?",
                (json.dumps([{"turn_index": 0, "role": "user"}, {"turn_index": 1, "role": "assistant"}]),
                 legacy["log_id"]),
            )
            cursor.execute("DROP TABLE session_log_feedback")
            cursor.execute("""
                CREATE TABLE session_log_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    log_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    rating TEXT NOT NULL CHECK(rating IN ('up', 'down')),
                    comment_ciphertext TEXT,
                    comment_ephemeral_pubkey TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(log_id, turn_index),
                    FOREIGN KEY (log_id) REFERENCES session_logs(log_id) ON DELETE CASCADE
                )
            """)
            cursor.executemany(
                "INSERT INTO session_log_feedback (log_id, turn_index, rating) VALUES (?, ?, ?)",
                [(log["log_id"], 1, "down"), (log["log_id"], 5, "up"), (legacy["log_id"], 1, "up")],
            )

        self.database._migrate_session_log_feedback_message_ids()

        recorded = {(f["message_id"], f["turn_index"]) for f in self.session_logs.list_feedback(log["log_id"])}
        self.assertEqual(recorded, {("m-1-a", 1), ("m-3-a", 5)})
        self.assertEqual(
            [(f["message_id"], f["turn_index"]) for f in self.session_logs.list_feedback(legacy["log_id"])],
            [("legacy-1", 1)],
        )

    def test_feedback_saved_before_the_transcript_changed_is_not_attached(self) -> None:
        log = self.session_logs.create_session_log(source="user", title="Sliding snapshot")
        self.session_logs.save_transcript(
            log["log_id"],
            [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        )
        with self.database.get_cursor() as cursor:
            cursor.execute(
                "UPDATE session_logs SET turn_metadata_json = ?, completed_at = ? WHERE log_id = ?",
                (json.dumps([{"turn_index": 0, "role": "user"}, {"turn_index": 1, "role": "assistant"}]),
                 "2026-10-20T12:00:00Z", log["log_id"]),
            )
            cursor.execute("DROP TABLE session_log_feedback")
            cursor.execute("""
                CREATE TABLE session_log_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    log_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    rating TEXT NOT NULL CHECK(rating IN ('up', 'down')),
                    comment_ciphertext TEXT,
                    comment_ephemeral_pubkey TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(log_id, turn_index),
                    FOREIGN KEY (log_id) REFERENCES session_logs(log_id) ON DELETE CASCADE
                )
            """)
            # Rated before the snapshot was last rewritten, and after it.
            cursor.execute(
                "INSERT INTO session_log_feedback (id, log_id, turn_index, rating, updated_at) VALUES (7, ?, 1, 'down', ?)",
                (log["log_id"], "2026-10-19 09:00:00"),
            )

        self.database._migrate_session_log_feedback_message_ids()

        feedback = self.session_logs.list_feedback(log["log_id"])
        self.assertEqual(feedback[0]["message_id"], "legacy-unplaced-7")
        self.assertIsNone(feedback[0]["turn_index"])
        self.assertEqual(feedback[0]["rating"], "down")

    def test_archiving_a_record_does_not_detach_feedback_on_redeploy(self) -> None:
        # Rated, then archived (which does not change the transcript), then
        # rolled back to positions and redeployed.
        user_id = self.database.create_user()
        log = self.record(self.sage_history(2), user_id=user_id)
        with self.database.get_cursor() as cursor:
            cursor.execute(
                "UPDATE session_logs SET completed_at = ?, updated_at = ?, status = 'archived' WHERE log_id = ?",
                ("2026-10-20T10:00:00Z", "2026-10-20T12:00:00Z", log["log_id"]),
            )
            cursor.execute("DROP TABLE session_log_feedback")
            cursor.execute("""
                CREATE TABLE session_log_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    log_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    rating TEXT NOT NULL CHECK(rating IN ('up', 'down')),
                    comment_ciphertext TEXT,
                    comment_ephemeral_pubkey TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(log_id, turn_index),
                    FOREIGN KEY (log_id) REFERENCES session_logs(log_id) ON DELETE CASCADE
                )
            """)
            # Rated before the record was archived and before it last grew.
            cursor.execute(
                "INSERT INTO session_log_feedback (log_id, turn_index, rating, updated_at) VALUES (?, 3, 'up', ?)",
                (log["log_id"], "2026-10-20T09:00:00Z"),
            )

        self.database._migrate_session_log_feedback_message_ids()

        feedback = self.session_logs.list_feedback(log["log_id"])
        self.assertEqual([(f["message_id"], f["turn_index"]) for f in feedback], [("m-2-a", 3)])


if __name__ == "__main__":
    unittest.main()
