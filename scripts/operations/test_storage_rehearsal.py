"""Opt-in integration rehearsal using dedicated disposable Docker containers."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import backup
import carryover
import test_operations


@unittest.skipUnless(os.getenv("ENCLAVE_RUN_STORAGE_REHEARSAL") == "1", "Requires dedicated rehearsal containers; see operations runbook")
class StorageRehearsal(unittest.TestCase):
    def test_retention_across_release_paths_preserves_other_snapshot_types(self):
        with tempfile.TemporaryDirectory(prefix="enclave-retention-rehearsal-") as temp:
            root = Path(temp)
            password = root / "password"
            password.write_text(os.urandom(32).hex())
            password.chmod(0o600)
            with patch.dict(os.environ, {"RESTIC_REPOSITORY": str(root / "repository"), "RESTIC_PASSWORD_FILE": str(password)}):
                backup.run(["restic", "init"])
                newest = datetime.now(timezone.utc).replace(hour=4, minute=0, second=0, microsecond=0)
                for day in range(35):
                    source = root / ("release-a" if day % 2 else "release-b")
                    source.write_text("invented retention fixture")
                    backup.run(["restic", "backup", "--tag", "enclave:liberator", "--host", "old-host" if day % 2 else "new-host",
                                "--time", (newest - timedelta(days=day)).strftime("%Y-%m-%d %H:%M:%S"), str(source)])
                for tag in ("enclave:liberator:candidate", "enclave:other", "liberator:release"):
                    backup.run(["restic", "backup", "--tag", tag, str(source)])
                before = json.loads(backup.run(["restic", "snapshots", "--json"]))
                backup.retention("liberator")
                self.assertEqual(len(json.loads(backup.run(["restic", "snapshots", "--json"]))), len(before))
                backup.retention("liberator", apply=True)
                after = json.loads(backup.run(["restic", "snapshots", "--json"]))
                retained = [s for s in after if s.get("tags") == ["enclave:liberator"]]
                self.assertLess(len(retained), 25, "Paths/hosts must not each receive their own 14-daily policy")
                retained_days = {datetime.fromisoformat(s["time"].replace("Z", "+00:00")).date() for s in retained}
                self.assertTrue({(newest - timedelta(days=n)).date() for n in range(14)} <= retained_days)
                # Calendar-week copies overlap daily copies; four weeks do not
                # imply that the oldest retained copy is 21 full days old.
                self.assertGreaterEqual(len({d.isocalendar()[:2] for d in retained_days}), 4)
                self.assertTrue(any(d <= (newest - timedelta(days=14)).date() for d in retained_days))
                for tag in ("enclave:liberator:candidate", "enclave:other", "liberator:release"):
                    self.assertEqual(sum(s.get("tags") == [tag] for s in after), 1)
                backup.run(["restic", "check", "--read-data"])

    def test_real_qdrant_and_postgres_replay(self):
        case = test_operations.CarryoverTests()
        case.setUp()
        try:
            url = "http://127.0.0.1:16333"
            postgres = "liberator-migration-postgres-fixture"
            # Containers are explicitly created for this test; never use a deployed stack.
            existing = carryover.request(url, "/collections")["collections"]
            self.assertEqual(existing, [], "Use a new empty Qdrant rehearsal container")
            carryover.request(url, "/collections/enclave_knowledge", {"vectors": case.index["vectors"]}, "PUT")
            carryover.request(url, "/collections/enclave_knowledge/points?wait=true", {"points": case.index["points"]}, "PUT")
            result = carryover.export_bundle(case.source, case.uploads, "/uploads", case.sage, url, case.bundle)
            self.assertEqual(result["knowledge_vector_count"], 1)
            output = case.root / "replay"
            carryover.replay(case.bundle, case.schema, output, case.destination())
            carryover.request(url, "/collections/enclave_knowledge", method="DELETE")
            self.assertEqual(carryover.apply_index(output, url)["point_count"], 1)
            # Applying into a populated index is refused rather than overwriting it.
            with self.assertRaises(carryover.MigrationError):
                carryover.apply_index(output, url)
            sql = (test_operations.ROOT / "runtime/sage/crates/sage-core/migrations/2026-04-12-000001_create_enclave_web_tables/up.sql").read_text()
            sql += "\nCREATE TABLE agents(id TEXT PRIMARY KEY); CREATE TABLE messages(id TEXT PRIMARY KEY);"
            subprocess.run(["docker", "exec", "-i", postgres, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres"], input=sql, text=True, capture_output=True, check=True)
            # The adapter reads POSTGRES_USER/DB exactly as a production container does.
            real_inspect = subprocess.check_output(["docker", "inspect", postgres], text=True)
            info = json.loads(real_inspect)
            info[0]["Config"]["Env"].extend(["POSTGRES_USER=postgres", "POSTGRES_DB=postgres"])
            original = subprocess.run
            def runner(args, **kwargs):
                if args == ["docker", "inspect", postgres]:
                    return subprocess.CompletedProcess(args, 0, stdout=json.dumps(info), stderr="")
                return original(args, **kwargs)
            with patch.object(carryover.subprocess, "run", side_effect=runner):
                self.assertEqual(carryover.apply_sage(output, postgres)["config_count"], 1)
                original(["docker", "exec", postgres, "psql", "-X", "-q", "-U", "postgres", "-c", "INSERT INTO agents VALUES('existing-history');"], capture_output=True, text=True, check=True)
                with self.assertRaises(subprocess.CalledProcessError):
                    carryover.apply_sage(output, postgres)
            carryover.request(url, "/collections/enclave_knowledge", method="DELETE")
        finally:
            case.tearDown()

    @unittest.skipUnless(shutil.which("restic"), "restic must be installed")
    def test_real_encrypted_backup_and_verified_restore(self):
        with tempfile.TemporaryDirectory(prefix="enclave-backup-rehearsal-") as temp:
            root = Path(temp)
            password = root / "restic-password"
            password.write_text("isolated-random-fixture-password-" + os.urandom(32).hex())
            password.chmod(0o600)
            environment = {"RESTIC_REPOSITORY": str(root / "encrypted-repository"), "RESTIC_PASSWORD_FILE": str(password)}
            with patch.dict(os.environ, environment):
                backup.run(["restic", "init"])
                backup.check_repository(allow_local=True)
                source = root / "source"
                source.mkdir()
                for name in ("sqlite", "postgres", "qdrant", "workspace", "uploads", "embedding-cache"):
                    (source / name).mkdir()
                    (source / name / "fixture.data").write_bytes((name + " private-data-canary").encode())
                with sqlite3.connect(source / "sqlite" / "enclave.db") as c:
                    c.execute("CREATE TABLE fixture(value TEXT)")
                    c.execute("INSERT INTO fixture VALUES('retained state')")
                config = root / "private.env"
                config.write_text("FAKE_SECRET=private-data-canary")
                calls = []
                containers = []
                for name in ("core-backend", "sage", "postgres", "qdrant", "frontend", "backend"):
                    containers.append({"Id": name, "Image": name + "-image", "State": {"Running": True}, "Config": {"Env": ["FAKE_SECRET=private-data-canary"], "Labels": {"com.docker.compose.service": name}}, "Mounts": [{"Type": "bind", "Source": str(source)}]})
                real_run = backup.run
                running = {c["Id"] for c in containers}
                def runner(args, **kwargs):
                    calls.append(args)
                    if args[:3] == ["docker", "image", "inspect"]:
                        return json.dumps([{"RepoDigests": ["fixture@sha256:fixture"]}])
                    if args[:3] == ["docker", "image", "save"]:
                        Path(args[args.index("--output") + 1]).write_bytes(b"fixture custom image archive")
                        return ""
                    if args[:2] == ["docker", "inspect"]:
                        return json.dumps([{"State": {"Running": name in running}} for name in args[2:]])
                    if args[:2] == ["docker", "stop"]:
                        running.difference_update(args[4:])
                    if args[:2] == ["docker", "start"]:
                        running.update(args[2:])
                    if args[0] == "docker":
                        return ""
                    return real_run(args, **kwargs)
                with patch.object(backup, "inspect_project", return_value=containers), patch.object(backup, "run", side_effect=runner):
                    result = backup.backup("fixture", [config], root / "state")
                snapshots = json.loads(real_run(["restic", "snapshots", "--json"]))
                self.assertEqual(len(snapshots), 1)
                self.assertEqual(snapshots[0]["tags"], ["enclave:fixture"])
                target = root / "isolated-restore"
                backup.restore(result["snapshot_id"], target)
                restored_source = target / str(source).lstrip("/")
                for name in ("sqlite", "postgres", "qdrant", "workspace", "uploads", "embedding-cache"):
                    self.assertEqual((restored_source / name / "fixture.data").read_bytes(), (source / name / "fixture.data").read_bytes())
                with sqlite3.connect(restored_source / "sqlite" / "enclave.db") as c:
                    self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(c.execute("SELECT value FROM fixture").fetchone()[0], "retained state")
                real_run(["restic", "check", "--read-data"])
                self.assertEqual(running, {c["Id"] for c in containers})
                manifest = json.loads((target / str(root / "state" / "recovery" / "runtime.json").lstrip("/")).read_text())
                self.assertEqual(set(manifest["images"]), {c["Image"] for c in containers})
                # Repository blobs must not contain the plaintext private-data canary.
                for p in (root / "encrypted-repository" / "data").rglob("*"):
                    if p.is_file():
                        self.assertNotIn(b"private-data-canary", p.read_bytes())


if __name__ == "__main__":
    unittest.main()
