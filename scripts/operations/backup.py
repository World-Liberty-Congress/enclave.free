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
import time
from datetime import datetime, timezone


REQUIRED_SERVICES = {"core-backend", "sage", "postgres", "qdrant", "frontend", "backend"}


class BackupError(RuntimeError):
    pass


def run(args: list[str], **kwargs) -> str:
    # Runtime env and command errors can contain credentials. Keep them private.
    # The coordinator owns cancellation; terminal Ctrl-C must not abandon an
    # accepted Docker stop or interrupt restic's repository-lock cleanup.
    result = subprocess.run(args, capture_output=True, text=True, start_new_session=True, **kwargs)
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


def wait_ready(ids: list[str], timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while True:
        containers = json.loads(run(["docker", "inspect", *ids]))
        if any(not c["State"]["Running"] for c in containers):
            raise BackupError("A restarted service exited; inspect the host before reporting recovery")
        if all(c["State"].get("Health", {}).get("Status", "healthy") == "healthy" for c in containers):
            return
        if time.monotonic() >= deadline:
            raise BackupError(f"Restarted services did not become healthy within {timeout:g} seconds")
        time.sleep(1)


def restart_original(containers: list[dict]) -> None:
    # Sage requires a ready Postgres server; nginx resolves upstreams at startup.
    # Docker start does not apply Compose dependency ordering or wait for health.
    groups: list[list[str]] = [[], [], []]
    for c in containers:
        if not c["State"]["Running"]:
            continue
        service = c["Config"]["Labels"].get("com.docker.compose.service")
        tier = 2 if service == "backend" else 1 if service in {"core-backend", "sage", "frontend"} else 0
        groups[tier].append(c["Id"])
    errors = []
    for ids in groups:
        if not ids:
            continue
        try:
            run(["docker", "start", *ids])
            wait_ready(ids)
        except BackupError as exc:
            # Still attempt the remaining originally running services on error.
            errors.append(str(exc))
    if errors:
        raise BackupError("Service recovery incomplete: " + "; ".join(errors))


def checkpoint_repository(state_dir: Path) -> list[str]:
    """Keep encryption/chunk boundaries compatible with the off-host repository."""
    repository = state_dir / "checkpoint"
    command = ["restic", "--repo", str(repository)]
    if not (repository / "config").exists():
        run([*command, "init", "--copy-chunker-params", "--from-repo", os.environ["RESTIC_REPOSITORY"],
             "--from-password-file", os.environ["RESTIC_PASSWORD_FILE"]])
    else:
        run([*command, "snapshots", "--json"])
    return command


def check_capture_space(paths: list[Path], state_dir: Path, image_bytes: int) -> None:
    # Conservative full-copy estimate, even when restic will deduplicate most data.
    size = 2 * image_bytes + 1024 ** 3  # image archive + encrypted copy + headroom
    for path in paths:
        if path.is_dir():
            for parent, _, names in os.walk(path):
                size += sum((Path(parent) / name).lstat().st_size for name in names)
        else:
            size += path.lstat().st_size
    if shutil.disk_usage(state_dir).free < size:
        raise BackupError("Insufficient local checkpoint space; services have not been stopped")


def backup(project: str, config_paths: list[Path], state_dir: Path, maintenance_file: Path | None = None) -> dict:
    containers = inspect_project(project)
    paths = source_paths(containers, config_paths)
    if not any(c["State"]["Running"] for c in containers):
        raise BackupError("No running services; inspect the deployment before backing it up")
    # Only the stateless frontend may remain online during the consistent capture.
    for c in containers:
        if c["Config"]["Labels"].get("com.docker.compose.service") == "frontend":
            if any(m.get("RW", True) and m["Type"] in {"bind", "volume"} for m in c["Mounts"]):
                raise BackupError("Frontend has writable persistent storage; cannot keep it online safely")
    paused = [c for c in containers if c["Config"]["Labels"].get("com.docker.compose.service") != "frontend"]
    original_running = [c["Id"] for c in paused if c["State"]["Running"]]
    if not original_running:
        raise BackupError("No running application/storage services; inspect the deployment before backing it up")
    # Custom images must survive account/host loss independently of local tags.
    images = {image: json.loads(run(["docker", "image", "inspect", image]))[0]
              for image in sorted({c["Image"] for c in containers})}
    custom_images = sorted({c["Image"] for c in containers if c["Config"]["Labels"].get(
        "com.docker.compose.service"
    ) in {"core-backend", "sage", "frontend"} or not images[c["Image"]].get("RepoDigests")})
    manifest = {
        "format_version": 1,
        "project": project,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "consistency": "all persistent writers stopped for local encrypted checkpoint; stateless frontend remains online",
        "sources": [str(p) for p in paths],
        "containers": containers,  # Contains secrets: encrypted repository only.
        "images": images,  # Registry digests permit exact infrastructure recovery.
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
        if maintenance_file and (not maintenance_file.is_absolute() or maintenance_file.exists() or maintenance_file.is_symlink()):
            raise BackupError("Maintenance path must be absolute and absent; inspect any existing maintenance marker")
        # Stable stage path allows image archives and manifest to deduplicate.
        stage = state_dir / "recovery"
        stage.mkdir(mode=0o700, exist_ok=False)
        interrupted = False

        def on_signal(signum, frame):
            nonlocal interrupted
            # Never kill a Docker client with an accepted stop still in flight,
            # or restic before it releases its repository locks. Finish the
            # command, then honor cancellation at the next safe boundary.
            interrupted = True

        def check_interrupted():
            if interrupted:
                raise BackupError("Backup interrupted; original service recovery completed")

        previous = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
        stopping_started = False
        maintenance_created = False
        try:
            checkpoint = checkpoint_repository(state_dir)
            check_interrupted()
            # Reclaim obsolete local data before checking the capture budget.
            run([*checkpoint, "forget", "--keep-last", "2", "--group-by", "", "--prune"])
            check_interrupted()
            check_capture_space(paths, state_dir, sum(images[i].get("Size", 0) for i in custom_images))
            check_interrupted()
            (stage / "runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
            if custom_images:
                run(["docker", "image", "save", "--output", str(stage / "custom-images.tar"), *custom_images])
            check_interrupted()
            candidate_tag = f"enclave:{project}:candidate"
            pause_started = time.monotonic()
            try:
                if maintenance_file:
                    with maintenance_file.open("x") as marker:
                        maintenance_created = True
                        marker.write("Scheduled backup in progress\n")
                    maintenance_file.chmod(0o644)  # Caddy can read the marker; it contains no private data.
                stopping_started = True
                # Drain application writers while their databases are still up.
                writers = [c["Id"] for c in paused if c["State"]["Running"] and
                           c["Config"]["Labels"].get("com.docker.compose.service") in {"core-backend", "sage", "backend"}]
                storage = [i for i in original_running if i not in writers]
                # Sage needs its full 90-second request drain plus shutdown overhead.
                for ids, stop_timeout in ((writers, "120"), (storage, "60")):
                    if ids:
                        run(["docker", "stop", "--time", stop_timeout, *ids])
                        check_interrupted()
                current = json.loads(run(["docker", "inspect", *original_running]))
                if any(c["State"]["Running"] for c in current):
                    raise BackupError("Could not quiesce all application/storage writers")
                output = run([*checkpoint, "backup", "--json", "--tag", candidate_tag, *map(str, paths), str(stage)])
                summaries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
                summary = next((s for s in summaries if s.get("message_type") == "summary" and s.get("snapshot_id")), None)
                if not summary:
                    raise BackupError("Restic did not report a complete local checkpoint")
            finally:
                # Cancellation cannot interrupt dependency-ordered recovery itself.
                if stopping_started:
                    restart_original(paused)
                if maintenance_created:
                    maintenance_file.unlink()
                    maintenance_created = False
            pause_seconds = round(time.monotonic() - pause_started, 3)
            check_interrupted()
            # The network is deliberately outside the pause/recovery section.
            local_id = summary["snapshot_id"]
            run(["restic", "copy", "--from-repo", str(state_dir / "checkpoint"),
                 "--from-password-file", os.environ["RESTIC_PASSWORD_FILE"], local_id])
            check_interrupted()
            copied = json.loads(run(["restic", "snapshots", "--json", "--tag", candidate_tag]))
            check_interrupted()
            matches = [s for s in copied if s.get("original") == local_id]
            if len(matches) != 1:
                raise BackupError("Could not identify the complete off-host copy")
            snapshot_id = matches[0]["id"]
            # Partial restic snapshots must never displace a complete daily copy.
            tagged = run(["restic", "tag", "--json", "--remove", candidate_tag, "--add", f"enclave:{project}", snapshot_id])
            check_interrupted()
            changes = [json.loads(line) for line in tagged.splitlines() if line.startswith("{")]
            changed = next((r for r in changes if r.get("old_snapshot_id") == snapshot_id and r.get("new_snapshot_id")), None)
            if not changed:
                raise BackupError("Complete snapshot tag change did not report a new snapshot ID")
            return {"project": project, "snapshot_id": changed["new_snapshot_id"], "source_count": len(paths),
                    "custom_image_count": len(custom_images), "pause_seconds": pause_seconds}
        finally:
            try:
                shutil.rmtree(stage)
            finally:
                for s, handler in previous.items():
                    signal.signal(s, handler)


def restore(snapshot: str, target: Path) -> dict:
    # Restore is deliberately isolated: never overwrite live database paths.
    if target.exists():
        raise BackupError("Restore target must be a new directory")
    target.mkdir(mode=0o700, parents=True)
    run(["restic", "restore", snapshot, "--target", str(target), "--verify"])
    return {"snapshot_id": snapshot, "isolated_restore": str(target), "status": "restored and verified; application startup drill still required"}


def retention(project: str, apply: bool = False) -> dict:
    if not project or any(not (c.isalnum() or c in "-_") for c in project):
        raise BackupError("Retention requires a simple Compose project name")
    args = ["restic", "forget", "--tag", f"enclave:{project}", "--group-by", "tags",
            "--keep-daily", "14", "--keep-weekly", "4"]
    # One policy across release paths/hosts, scoped to complete project backups.
    # Candidate snapshots and release archives have separate tags and survive.
    args += ["--prune"] if apply else ["--dry-run"]
    run(args)
    if apply:
        run(["restic", "check"])
    return {"status": "retention applied" if apply else "retention dry run passed; no snapshots removed",
            "project": project, "keep_daily": 14, "keep_weekly": 4}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-repository", action="store_true", help="Only for isolated tooling tests")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--project", required=True)
    b.add_argument("--config-path", action="append", type=Path, required=True, help="Private env/key/TLS config path; repeat as needed")
    b.add_argument("--state-dir", type=Path, default=Path("/var/lib/enclave-backup"))
    b.add_argument("--maintenance-file", type=Path, help="Caddy-readable marker; removed only after healthy recovery")
    r = sub.add_parser("restore")
    r.add_argument("--snapshot", required=True)
    r.add_argument("--target", type=Path, required=True)
    sub.add_parser("check")
    f = sub.add_parser("retention-preview")
    f.add_argument("--project", required=True)
    f = sub.add_parser("retention-apply", help="Apply the accepted 14-daily/4-weekly policy to complete project backups")
    f.add_argument("--project", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        check_repository(args.allow_local_repository)
        if args.command == "backup":
            result = backup(args.project, args.config_path, args.state_dir, args.maintenance_file)
        elif args.command == "restore":
            result = restore(args.snapshot, args.target)
        elif args.command == "check":
            run(["restic", "check", "--read-data"])
            result = {"status": "repository contents verified"}
        else:
            result = retention(args.project, apply=args.command == "retention-apply")
        print(json.dumps(result))
    except (BackupError, OSError, ValueError) as exc:
        print(f"Backup operation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
