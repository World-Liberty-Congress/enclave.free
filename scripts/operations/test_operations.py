"""Migration data-boundary, encryption, and backup failure-path tests."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "app"))
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("SECRET_KEY", "fixture-auth-secret-" * 3)
import database
import content_artifacts
import deployment_config
import backup
import carryover


class CarryoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"SECRET_KEY": "new-auth-key-" * 4, "CONTENT_ENCRYPTION_KEY": "new-content-key-" * 4, "SOURCE_CONTENT_ENCRYPTION_KEY": ""})
        self.env.start()
        self.key = self.root / "bundle.key"
        self.key.write_text("ab" * 32)
        self.key.chmod(0o600)
        os.environ["MIGRATION_BUNDLE_KEY_FILE"] = str(self.key)
        self.source = self.root / "source.db"
        self.schema = self.root / "schema.db"
        for p in (self.source, self.schema):
            if database._connection is not None:
                database._connection.close()
            database._connection = None
            database.SQLITE_PATH = str(p)
            database.init_schema()
            database.seed_default_settings()
            deployment_config._sync_env_to_db()
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        (self.uploads / "current.txt").write_bytes(b"Curated organization knowledge")
        with sqlite3.connect(self.source) as c:
            c.execute("INSERT INTO user_types(id,name) VALUES(7,'Organization member')")
            c.execute("INSERT INTO user_field_definitions(field_name,field_type,user_type_id) VALUES('country','text',7)")
            c.execute("INSERT INTO users(id,email) VALUES(99,'DO_NOT_CARRY_USER_HISTORY')")
            c.execute("INSERT INTO admin_test_provenance(user_id,pubkey,provisioning_type_id) VALUES(99,'DO_NOT_CARRY_TEST_IDENTITY',7)")
            c.execute("INSERT INTO admins(pubkey) VALUES('DO_NOT_CARRY_ADMIN_IDENTITY')")
            c.execute("UPDATE deployment_config SET value='DO_NOT_CARRY_SOURCE_SECRET',is_secret=1 WHERE key='LLM_API_KEY'")
            for job, filename, current in (("current", "current.txt", 1), ("old", "missing.txt", 0)):
                c.execute("INSERT INTO ingest_jobs(job_id,filename,file_path,status,ontology_id,is_current) VALUES(?,?,?,'completed','default',?)", (job, filename, "/uploads/" + filename, current))
                c.execute("INSERT INTO retrieval_chunks(chunk_id,job_id,chunk_index,source_file,char_count,encrypted_text,created_at,updated_at) VALUES(?,?,0,?,4,?,'2026-01-01','2026-01-01')", (job + "_chunk_0000", job, filename, job + " organization text"))
                c.execute("INSERT INTO document_defaults(job_id) VALUES(?)", (job,))
        self.sage = {"ai_config": [{"key": "prompt_rules", "value": "Curated rules with 'quotes' and \\slashes", "value_type": "string", "category": "prompt", "description": None, "updated_at": "2026-01-01T00:00:00Z"}], "ai_config_user_type_overrides": [{"id": "00000000-0000-0000-0000-000000000001", "ai_config_key": "prompt_rules", "user_type_id": 7, "value": "Type-specific rule", "updated_at": "2026-01-01T00:00:00Z"}]}
        self.index = {"vectors": {"size": 3, "distance": "Cosine"}, "points": [{"id": "00000000-0000-0000-0000-000000000002", "vector": [1.0, 0.0, 0.0], "payload": {"chunk_id": "current_chunk_0000", "job_id": "current", "type": "chunk", "source_file": "current.txt"}}]}
        self.bundle = self.root / "carryover.encrypted"

    def tearDown(self):
        if database._connection is not None:
            database._connection.close()
            database._connection = None
        self.env.stop()
        self.temp.cleanup()

    def export(self):
        with patch.object(carryover, "collect_index", return_value=self.index):
            return carryover.export_bundle(self.source, self.uploads, "/uploads", self.sage, "http://unused", self.bundle)

    def destination(self):
        with carryover.open_bundle(self.bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        result = {key: "" for key in manifest["required_destination_keys"]}
        result.update(CONTENT_ENCRYPTION_KEY=os.environ["CONTENT_ENCRYPTION_KEY"], FRONTEND_URL="https://liberator.invalid", LLM_API_KEY="organization-fixture-key", EMBEDDING_API_KEY="organization-fixture-key", SMTP_USER="fixture-organization-username", SMTP_PASS="fixture-organization-password")
        return result

    def test_replay_preserves_knowledge_and_settings_without_users_or_source_secrets(self):
        exported = self.export()
        self.assertEqual(exported["absent_inactive_artifact_count"], 1)
        self.assertNotIn(b"Curated", self.bundle.read_bytes())
        with carryover.open_bundle(self.bundle) as archive:
            raw = archive.read("tables.json")
            self.assertNotIn(b"DO_NOT_CARRY", raw)
            self.assertNotIn("users", json.loads(raw))
            self.assertNotIn("admin_test_provenance", json.loads(raw))
        output = self.root / "replay"
        result = carryover.replay(self.bundle, self.schema, output, self.destination())
        self.assertEqual(result["excluded_identity_and_history_rows"], 0)
        self.assertEqual(result["retained_counts"]["retrieval_chunks"], 2)
        raw_artifact = next((output / "uploads" / "migrated").iterdir()).read_bytes()
        # Verify with the application's actual reader, not just this tool's implementation.
        database._connection.close()
        database._connection = None
        database.SQLITE_PATH = str(output / "enclave.db")
        database._deployment_secret_key = None
        import auth
        auth.SECRET_KEY = os.environ["SECRET_KEY"]
        self.assertEqual(content_artifacts.decrypt_bytes(raw_artifact), b"Curated organization knowledge")
        self.assertEqual(database.get_deployment_config_value("LLM_API_KEY"), "organization-fixture-key")
        with sqlite3.connect(output / "enclave.db") as c:
            encrypted = c.execute("SELECT encrypted_text FROM retrieval_chunks WHERE job_id='old'").fetchone()[0]
            self.assertEqual(content_artifacts.decrypt_bytes(encrypted.encode()), b"old organization text")
            self.assertEqual(c.execute("SELECT name FROM user_types WHERE id=7").fetchone()[0], "Organization member")
            self.assertEqual(c.execute("SELECT count(*) FROM admins").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM admin_test_provenance").fetchone()[0], 0)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_replay_rejects_existing_destination_history(self):
        self.export()
        with sqlite3.connect(self.schema) as c:
            c.execute("INSERT INTO users(email) VALUES('existing')")
        with self.assertRaises(carryover.MigrationError):
            carryover.replay(self.bundle, self.schema, self.root / "replay", self.destination())
        self.assertFalse((self.root / "replay").exists())

    def test_bundle_tampering_is_rejected_before_destination_creation(self):
        self.export()
        destination = self.destination()
        raw = bytearray(self.bundle.read_bytes())
        raw[-20] ^= 1
        self.bundle.write_bytes(raw)
        with self.assertRaises(ValueError):
            carryover.replay(self.bundle, self.schema, self.root / "replay", destination)
        self.assertFalse((self.root / "replay").exists())

    def test_export_rejects_missing_active_upload_and_unknown_tables(self):
        (self.uploads / "current.txt").unlink()
        with self.assertRaises(carryover.MigrationError):
            self.export()
        self.assertFalse(self.bundle.exists())
        with sqlite3.connect(self.source) as c:
            c.execute("CREATE TABLE new_data_class(id INTEGER)")
        with self.assertRaises(carryover.MigrationError):
            self.export()

    def test_destination_overrides_required(self):
        self.export()
        with self.assertRaises(carryover.MigrationError):
            carryover.replay(self.bundle, self.schema, self.root / "replay", {})

    def test_verification_detects_changed_settings_and_missing_active_vectors(self):
        self.export()
        output = self.root / "replay"
        carryover.replay(self.bundle, self.schema, output, self.destination())
        with sqlite3.connect(output / "enclave.db") as c:
            c.execute("UPDATE user_types SET name='Unexpected changed setting' WHERE id=7")
        with self.assertRaises(carryover.MigrationError):
            carryover.verify_replay(output)
        def empty_index(base, path, body=None, method=None):
            if path.endswith("scroll"):
                return {"points": [], "next_page_offset": None}
            return {"config": {"params": {"vectors": self.index["vectors"]}}}
        with patch.object(carryover, "request", side_effect=empty_index), self.assertRaises(carryover.MigrationError):
            carryover.collect_index("http://unused", {"current_chunk_0000": {"job_id": "current"}}, {"current_chunk_0000"})

    def test_export_does_not_overwrite_an_existing_bundle(self):
        self.bundle.write_bytes(b"existing export")
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(self.bundle.read_bytes(), b"existing export")

    def test_source_encryption_is_decrypted_and_new_key_used(self):
        old_key = "old-source-content-key-" * 3
        os.environ["SOURCE_CONTENT_ENCRYPTION_KEY"] = old_key
        (self.uploads / "current.txt").write_bytes(carryover.encrypt_artifact(b"Curated organization knowledge", old_key))
        with sqlite3.connect(self.source) as c:
            c.execute("UPDATE retrieval_chunks SET encrypted_text=? WHERE job_id='current'", (carryover.encrypt_artifact(b"current organization text", old_key).decode(),))
        self.export()
        output = self.root / "replay"
        carryover.replay(self.bundle, self.schema, output, self.destination())
        self.assertEqual(carryover.verify_replay(output)["encrypted_artifact_count"], 1)
        with self.assertRaises(ValueError):
            carryover.decrypt_artifact(next((output / "uploads" / "migrated").iterdir()).read_bytes(), old_key)

    def test_live_index_rejects_unclassified_payload(self):
        def response(base, path, body=None, method=None):
            if path.endswith("scroll"):
                point = dict(self.index["points"][0])
                point["payload"] = dict(point["payload"], user_id="DO_NOT_CARRY")
                return {"points": [point], "next_page_offset": None}
            return {"config": {"params": {"vectors": self.index["vectors"]}}}
        with patch.object(carryover, "request", side_effect=response), self.assertRaises(carryover.MigrationError):
            carryover.collect_index("http://unused", {"current_chunk_0000": {"job_id": "current"}}, {"current_chunk_0000"})


class BackupTests(unittest.TestCase):
    def test_interruption_finishes_all_recovery_tiers_and_reports_failure(self):
        for phase in ("snapshot", "recovery-start", "recovery-health"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config = root / "config.env"
                config.write_text("fixture config")
                state = root / "backup-state"
                containers = [{"Id": service, "Image": "fixture-image", "State": {"Running": True},
                               "Config": {"Labels": {"com.docker.compose.service": service}}, "Mounts": []}
                              for service in ("postgres", "sage", "backend")]
                started = []
                ready = set()
                stopped = False
                signalled = False
                previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}

                def interrupt():
                    nonlocal signalled
                    signalled = True
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                    signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

                def runner(args, **kwargs):
                    nonlocal stopped
                    if args[:3] == ["docker", "image", "inspect"]:
                        return json.dumps([{"RepoDigests": []}])
                    if args[:2] == ["docker", "stop"]:
                        stopped = True
                    if args[:2] == ["docker", "start"]:
                        for service in args[2:]:
                            dependency = {"sage": "postgres", "backend": "sage"}.get(service)
                            if dependency and dependency not in ready:
                                raise backup.BackupError("fixture dependency not ready")
                        started.extend(args[2:])
                        if phase == "recovery-start" and not signalled:
                            interrupt()
                    if args[:2] == ["docker", "inspect"]:
                        if phase == "recovery-health" and started and not signalled:
                            interrupt()
                        ready.update(service for service in args[2:] if service in started)
                        return json.dumps([{"State": {"Running": not stopped or service in started}}
                                           for service in args[2:]])
                    if args[:2] == ["restic", "backup"]:
                        if phase == "snapshot":
                            interrupt()
                        return json.dumps({"message_type": "summary", "snapshot_id": "candidate"})
                    if args[:2] == ["restic", "tag"]:
                        return json.dumps({"old_snapshot_id": "candidate", "new_snapshot_id": "complete"})
                    return ""

                with patch.object(backup, "inspect_project", return_value=containers), patch.object(backup, "run", side_effect=runner):
                    with self.assertRaisesRegex(backup.BackupError, "Backup interrupted"):
                        backup.backup("fixture", [config], state)
                self.assertTrue(signalled)
                self.assertEqual(started, ["postgres", "sage", "backend"])
                self.assertEqual(ready, {"postgres", "sage", "backend"})
                self.assertFalse((state / "recovery").exists())
                self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)

    def test_retention_rejects_a_project_that_could_select_multiple_tag_sets(self):
        with patch.object(backup, "run") as runner, self.assertRaises(backup.BackupError):
            backup.retention("liberator,other", apply=True)
        runner.assert_not_called()

    def test_retention_does_not_report_success_after_a_repository_check_fails(self):
        def runner(args, **kwargs):
            if args == ["restic", "check"]:
                raise backup.BackupError("fixture repository check failure")
            return ""
        with patch.object(backup, "run", side_effect=runner), self.assertRaisesRegex(backup.BackupError, "repository check failure"):
            backup.retention("liberator", apply=True)

    def test_legacy_wrappers_reject_ambiguous_positional_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "existing-state"
            marker.write_text("must remain unchanged")
            for command in ("backup", "restore"):
                result = subprocess.run(["bash", str(ROOT / "scripts" / (command + ".sh")), temp], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(marker.read_text(), "must remain unchanged")

    def test_backup_restarts_original_services_if_upload_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.env"
            config.write_text("private test config")
            mount = root / "data"
            mount.mkdir()
            state = root / "backup-state"
            calls = []
            running = True
            container = {"Id": "original-running", "Image": "image-id", "State": {"Running": True}, "Config": {"Labels": {"com.docker.compose.service": "sage"}}, "Mounts": [{"Type": "bind", "Source": str(mount)}]}
            def runner(args, **kwargs):
                nonlocal running
                calls.append(args)
                if args[:3] == ["docker", "image", "inspect"]:
                    return json.dumps([{"RepoDigests": []}])
                if args[:2] == ["docker", "stop"]:
                    running = False
                if args[:2] == ["docker", "start"]:
                    running = True
                if args[:2] == ["docker", "inspect"]:
                    return json.dumps([{"State": {"Running": running}}])
                if args[:2] == ["restic", "backup"]:
                    raise backup.BackupError("fixture upload failure")
                return ""
            with patch.object(backup, "inspect_project", return_value=[container]), patch.object(backup, "run", side_effect=runner), self.assertRaisesRegex(backup.BackupError, "fixture upload failure"):
                backup.backup("fixture", [config], state)
            self.assertIn(["docker", "start", "original-running"], calls)
            self.assertLess(next(i for i,a in enumerate(calls) if a[:2] == ["docker", "stop"]), next(i for i,a in enumerate(calls) if a[:2] == ["restic", "backup"]))
            self.assertFalse((state / "recovery").exists())

    def test_recovery_waits_for_dependencies_and_keeps_stopped_services_stopped(self):
        containers = [{"Id": service, "State": {"Running": service != "stopped"},
                       "Config": {"Labels": {"com.docker.compose.service": service}}}
                      for service in ("backend", "sage", "postgres", "stopped")]
        ready = set()
        starting = set()
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if args[:2] == ["docker", "start"]:
                for service in args[2:]:
                    dependency = {"sage": "postgres", "backend": "sage"}.get(service)
                    if dependency and dependency not in ready:
                        raise backup.BackupError("dependency not ready")
                    starting.add(service)
                return ""
            result = []
            for service in args[2:]:
                status = "healthy" if service in ready else "starting"
                result.append({"State": {"Running": service in starting, "Health": {"Status": status}}})
                ready.add(service)
            return json.dumps(result)
        with patch.object(backup, "run", side_effect=runner), patch.object(backup.time, "sleep"):
            backup.restart_original(containers)
        self.assertEqual(ready, {"postgres", "sage", "backend"})
        self.assertNotIn("stopped", starting)

    def test_failed_recovery_attempts_remaining_services_and_reports_failure(self):
        containers = [{"Id": service, "State": {"Running": True},
                       "Config": {"Labels": {"com.docker.compose.service": service}}}
                      for service in ("postgres", "sage", "backend")]
        started = []
        def runner(args, **kwargs):
            if args[:2] == ["docker", "start"]:
                started.extend(args[2:])
                return ""
            return json.dumps([{"State": {"Running": args[2] != "sage"}}])
        with patch.object(backup, "run", side_effect=runner), self.assertRaisesRegex(backup.BackupError, "Service recovery incomplete"):
            backup.restart_original(containers)
        self.assertEqual(started, ["postgres", "sage", "backend"])

    def test_recovery_timeout_is_reported(self):
        with patch.object(backup, "run", return_value=json.dumps([{"State": {"Running": True, "Health": {"Status": "starting"}}}])), self.assertRaisesRegex(backup.BackupError, "did not become healthy"):
            backup.wait_ready(["slow-service"], timeout=0)

    def test_restore_never_overwrites_existing_target(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(backup, "run") as runner, self.assertRaises(backup.BackupError):
                backup.restore("snapshot", Path(temp))
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
