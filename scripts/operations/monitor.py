#!/usr/bin/env python3
"""Content-free checks for stale backups, public API health, and working search."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

MAINTENANCE_FILE = Path("/run/enclave-backup/maintenance")
FAILURE_THRESHOLD = 3
BACKUP_MAX_AGE = 30 * 3600
MAINTENANCE_MAX_AGE = 15 * 60
SEARCH_PROBE = """import json,urllib.request
try:
 with urllib.request.urlopen('http://127.0.0.1:8080/search?q=human+rights&format=json&categories=general',timeout=12) as r:d=json.load(r)
 ok=any(isinstance(x,dict) and str(x.get('url','')).startswith(('https://','http://')) for x in d.get('results',[]))
 print(json.dumps({'ok':ok}))
except Exception:print(json.dumps({'ok':False}))
"""


def command_json(args: list[str], timeout: int) -> object:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True)
    return json.loads(result.stdout)


def check_failures(now: float) -> list[str]:
    failures = []
    try:
        snapshots = command_json([
            "restic", "--no-cache", "snapshots", "--json", "--tag", "enclave:liberator",
        ], 30)
        latest = max(datetime.fromisoformat(s["time"].replace("Z", "+00:00")).timestamp()
                     for s in snapshots if "enclave:liberator" in s.get("tags", []))
        if not -300 <= now - latest <= BACKUP_MAX_AGE:
            failures.append("backup_stale")
    except Exception:
        failures.append("backup_repository_unavailable")

    if MAINTENANCE_FILE.exists():
        if now - MAINTENANCE_FILE.stat().st_mtime > MAINTENANCE_MAX_AGE:
            failures.append("maintenance_overdue")
        return failures

    try:
        with urllib.request.urlopen("https://liberator.global/api/health", timeout=10) as response:
            if response.status != 200 or json.load(response).get("status") != "healthy":
                failures.append("api_unhealthy")
    except Exception:
        failures.append("api_unhealthy")
    try:
        if command_json(["docker", "exec", "enclave-searxng", "python3", "-c", SEARCH_PROBE], 20).get("ok") is not True:
            failures.append("search_unavailable")
    except Exception:
        failures.append("search_unavailable")
    return failures


def record_check(previous: dict, failures: list[str], now: float) -> dict:
    # A long gap between checks breaks a consecutive-failure sequence.
    prior_counts = previous.get("consecutive_failures", {}) if 0 <= now - previous.get("checked_at", 0) <= 900 else {}
    counts = {name: min(FAILURE_THRESHOLD, prior_counts.get(name, 0) + 1) for name in failures}
    alert = any(count >= FAILURE_THRESHOLD for count in counts.values())
    return {"checked_at": now, "status": "failed" if alert else "pending" if failures else "healthy",
            "checks_failed": failures, "consecutive_failures": counts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/enclave-monitor"))
    parser.add_argument("--check-only", action="store_true", help="Probe once without changing counters or sending notifications")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        now = time.time()
        failures = check_failures(now)
        if args.check_only:
            print(json.dumps({"checks_failed": failures, "status": "unhealthy" if failures else "healthy"}))
            return int(bool(failures))
        args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if args.state_dir.stat().st_mode & 0o077:
            raise ValueError("Monitor state must be private")
        path = args.state_dir / "status.json"
        previous = json.loads(path.read_text()) if path.exists() else {}
        status = record_check(previous, failures, now)
        stage = path.with_suffix(".tmp")
        stage.write_text(json.dumps(status) + "\n")
        stage.replace(path)
        print(json.dumps(status))
        return int(status["status"] == "failed")
    except Exception:
        # Exceptions may contain repository credentials or remote response data.
        print(json.dumps({"status": "monitor_error"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
