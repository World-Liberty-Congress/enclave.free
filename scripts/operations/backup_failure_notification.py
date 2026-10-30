#!/usr/bin/env python3
"""Send a content-free backup failure alert using the private production SMTP settings."""
from __future__ import annotations

import argparse
from email.message import EmailMessage
from email.utils import make_msgid
import json
import os
from pathlib import Path
import shlex
import smtplib
import ssl
import subprocess
from datetime import datetime, timezone

ALLOWED_UNITS = {"enclave-backup.service", "enclave-backup-notification-test.service", "enclave-monitor.service"}


def read_settings(path: Path) -> dict[str, str]:
    settings = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        settings[key] = " ".join(shlex.split(value, comments=False))
    return settings


def failure_summary(unit: str) -> dict[str, str]:
    if unit not in ALLOWED_UNITS:
        raise ValueError("Unsupported backup notification unit")
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=Id,ActiveState,Result,ExecMainCode,ExecMainStatus"],
        capture_output=True, text=True, check=True,
    )
    summary = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if summary.get("ActiveState") != "failed" or summary.get("Result") in {None, "success"}:
        raise ValueError("Unit is not in a failed state")
    return summary


def send_notification(unit: str, smtp: dict[str, str], recipient: str) -> dict:
    summary = failure_summary(unit)
    if not recipient or any(char in recipient for char in "\r\n"):
        raise ValueError("A valid backup alert recipient is required")
    test = unit == "enclave-backup-notification-test.service"
    monitor = unit == "enclave-monitor.service"
    message = EmailMessage()
    message["From"] = smtp["SMTP_FROM"]
    message["To"] = recipient
    message["Message-ID"] = make_msgid(domain="mg.liberator.global")
    message["Subject"] = (
        "[Liberator TEST] Backup failure notification verified"
        if test else "[Liberator] Operational checks failed — operator action required"
        if monitor else "[Liberator] Backup failed — operator action required"
    )
    message["X-Mailgun-Track"] = "no"
    message["X-Mailgun-Track-Clicks"] = "no"
    message["X-Mailgun-Track-Opens"] = "no"
    message.set_content(
        ("This is an intentional notification test. This test did not run the production backup or stop application services.\n\n"
         if test else "Backup freshness, API, search, or maintenance checks need attention.\n\n"
         if monitor else "The scheduled encrypted backup or its recovery/retention step failed.\n\n")
        + "Check the production server before treating this backup as successful.\n"
        + "\n".join(f"{key}: {value}" for key, value in sorted(summary.items()))
        + "\n\nOn the server, inspect:\n"
        + f"systemctl status {unit}\n"
        + f"journalctl -u {unit}\n"
        + "docker ps\n\n"
        + "Confirm the application has recovered and verify the latest successful encrypted snapshot.\n"
        + "This message contains no logs, credentials, prompts, or document content.\n"
    )
    with smtplib.SMTP(smtp["SMTP_HOST"], int(smtp["SMTP_PORT"]), timeout=30) as client:
        client.ehlo()
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
        client.login(smtp["SMTP_USER"], smtp["SMTP_PASS"])
        rejected = client.send_message(message)
        if rejected:
            raise RuntimeError("Backup alert recipient was rejected")
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "unit": unit, "test": test, "failure_summary": summary,
        "message_id": message["Message-ID"],
        "smtp_accepted": True, "certificate_verified_tls": True,
        "tracking_disabled": True, "sensitive_content_included": False,
    }


def recent_monitor_notification(path: Path, now: datetime) -> bool:
    try:
        receipt = json.loads(path.read_text())
        sent = datetime.fromisoformat(receipt["checked_at"])
        return receipt.get("smtp_accepted") is True and 0 <= (now - sent).total_seconds() < 6 * 3600
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("unit", choices=sorted(ALLOWED_UNITS))
    parser.add_argument("--production-env", type=Path, default=Path("/etc/enclave/production.env"))
    parser.add_argument("--receipt-dir", type=Path, default=Path("/var/lib/enclave-backup/notifications"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.unit == "enclave-monitor.service" and recent_monitor_notification(
            args.receipt_dir / (args.unit + ".json"), datetime.now(timezone.utc),
        ):
            failure_summary(args.unit)
            print(json.dumps({"unit": args.unit, "status": "already notified within six hours"}))
            return
        receipt = send_notification(args.unit, read_settings(args.production_env), os.environ.get("BACKUP_ALERT_RECIPIENT", ""))
    except Exception:
        # SMTP failures and source settings can include secrets; keep exception details out of the journal.
        raise SystemExit("Backup failure email could not be sent; inspect notifier configuration and SMTP access") from None
    args.receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (args.receipt_dir / (args.unit + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
