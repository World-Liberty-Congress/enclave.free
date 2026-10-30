import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("monitor", Path(__file__).with_name("monitor.py"))
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.marker = Path(self.temp.name) / "maintenance"
        self.now = datetime(2026, 10, 30, 14, tzinfo=timezone.utc).timestamp()
        self.marker_patch = patch.object(monitor, "MAINTENANCE_FILE", self.marker)
        self.marker_patch.start()
        self.addCleanup(self.marker_patch.stop)

    def snapshots(self, age=0):
        stamp = datetime.fromtimestamp(self.now - age, timezone.utc).isoformat()
        return [{"time": stamp, "tags": ["enclave:liberator"]}]

    def response(self):
        response = io.BytesIO(b'{"status":"healthy"}')
        response.status = 200
        return response

    def test_healthy_checks_inspect_offhost_complete_backup_api_and_search(self):
        with patch.object(monitor, "command_json", side_effect=[self.snapshots(), {"ok": True}]) as commands, patch.object(monitor.urllib.request, "urlopen", return_value=self.response()):
            self.assertEqual(monitor.check_failures(self.now), [])
        self.assertEqual(commands.call_args_list[0].args[0], ["restic", "--no-cache", "snapshots", "--json", "--tag", "enclave:liberator"])
        self.assertEqual(commands.call_args_list[1].args[0][:3], ["docker", "exec", "enclave-searxng"])

    def test_stale_backup_and_failing_search_are_detected_independently(self):
        with patch.object(monitor, "command_json", side_effect=[self.snapshots(31 * 3600), {"ok": False}]), patch.object(monitor.urllib.request, "urlopen", return_value=self.response()):
            self.assertEqual(monitor.check_failures(self.now), ["backup_stale", "search_unavailable"])

    def test_api_and_repository_errors_are_content_free(self):
        with patch.object(monitor, "command_json", side_effect=[RuntimeError("private repository credential"), {"ok": True}]), patch.object(monitor.urllib.request, "urlopen", side_effect=TimeoutError("private response")):
            self.assertEqual(monitor.check_failures(self.now), ["backup_repository_unavailable", "api_unhealthy"])

    def test_partial_or_unrelated_snapshots_cannot_satisfy_freshness(self):
        candidate = [{"time": datetime.fromtimestamp(self.now, timezone.utc).isoformat(), "tags": ["enclave-candidate:liberator"]}]
        with patch.object(monitor, "command_json", side_effect=[candidate, {"ok": True}]), patch.object(monitor.urllib.request, "urlopen", return_value=self.response()):
            self.assertEqual(monitor.check_failures(self.now), ["backup_repository_unavailable"])

    def test_maintenance_skips_expected_api_and_search_failures_but_not_backup_age(self):
        self.marker.touch()
        with patch.object(monitor, "command_json", return_value=self.snapshots(31 * 3600)) as commands, patch.object(monitor.urllib.request, "urlopen") as http:
            self.assertEqual(monitor.check_failures(self.marker.stat().st_mtime), ["backup_stale"])
        self.assertEqual(commands.call_count, 1)
        http.assert_not_called()

    def test_overdue_maintenance_is_detected(self):
        self.marker.touch()
        now = self.marker.stat().st_mtime + 901
        with patch.object(monitor, "command_json", return_value=[{"time": datetime.fromtimestamp(now, timezone.utc).isoformat(), "tags": ["enclave:liberator"]}]):
            self.assertEqual(monitor.check_failures(now), ["maintenance_overdue"])

    def test_only_three_consecutive_failures_of_the_same_check_trigger_alert(self):
        state = {}
        for index in range(3):
            state = monitor.record_check(state, ["api_unhealthy"], self.now + index * 300)
            self.assertEqual(state["status"], "failed" if index == 2 else "pending")
        recovered = monitor.record_check(state, [], self.now + 900)
        self.assertEqual(recovered["status"], "healthy")
        self.assertEqual(monitor.record_check(recovered, ["api_unhealthy"], self.now + 1200)["status"], "pending")
        self.assertEqual(monitor.record_check(state, ["search_unavailable"], self.now + 900)["status"], "pending")
        self.assertEqual(monitor.record_check(state, ["api_unhealthy"], self.now + 3600)["status"], "pending")

    def test_check_only_never_writes_state(self):
        directory = Path(self.temp.name) / "uncreated"
        with patch("sys.argv", ["monitor", "--check-only", "--state-dir", str(directory)]), patch.object(monitor, "check_failures", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.main(), 0)
        self.assertFalse(directory.exists())

    def test_persistent_counters_are_written_privately(self):
        directory = Path(self.temp.name) / "state"
        for _ in range(3):
            with patch("sys.argv", ["monitor", "--state-dir", str(directory)]), patch.object(monitor, "check_failures", return_value=["api_unhealthy"]), contextlib.redirect_stdout(io.StringIO()):
                result = monitor.main()
        self.assertEqual(result, 1)
        self.assertEqual(json.loads((directory / "status.json").read_text())["status"], "failed")
        self.assertEqual((directory / "status.json").stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
