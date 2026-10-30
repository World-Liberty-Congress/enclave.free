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
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.env"
        self.config.write_text("fixture private config")
        self.state = self.root / "state"
        self.marker = self.root / "maintenance"
        self.environment = patch.dict(os.environ, RESTIC_REPOSITORY="s3:fixture.invalid/repo", RESTIC_PASSWORD_FILE=str(self.config))
        self.environment.start()
        self.containers = [{"Id": name, "Image": "fixture-image", "State": {"Running": name != "stopped"},
                            "Config": {"Labels": {"com.docker.compose.service": name}}, "Mounts": []}
                           for name in ("core-backend", "sage", "postgres", "qdrant", "frontend", "backend", "stopped")]
        self.running = {c["Id"] for c in self.containers if c["State"]["Running"]}
        self.original = self.running.copy()
        self.calls = []
        self.failure = None
        self.signal_phase = None
        self.signalled = False
        self.stop_finished = False

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def interrupt(self, phase):
        if phase == self.signal_phase and not self.signalled:
            self.signalled = True
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"RepoDigests": ["fixture@sha256:fixture"]}])
        if args[:2] == ["docker", "stop"]:
            self.assertTrue(self.marker.exists())
            self.assertNotIn("frontend", args[4:])
            self.interrupt("stop")
            self.running.difference_update(args[4:])
            self.stop_finished = True
        if args[:2] == ["docker", "start"]:
            self.running.update(args[2:])
            if self.failure == "recovery" and "core-backend" in args[2:]:
                self.running.remove("core-backend")
            self.interrupt("recovery-start")
        if args[:2] == ["docker", "inspect"]:
            if any(c[:2] == ["docker", "start"] for c in self.calls):
                self.interrupt("recovery-health")
            return json.dumps([{"State": {"Running": name in self.running}} for name in args[2:]])
        if args[0] == "restic":
            local = args[1:2] == ["--repo"]
            command = args[3] if local else args[1]
            if command == "backup":
                self.assertTrue(local, "The capture must not use the network repository")
                self.assertEqual(self.running, {"frontend"})
                self.interrupt("capture")
                if self.failure == "capture":
                    raise backup.BackupError("fixture capture failure")
                return json.dumps({"message_type": "summary", "snapshot_id": "local-checkpoint"})
            if command == "copy":
                self.assertEqual(self.running, self.original)
                self.assertFalse(self.marker.exists())
                self.interrupt("upload")
                if self.failure == "upload":
                    raise backup.BackupError("fixture upload failure")
            if command == "snapshots":
                return "[]" if local else json.dumps([{"id": "remote-candidate", "original": "local-checkpoint"}])
            if command == "tag":
                return json.dumps({"old_snapshot_id": "remote-candidate", "new_snapshot_id": "complete"})
        return ""

    def capture(self):
        with patch.object(backup, "inspect_project", return_value=self.containers), patch.object(backup, "run", side_effect=self.runner):
            return backup.backup("fixture", [self.config], self.state, self.marker)

    def test_offhost_upload_starts_only_after_recovery_and_frontend_stays_online(self):
        result = self.capture()
        self.assertEqual(result["snapshot_id"], "complete")
        self.assertGreaterEqual(result["pause_seconds"], 0)
        self.assertEqual(self.running, self.original)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.state / "recovery").exists())
        stops = [a for a in self.calls if a[:2] == ["docker", "stop"]]
        self.assertEqual(set(stops[0][4:]), {"core-backend", "sage", "backend"})
        self.assertEqual(set(stops[1][4:]), {"postgres", "qdrant"})

    def test_scheduled_backup_preserves_sage_shutdown_allowance(self):
        self.capture()
        sage_stop = next(a for a in self.calls if a[:2] == ["docker", "stop"] and "sage" in a[4:])
        self.assertGreaterEqual(int(sage_stop[3]), 120, "Sage's 90-second drain must finish before Docker can kill it")

    def test_capture_and_upload_failures_recover_without_completing_backup(self):
        for phase in ("capture", "upload"):
            with self.subTest(phase=phase):
                self.calls.clear()
                self.failure = phase
                with self.assertRaisesRegex(backup.BackupError, f"fixture {phase} failure"):
                    self.capture()
                self.assertEqual(self.running, self.original)
                self.assertFalse(self.marker.exists())
                self.assertFalse((self.state / "recovery").exists())
                self.assertFalse(any(a[:2] == ["restic", "tag"] for a in self.calls))

    def test_interruption_finishes_recovery_and_never_reports_complete(self):
        previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        for phase in ("stop", "capture", "recovery-start", "recovery-health", "upload"):
            with self.subTest(phase=phase):
                self.calls.clear()
                self.signalled = False
                self.stop_finished = False
                self.signal_phase = phase
                with self.assertRaisesRegex(backup.BackupError, "Backup interrupted"):
                    self.capture()
                self.assertTrue(self.signalled)
                self.assertTrue(self.stop_finished)
                self.assertEqual(self.running, self.original)
                self.assertFalse(self.marker.exists())
                self.assertFalse(any(a[:2] == ["restic", "tag"] for a in self.calls))
                self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)

    def test_local_pruning_can_reclaim_capture_space_before_preflight(self):
        def space_check(*args):
            self.assertTrue(any("forget" in c and "--prune" in c for c in self.calls))
            self.assertFalse(any(c[:2] == ["docker", "stop"] for c in self.calls))
        with patch.object(backup, "check_capture_space", side_effect=space_check):
            self.capture()

    def test_terminal_interrupt_does_not_kill_outstanding_child_command(self):
        program = """
import os, signal, sys, threading
import backup
interrupted = []
signal.signal(signal.SIGINT, lambda *_: interrupted.append(True))
sender = threading.Timer(.3, lambda: os.killpg(os.getpgrp(), signal.SIGINT))
sender.start()
output = backup.run([sys.executable, '-c', 'import time; time.sleep(.6); print("completed")'])
sender.join()
assert interrupted and output.strip() == 'completed'
"""
        result = subprocess.run([sys.executable, "-c", program], cwd=ROOT / "scripts/operations",
                                capture_output=True, text=True, start_new_session=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_recovery_keeps_maintenance_and_skips_upload(self):
        self.failure = "recovery"
        with self.assertRaisesRegex(backup.BackupError, "Service recovery incomplete"):
            self.capture()
        self.assertTrue(self.marker.exists())
        self.assertIn("backend", self.running, "Remaining recovery tiers must still be attempted")
        self.assertFalse(any(a[:2] == ["restic", "copy"] for a in self.calls))
        with self.assertRaisesRegex(backup.BackupError, "existing maintenance marker"):
            self.capture()

    def test_preflight_rejects_writable_frontend_and_full_disk_before_stopping(self):
        self.containers[4]["Mounts"] = [{"Type": "bind", "Source": str(self.config), "RW": True}]
        with self.assertRaisesRegex(backup.BackupError, "writable persistent storage"):
            self.capture()
        self.containers[4]["Mounts"] = []
        with patch.object(backup.shutil, "disk_usage", return_value=type("Disk", (), {"free": 0})()):
            with self.assertRaisesRegex(backup.BackupError, "Insufficient local checkpoint space"):
                self.capture()
        self.assertFalse(any(a[:2] == ["docker", "stop"] for a in self.calls))
        self.assertFalse(self.marker.exists())

    def test_installed_unit_and_notification_override_both_enable_maintenance(self):
        for name in ("enclave-backup.service", "enclave-backup.service.d/notifications.conf"):
            command = next(line for line in (ROOT / "scripts/operations" / name).read_text().splitlines()
                           if line.startswith("ExecStart=/"))
            self.assertIn("--maintenance-file /run/enclave-backup/maintenance", command)
            self.assertIn("--config-path /etc/systemd/journald.conf.d/enclave-retention.conf", command)

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
