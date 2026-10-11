#!/usr/bin/env python3
"""Consistent, encrypted backups of the current Compose stack (Linux host)."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone


REQUIRED_SERVICES = {"core-backend", "sage", "postgres", "qdrant", "frontend", "backend"}


class BackupError(RuntimeError):
    pass


def run(args: list[str], **kwargs) -> str:
    # Runtime env and command errors can contain credentials. Keep them private.
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise BackupError(f"{args[0]} operation failed (exit {result.returncode}); no successful backup reported")
    return result.stdout


def inspect_project(project: str) -> list[dict]:
    ids = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]).split()
    if not ids:
        raise BackupError("Compose project has no containers")
    containers = json.loads(run(["docker", "inspect", *ids]))
    services = {c["Config"]["Labels"].get("com.docker.compose.service") for c in containers}
    if not REQUIRED_SERVICES <= services:
        raise BackupError("Compose project is missing required application/storage containers")
    return containers


def source_paths(containers: list[dict], config_paths: list[Path]) -> list[Path]:
    paths = set(config_paths)
    for c in containers:
        for mount in c["Mounts"]:
            if mount["Type"] in {"bind", "volume"}:
                paths.add(Path(mount["Source"]))
            elif mount["Type"] != "tmpfs":
                raise BackupError("Unsupported persistent mount type")
        files = c["Config"]["Labels"].get("com.docker.compose.project.config_files", "")
        paths.update(Path(p) for p in files.split(",") if p)
    for p in paths:
        if not p.is_absolute() or not p.exists() or p.is_symlink() or str(p) in {"/", "/opt", "/root", "/var", "/var/lib/docker"}:
            raise BackupError("Backup source is missing, indirect, or too broad")
    return sorted(paths)


def check_repository(allow_local: bool) -> None:
    repo = os.getenv("RESTIC_REPOSITORY", "")
    if not repo or not os.getenv("RESTIC_PASSWORD_FILE"):
        raise BackupError("RESTIC_REPOSITORY and RESTIC_PASSWORD_FILE are required")
    password = Path(os.environ["RESTIC_PASSWORD_FILE"])
    if not password.is_file() or password.stat().st_mode & 0o077:
        raise BackupError("Restic password file must exist and have private permissions")
    if not allow_local and not repo.startswith(("s3:", "sftp:", "rest:", "azure:", "gs:", "b2:")):
        raise BackupError("Production backups require an off-host repository")
    run(["restic", "snapshots", "--json"])


def backup(project: str, config_paths: list[Path], state_dir: Path) -> dict:
    containers = inspect_project(project)
    paths = source_paths(containers, config_paths)
    original_running = [c["Id"] for c in containers if c["State"]["Running"]]
    if not original_running:
        raise BackupError("No running services; inspect the deployment before backing it up")
    # Custom images must survive account/host loss independently of local tags.
    custom_images = sorted({c["Image"] for c in containers if c["Config"]["Labels"].get(
        "com.docker.compose.service"
    ) in {"core-backend", "sage", "frontend"} or not json.loads(
        run(["docker", "image", "inspect", c["Image"]])
    )[0].get("RepoDigests")})
    manifest = {
        "format_version": 1,
        "project": project,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "consistency": "all originally running project containers stopped for entire snapshot",
        "sources": [str(p) for p in paths],
        "containers": containers,  # Contains secrets: encrypted repository only.
    }
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state_dir.stat().st_mode & 0o077:
        raise BackupError("Backup state directory must have private permissions")
    for p in paths:
        if state_dir.resolve() == p.resolve() or state_dir.resolve().is_relative_to(p.resolve()):
            raise BackupError("Backup state directory cannot be inside a backup source")
    with (state_dir / "backup.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("Another backup is running") from None
        # Stable stage path allows image archives and manifest to deduplicate.
        stage = state_dir / "recovery"
        stage.mkdir(mode=0o700, exist_ok=False)
        interrupted = False

        def on_signal(signum, frame):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise BackupError("Backup interrupted; restarting original services")

        previous = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
        stopping_started = False
        try:
            (stage / "runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
            if custom_images:
                run(["docker", "image", "save", "--output", str(stage / "custom-images.tar"), *custom_images])
            stopping_started = True
            run(["docker", "stop", "--time", "60", *original_running])
            current = json.loads(run(["docker", "inspect", *original_running]))
            if any(c["State"]["Running"] for c in current):
                raise BackupError("Could not quiesce all application/storage writers")
            candidate_tag = f"enclave:{project}:candidate"
            output = run(["restic", "backup", "--json", "--tag", candidate_tag, *map(str, paths), str(stage)])
            summaries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
            summary = next((s for s in summaries if s.get("message_type") == "summary" and s.get("snapshot_id")), None)
            if not summary:
                raise BackupError("Restic did not report a complete snapshot")
            # Partial restic snapshots must never displace a complete daily copy.
            tagged = run(["restic", "tag", "--json", "--remove", candidate_tag, "--add", f"enclave:{project}", summary["snapshot_id"]])
            changes = [json.loads(line) for line in tagged.splitlines() if line.startswith("{")]
            changed = next((r for r in changes if r.get("old_snapshot_id") == summary["snapshot_id"] and r.get("new_snapshot_id")), None)
            if not changed:
                raise BackupError("Complete snapshot tag change did not report a new snapshot ID")
            return {"project": project, "snapshot_id": changed["new_snapshot_id"], "source_count": len(paths), "custom_image_count": len(custom_images)}
        finally:
            try:
                # Includes containers stopped before docker stop was interrupted.
                if stopping_started:
                    run(["docker", "start", *original_running])
            finally:
                for s, handler in previous.items():
                    signal.signal(s, handler)
                shutil.rmtree(stage)


def restore(snapshot: str, target: Path) -> dict:
    # Restore is deliberately isolated: never overwrite live database paths.
    if target.exists():
        raise BackupError("Restore target must be a new directory")
    target.mkdir(mode=0o700, parents=True)
    run(["restic", "restore", snapshot, "--target", str(target), "--verify"])
    return {"snapshot_id": snapshot, "isolated_restore": str(target), "status": "restored and verified; application startup drill still required"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-repository", action="store_true", help="Only for isolated tooling tests")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--project", required=True)
    b.add_argument("--config-path", action="append", type=Path, required=True, help="Private env/key/TLS config path; repeat as needed")
    b.add_argument("--state-dir", type=Path, default=Path("/var/lib/enclave-backup"))
    r = sub.add_parser("restore")
    r.add_argument("--snapshot", required=True)
    r.add_argument("--target", type=Path, required=True)
    sub.add_parser("check")
    f = sub.add_parser("retention-preview")
    f.add_argument("--project", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        check_repository(args.allow_local_repository)
        if args.command == "backup":
            result = backup(args.project, args.config_path, args.state_dir)
        elif args.command == "restore":
            result = restore(args.snapshot, args.target)
        elif args.command == "check":
            run(["restic", "check", "--read-data"])
            result = {"status": "repository contents verified"}
        else:
            # Deletion is separate from taking a backup; preview before enabling.
            run(["restic", "forget", "--dry-run", "--tag", f"enclave:{args.project}", "--keep-daily", "14", "--keep-weekly", "4"])
            result = {"status": "retention dry run passed; no snapshots removed"}
        print(json.dumps(result))
    except (BackupError, OSError, ValueError) as exc:
        print(f"Backup operation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
