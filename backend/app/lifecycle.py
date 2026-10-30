from __future__ import annotations

"""
Operator-Controlled Privacy lifecycle status.

This module is the product-facing registry for current Data Retention,
Data Deletion, and Audit Log coverage. It intentionally describes current
coverage, including gaps, instead of implying complete guarantees.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import secrets
import stat

from fastapi import APIRouter, Depends, Query, Response
from fastapi import Header, HTTPException
import httpx
from pydantic import BaseModel, Field

import auth
import content_artifacts
import data_classification
import database
from data_deletion import (
    deletion_target_failed,
    deletion_target_skipped,
    deletion_target_succeeded,
    summarize_deletion_results,
)
import ingest
import ingest_db
import store


router = APIRouter(prefix="/admin/lifecycle", tags=["lifecycle"])

_CONFIDENTIALITY_MIGRATION_STATUS_CACHE: dict | None = None
logger = logging.getLogger("enclave.lifecycle")
_warned_missing_internal_agent_token = False
_sage_client: httpx.AsyncClient | None = None
_sage_client_timeout = httpx.Timeout(10.0, connect=5.0, read=10.0, write=10.0, pool=5.0)
RETENTION_AUTOMATION_ACTOR = "machine:scheduled-retention"


def _get_sage_client() -> httpx.AsyncClient:
    global _sage_client
    if _sage_client is None or _sage_client.is_closed:
        _sage_client = httpx.AsyncClient(timeout=_sage_client_timeout)
    return _sage_client


def _require_retention_automation_actor(
    x_retention_automation_token: str | None = Header(default=None),
) -> dict:
    configured_token = os.getenv("RETENTION_AUTOMATION_TOKEN", "").strip()
    if not configured_token:
        raise HTTPException(status_code=503, detail="Retention Automation Token is not configured")
    if not x_retention_automation_token or not secrets.compare_digest(
        x_retention_automation_token,
        configured_token,
    ):
        raise HTTPException(status_code=401, detail="Invalid Retention Automation Token")
    return {
        "type": "machine",
        "pubkey": RETENTION_AUTOMATION_ACTOR,
        "actor": RETENTION_AUTOMATION_ACTOR,
    }


async def close_sage_client() -> None:
    global _sage_client
    if _sage_client is not None:
        await _sage_client.aclose()
        _sage_client = None


DATA_CLASSES = [
    {
        "key": "user_profiles",
        "label": "User Profiles",
        "owner": "Enclave Control Plane",
        "storage_targets": ["SQLite"],
        "deletion": {
            "status": "complete",
            "summary": "User deletion removes the active User Profile, approval/access state, User Memory, and active Conversation state covered by this prototype.",
        },
        "retention": {
            "status": "partial",
            "summary": "Operators can invoke retention execution for stale active Conversation state.",
        },
        "audit": {
            "status": "partial",
            "summary": "User approval changes, auto-approval setting changes, and User Type administration are audited; deletion audit coverage remains separate.",
        },
        "notes": [
            "User Profile data is owned by the Enclave Control Plane.",
            "User Profile must remain distinct from Sage-owned User Memory.",
        ],
    },
    {
        "key": "user_memory",
        "label": "User Memory",
        "owner": "Sage",
        "storage_targets": ["SQLite"],
        "deletion": {
            "status": "complete",
            "summary": "Deleting a User purges Sage-owned User Memory for that subject User before the profile leaves active product surfaces.",
        },
        "retention": {
            "status": "partial",
            "summary": "Retention removes stale expirable or superseded User Memory without exposing raw memory content in lifecycle evidence.",
        },
        "audit": {
            "status": "partial",
            "summary": "Admin-confirmed User Memory creates, supersedes, deletes, and User deletion outcomes are audited; ambient Sage memory capture remains outside operator audit posture.",
        },
        "notes": [
            "User Memory is Sage-owned context and remains distinct from User Profile fields.",
        ],
    },
    {
        "key": "document_library",
        "label": "Document Library",
        "owner": "Enclave Control Plane",
        "storage_targets": ["SQLite", "uploads"],
        "deletion": {
            "status": "complete",
            "summary": "Active Document deletion removes library metadata, access defaults, uploaded artifact, retrieval entries, and runtime state.",
        },
        "retention": {
            "status": "partial",
            "summary": "Retention cleanup removes failed, superseded, abandoned, and orphaned Document artifacts without deleting current successful Documents.",
        },
        "audit": {
            "status": "partial",
            "summary": "Document upload, replacement, deletion, cleanup, and access/default changes are audited; some lower-level retrieval mutations are not direct operator events.",
        },
        "notes": [
            "Document Replacement keeps the current Document active until replacement succeeds.",
        ],
    },
    {
        "key": "retrieval_index",
        "label": "Retrieval Index",
        "owner": "Enclave Control Plane",
        "storage_targets": ["Qdrant"],
        "deletion": {
            "status": "complete",
            "summary": "Active Document deletion removes its derived Retrieval entries and reports the per-target result.",
        },
        "retention": {
            "status": "partial",
            "summary": "Retention execution removes retrieval points when failed or superseded Document artifacts are cleaned up.",
        },
        "audit": {
            "status": "partial",
            "summary": "Retrieval index deletion outcomes are represented in Data Deletion audit events, but individual retrieval writes are not directly audited.",
        },
        "notes": [
            "Retrieval selects knowledge from the Document Library for Sage Conversations.",
            "New Retrieval Index points store vectors and minimal metadata; encrypted chunk text lives in SQLite.",
        ],
    },
    {
        "key": "uploaded_document_artifacts",
        "label": "Uploaded Document Artifacts",
        "owner": "Enclave Control Plane",
        "storage_targets": ["uploads"],
        "deletion": {
            "status": "complete",
            "summary": "Active Document deletion and lifecycle cleanup remove uploaded artifacts for current, failed, and superseded Document ingestion jobs.",
        },
        "retention": {
            "status": "partial",
            "summary": "Operators can run retention cleanup for failed and superseded artifacts; scheduled retention execution is not implemented.",
        },
        "audit": {
            "status": "partial",
            "summary": "Lifecycle cleanup and Document deletion audit uploaded artifact outcomes; raw upload artifact writes are represented by Document upload events.",
        },
        "notes": [
            "New uploaded artifacts are encrypted by default when a Content Encryption Key is configured.",
            "Retrieval chunk text is stored separately from uploaded artifacts.",
        ],
    },
    {
        "key": "sage_session_memory",
        "label": "Sage Session Memory",
        "owner": "Sage",
        "storage_targets": ["Postgres"],
        "deletion": {
            "status": "complete",
            "summary": "Conversation deletion removes the public session record and associated Sage Session Memory.",
        },
        "retention": {
            "status": "partial",
            "summary": "Operators can invoke retention execution for stale active Conversation state; persistent Sage Session Memory retention is not implemented.",
        },
        "audit": {
            "status": "partial",
            "summary": "Retention, User deletion, public Conversation deletion, and tombstone retry record privacy-preserving Session Memory lifecycle evidence.",
        },
        "notes": [
            "Session Memory belongs to the Agent Runtime.",
            "Session Memory Deletion is distinct from deleting a public session record.",
        ],
        "retention_semantics": {
            "lifecycle_unit": "conversation",
            "policy_scope": "instance",
            "activity_basis": "human_or_sage_turn",
            "view_refreshes_activity": False,
            "ordinary_history_after_retention": "removed",
            "lifecycle_evidence_visibility": "admin_metadata_only",
            "summary": (
                "Conversation retention uses Instance-level policy and last human or Sage assistant "
                "turn activity; opening or viewing a Conversation does not refresh eligibility."
            ),
        },
    },
    {
        "key": "inference_verification_records",
        "label": "Inference Verification Records",
        "owner": "Enclave Control Plane",
        "storage_targets": ["SQLite"],
        "deletion": {
            "status": "not_started",
            "summary": "Deletion controls for Inference Verification Records are not implemented yet.",
        },
        "retention": {
            "status": "indefinite",
            "summary": "Inference Verification Records are retained indefinitely by default until a separate evidence-retention policy implements deletion or compaction.",
        },
        "audit": {
            "status": "partial",
            "summary": "Manual verification and blocked protected inference create concise Audit Log events without storing full attestation material in the Audit Log.",
        },
        "notes": [
            "Full provider attestation material remains in the Inference Verification Record, not in the Audit Log.",
            "These records support operator repair and historical evidence for protected Model Provider calls.",
        ],
        "evidence_retention": {
            "ordinary_conversation_policy_applies": False,
            "summary": "Inference Verification Records are governance evidence and do not share ordinary Conversation retention policy.",
        },
    },
    {
        "key": "retention_run_records",
        "label": "Retention Run Records",
        "owner": "Enclave Control Plane",
        "storage_targets": ["SQLite"],
        "deletion": {
            "status": "not_started",
            "summary": "Deletion controls for Retention Run Records are not implemented in v1.",
        },
        "retention": {
            "status": "indefinite",
            "summary": "Retention Run Records are retained indefinitely as metadata-only lifecycle evidence in v1 until a separate evidence-retention policy implements deletion or compaction.",
        },
        "audit": {
            "status": "partial",
            "summary": "Retention Execution creates run records and Audit Log evidence for reviewable lifecycle history.",
        },
        "notes": [
            "Retention Run Records store metadata-only policy snapshots and outcomes, not Conversation Content or User Memory text.",
            "These records are evidence for lifecycle execution and do not share ordinary Conversation retention policy.",
        ],
        "evidence_retention": {
            "ordinary_conversation_policy_applies": False,
            "summary": "Retention Run Records remain metadata-only lifecycle evidence until a separate evidence-retention policy exists.",
        },
    },
    {
        "key": "audit_log",
        "label": "Audit Log",
        "owner": "Enclave Control Plane",
        "storage_targets": ["SQLite"],
        "deletion": {
            "status": "not_started",
            "summary": "Audit Log deletion is not implemented as a product workflow.",
        },
        "retention": {
            "status": "partial",
            "summary": "Audit Log retention can compact sensitive detail while preserving lifecycle evidence; ordinary full Audit Log deletion is not exposed.",
        },
        "audit": {
            "status": "partial",
            "summary": "The Audit Log is tamper-evident for covered actions, including approval, User Type governance, Document actions, and Data Deletion workflows, but coverage is partial.",
        },
        "notes": [
            "The Audit Log is operator-visible governance data, not a debug/server log.",
        ],
    },
]


UNSUPPORTED_DEPLOYMENT_SURFACES = [
    {
        "key": "docker_logs",
        "label": "Docker Logs",
        "category": "runtime_logs",
        "summary": "Container stdout/stderr logs are managed by the deployment runtime, not product lifecycle controls.",
    },
    {
        "key": "gateway_logs",
        "label": "Gateway Logs",
        "category": "runtime_logs",
        "summary": "Gateway and reverse-proxy logs are operational records outside application retention/deletion workflows.",
    },
    {
        "key": "sqlite_wal",
        "label": "SQLite WAL",
        "category": "database_internals",
        "summary": "SQLite write-ahead-log files are database runtime artifacts, not product lifecycle records.",
    },
    {
        "key": "postgres_wal",
        "label": "Postgres WAL",
        "category": "database_internals",
        "summary": "Postgres write-ahead-log files are database runtime artifacts managed by the database operator.",
    },
    {
        "key": "host_backups",
        "label": "Host Backups",
        "category": "backups_snapshots",
        "summary": "Host-level backups must be governed by operator backup policy outside the product.",
    },
    {
        "key": "host_snapshots",
        "label": "Host Snapshots",
        "category": "backups_snapshots",
        "summary": "Filesystem, VM, and volume snapshots are outside active-storage lifecycle control.",
    },
    {
        "key": "browser_storage",
        "label": "Browser Storage and Cache",
        "category": "browser_held_copies",
        "summary": "Browser local storage, session storage, downloads, and cache are client-side Deployment Surfaces outside product lifecycle controls.",
    },
    {
        "key": "copied_exports",
        "label": "Copied Exports",
        "category": "copied_exports",
        "summary": "Downloaded or copied exports become operator-held artifacts outside Active Storage Lifecycle after creation.",
    },
    {
        "key": "provider_traces",
        "label": "Provider-Side Traces",
        "category": "provider_traces",
        "summary": "LLM, email, search, or infrastructure provider traces are governed by those providers and deployment contracts.",
    },
]


UNSUPPORTED_DEPLOYMENT_SURFACE_CATEGORIES = {
    "runtime_logs": {
        "label": "Runtime Logs",
        "guidance": "Configure deployment log retention, redaction, and access controls outside the product.",
        "operator_retention_policy": "Set runtime log retention, redaction, access controls, and disposal in the container host, platform, or log sink.",
    },
    "database_internals": {
        "label": "Database Internals",
        "guidance": "Manage WAL, replication, and database maintenance artifacts through database operator policy.",
        "operator_retention_policy": "Manage SQLite/Postgres WAL, replication, vacuum/checkpoint behavior, and database maintenance artifacts through database operations policy.",
    },
    "backups_snapshots": {
        "label": "Backups and Snapshots",
        "guidance": "Apply backup expiry, encryption, and restore-test policy at the host or platform layer.",
        "operator_retention_policy": "Define backup and snapshot encryption, expiry, restore testing, and disposal outside the product lifecycle controls.",
    },
    "browser_held_copies": {
        "label": "Browser-Held Copies",
        "guidance": "Clear browser storage and cache through browser or device management; product lifecycle controls cannot recall client-side copies.",
        "operator_retention_policy": "Use browser/device management for cache, downloads, local storage, and profile data created on operator or user devices.",
    },
    "copied_exports": {
        "label": "Copied Exports",
        "guidance": "Treat downloaded exports as operator-controlled records with separate storage, sharing, and disposal policy.",
        "operator_retention_policy": "Classify downloaded exports as operator-held records with separate storage, sharing, retention, and disposal rules.",
    },
    "provider_traces": {
        "label": "Provider Traces",
        "guidance": "Review provider retention contracts and disable provider-side logging where the deployment requires it.",
        "operator_retention_policy": "Review LLM, email, search, hosting, and infrastructure provider contracts; disable provider-side logging where required.",
    },
}


ENFORCED_RETENTION_DATA_CLASS_KEYS = {
    "sage_session_memory",
    "uploaded_document_artifacts",
    "user_memory",
    "audit_log",
}


DEFAULT_RETENTION_POLICY_OVERRIDES = {
    "sage_session_memory": {
        "enabled": True,
        "retention_window_days": 90,
        "scheduled_enforcement_enabled": True,
    },
    "uploaded_document_artifacts": {
        "enabled": True,
        "retention_window_days": 30,
        "scheduled_enforcement_enabled": True,
    },
    "user_memory": {
        "enabled": True,
        "retention_window_days": 180,
        "scheduled_enforcement_enabled": True,
    },
    "audit_log": {
        "enabled": True,
        "retention_window_days": 180,
        "scheduled_enforcement_enabled": True,
    },
}


SECURE_ERASE_SCOPE = {
    "status": "unsupported",
    "summary": (
        "Secure Erase is out of scope for v1; lifecycle controls apply to stated "
        "active-storage targets and exclude unsupported Deployment Surfaces such as logs, "
        "WAL, backups, snapshots, and provider traces."
    ),
}


HISTORICAL_SESSION_LOG_RETENTION = {
    "status": "operator_responsibility",
    "summary": (
        "Historical log/session retention is separate from active Session Memory deletion. "
        "Product lifecycle controls can delete supported active Session Memory, while runtime logs, "
        "provider traces, backups, snapshots, and historical platform records remain Deployment Surface responsibilities."
    ),
    "secure_erase_claimed": False,
}


LIFECYCLE_READINESS_CONVERSATION_BLOCKING_POLICY = {
    "user_conversations": "not_blocked",
    "admin_conversations": "available_for_repair",
    "protected_inference_gate": "independent",
    "summary": (
        "Lifecycle Readiness warnings are Admin review signals in v1; they do not block ordinary "
        "User Conversations or Admin Conversations used for lifecycle repair. Verifiable Inference "
        "gates remain independent."
    ),
}


class RetentionRunRequest(BaseModel):
    stale_conversation_days: int = Field(default=30, ge=0)
    document_artifact_days: int = Field(default=0, ge=0)
    preview_token: str | None = None
    confirm_current_counts: bool = False


class ScheduledRetentionRunRequest(BaseModel):
    retry_limit: int = Field(default=3, ge=0)


class RetentionPolicyUpdateRequest(BaseModel):
    enabled: bool
    retention_window_days: int = Field(ge=1)
    scheduled_enforcement_enabled: bool


class UnsupportedSurfaceAcknowledgementRequest(BaseModel):
    acknowledged: bool = True


class ArtifactEncryptionPostureUpdateRequest(BaseModel):
    posture: str


LIFECYCLE_SCOPE = {
    "key": "active_storage_lifecycle",
    "label": "Active Storage Lifecycle",
    "summary": (
        "Lifecycle controls apply to supported Lifecycle Data Classes in active product storage. "
        "Unsupported Deployment Surfaces remain disclosed separately."
    ),
    "excludes": "Deployment Surfaces such as runtime logs, WAL, backups, snapshots, and provider traces.",
}


RETENTION_SCHEDULER_SCOPE = {
    "status": "external_or_manual",
    "summary": (
        "Scheduled Retention Policy can mark Lifecycle Data Classes for scheduled Retention Execution; "
        "the product does not yet include its own Retention Scheduler, so execution is manual or externally invoked."
    ),
}


def _data_class_keys() -> set[str]:
    return {
        data_class["key"]
        for data_class in DATA_CLASSES
        if data_class["key"] in ENFORCED_RETENTION_DATA_CLASS_KEYS
    }


def _default_retention_policy(data_class_key: str) -> dict:
    policy = {
        "lifecycle_data_class": data_class_key,
        "enabled": False,
        "retention_window_days": 30,
        "scheduled_enforcement_enabled": False,
    }
    policy.update(DEFAULT_RETENTION_POLICY_OVERRIDES.get(data_class_key, {}))
    return policy


def _stored_retention_policies() -> dict[str, dict]:
    policies = {
        key: _default_retention_policy(key)
        for key in _data_class_keys()
    }
    raw_value = database.get_setting("lifecycle_retention_policies")
    if not raw_value:
        return policies
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return policies
    if not isinstance(parsed, dict):
        return policies
    valid_keys = _data_class_keys()
    for key, value in parsed.items():
        if key not in valid_keys or not isinstance(value, dict):
            continue
        policy = _default_retention_policy(key)
        policy["enabled"] = bool(value.get("enabled", policy["enabled"]))
        try:
            retention_window_days = int(value.get("retention_window_days", policy["retention_window_days"]))
        except (TypeError, ValueError):
            retention_window_days = policy["retention_window_days"]
        policy["retention_window_days"] = max(1, retention_window_days)
        policy["scheduled_enforcement_enabled"] = bool(
            value.get("scheduled_enforcement_enabled", policy["scheduled_enforcement_enabled"])
        )
        policies[key] = policy
    return policies


def _retention_policy_for(data_class_key: str) -> dict:
    return {
        **_default_retention_policy(data_class_key),
        **_stored_retention_policies().get(data_class_key, {}),
    }


def _scheduled_retention_enabled_classes(policies: dict[str, dict] | None = None) -> list[str]:
    source = policies if policies is not None else _stored_retention_policies()
    return sorted([
        key
        for key, policy in source.items()
        if policy.get("enabled") and policy.get("scheduled_enforcement_enabled")
    ])


def _retention_scheduler_observation(enabled_classes: list[str]) -> dict:
    if not enabled_classes:
        return {
            "status": "disabled",
            "enabled_classes": [],
            "last_run": None,
            "summary": "No Lifecycle Data Classes have scheduled Retention Execution enabled.",
        }
    machine_runs = database.list_retention_run_records(limit=50, trigger="machine")
    if not machine_runs:
        return {
            "status": "never_observed",
            "enabled_classes": enabled_classes,
            "last_run": None,
            "summary": "Scheduled Retention Policy is enabled, but no Retention Scheduler run has been observed.",
        }
    last_run = machine_runs[0]
    finished_at = _parse_timestamp(last_run.get("finished_at"))
    if last_run.get("status") in {"failed", "partial_failure", "incomplete"}:
        status = "failing"
        summary = "The most recent Retention Scheduler run did not fully succeed."
    elif finished_at and finished_at < datetime.utcnow() - timedelta(hours=48):
        status = "stale"
        summary = "The most recent Retention Scheduler run is older than the expected observation window."
    else:
        status = "healthy"
        summary = "A recent Retention Scheduler run created lifecycle evidence."
    return {
        "status": status,
        "enabled_classes": enabled_classes,
        "last_run": {
            "id": last_run.get("id"),
            "status": last_run.get("status"),
            "trigger": last_run.get("trigger"),
            "actor": last_run.get("actor"),
            "finished_at": last_run.get("finished_at"),
        },
        "summary": summary,
    }


def _data_classes_with_retention_policies() -> list[dict]:
    stored_policies = _stored_retention_policies()
    classes = []
    for data_class in DATA_CLASSES:
        class_copy = deepcopy(data_class)
        class_copy["confidentiality"] = _confidentiality_posture_for_data_class(data_class["key"])
        if data_class["key"] in _data_class_keys():
            class_copy["retention_policy"] = {
                **_default_retention_policy(data_class["key"]),
                **stored_policies.get(data_class["key"], {}),
            }
        classes.append(class_copy)
    return classes


def _confidentiality_posture_for_data_class(data_class_key: str) -> dict:
    artifact_status = _artifact_encryption_status()
    if data_class_key == "uploaded_document_artifacts":
        return {
            "status": artifact_status["status"],
            "summary": artifact_status["summary"],
        }
    if data_class_key == "document_library":
        return {
            "status": "partial" if artifact_status["status"] != "encrypted" else "encrypted",
            "summary": "Document metadata is stored separately from uploaded artifact content; artifact content posture is reported under Uploaded Document Artifacts.",
        }
    if data_class_key == "retrieval_index":
        retrieval_status = _retrieval_index_confidentiality_status()
        if retrieval_status["status"] != "partial":
            return retrieval_status
        return {
            "status": "partial",
            "summary": (
                "New Qdrant Retrieval Index entries store vector data and minimal metadata; "
                "legacy plaintext payloads may remain until the Confidentiality Migration "
                "preview/execute workflow inspects and repairs eligible records."
            ),
        }
    if data_class_key == "user_profiles":
        return {
            "status": "encrypted",
            "summary": "User PII fields are encrypted at rest in SQLite.",
        }
    if data_class_key == "user_memory":
        return {
            "status": "partial",
            "summary": "Initial User Memory is low-sensitivity Sage context and may be stored without content encryption.",
        }
    if data_class_key == "sage_session_memory":
        return {
            "status": "partial",
            "summary": "Sage Session Memory uses active-storage lifecycle controls; Secure Erase and full historical/log retention remain unsupported.",
        }
    if data_class_key == "inference_verification_records":
        return {
            "status": "partial",
            "summary": "Attestation material is encrypted at rest when the deployment secret is configured; Audit Log references intentionally omit full attestation material.",
        }
    if data_class_key == "audit_log":
        return {
            "status": "partial",
            "summary": "Audit Log detail can be compacted by retention while lifecycle evidence is preserved.",
        }
    return {
        "status": "unsupported",
        "summary": "No confidentiality posture is defined for this Lifecycle Data Class.",
    }


def _artifact_encryption_status() -> dict:
    status = content_artifacts.artifact_encryption_status()
    if status["status"] != "encrypted":
        return status

    inventory = _active_artifact_confidentiality_inventory()
    if inventory["plaintext_artifacts"]:
        return {
            "posture": "required",
            "status": "mixed",
            "summary": (
                f"Artifact encryption is required, but {len(inventory['plaintext_artifacts'])} active uploaded "
                "artifact(s) still appear to be legacy plaintext."
            ),
            "legacy_plaintext_artifacts": inventory["plaintext_artifacts"],
        }
    if inventory["missing_artifacts"]:
        return {
            "posture": "required",
            "status": "mixed",
            "summary": (
                f"Artifact encryption is required, but {len(inventory['missing_artifacts'])} active uploaded "
                "artifact path(s) could not be verified."
            ),
            "missing_artifacts": inventory["missing_artifacts"],
        }
    return {
        **status,
        "summary": "Uploaded Document artifacts are encrypted in active storage for new writes and verified active artifacts.",
    }


def _active_artifact_confidentiality_inventory() -> dict:
    plaintext_artifacts = []
    encrypted_artifacts = []
    missing_artifacts = []
    offset = 0
    limit = 1000
    active_statuses = {"completed", "completed_with_errors", "processing", "pending"}
    while True:
        jobs = ingest_db.list_jobs(limit=limit, offset=offset)
        if not jobs:
            break
        for job in jobs:
            if job.get("status") not in active_statuses:
                continue
            if job.get("is_current") in (0, False):
                continue
            if job.get("replaced_by_job_id"):
                continue
            file_path = job.get("file_path")
            if not file_path:
                continue
            try:
                with open(file_path, "rb") as artifact:
                    content = artifact.read(64)
            except FileNotFoundError:
                missing_artifacts.append(_document_artifact_result(job, "missing"))
                continue
            except OSError as exc:
                missing_artifacts.append({**_document_artifact_result(job, "unreadable"), "error": str(exc)})
                continue
            if content_artifacts.is_encrypted_artifact(content):
                encrypted_artifacts.append(_document_artifact_result(job, "encrypted"))
            else:
                plaintext_artifacts.append(_document_artifact_result(job, "legacy_plaintext"))
        if len(jobs) < limit:
            break
        offset += limit
    return {
        "plaintext_artifacts": plaintext_artifacts,
        "encrypted_artifacts": encrypted_artifacts,
        "missing_artifacts": missing_artifacts,
    }


def _document_artifact_result(job: dict, status: str) -> dict:
    return {
        "job_id": job.get("job_id"),
        "filename": job.get("filename"),
        "file_path": job.get("file_path"),
        "status": status,
    }


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    temp_path: Path | None = None
    with tempfile.NamedTemporaryFile(delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as temp:
        temp.write(content)
        temp.flush()
        os.fsync(temp.fileno())
        temp_path = Path(temp.name)
    try:
        temp_path.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def _retrieval_index_confidentiality_status() -> dict:
    return {
        "status": "encrypted",
        "summary": "Qdrant Retrieval Index payloads are minimized; encrypted chunk text is hydrated from product-owned storage.",
    }


def preview_confidentiality_migration_status() -> dict:
    """Return the last computed migration status without running inventory work."""
    if _CONFIDENTIALITY_MIGRATION_STATUS_CACHE is not None:
        return deepcopy(_CONFIDENTIALITY_MIGRATION_STATUS_CACHE)
    return {
        "status": "unknown",
        "summary": "Confidentiality Migration status has not been computed yet.",
    }


def _remember_confidentiality_migration_status(preview: dict) -> None:
    global _CONFIDENTIALITY_MIGRATION_STATUS_CACHE
    _CONFIDENTIALITY_MIGRATION_STATUS_CACHE = {
        "status": preview.get("status", "unknown"),
        "summary": preview.get(
            "summary",
            "Confidentiality Migration rewrites or verifies eligible legacy plaintext active content.",
        ),
    }


def _active_content_encryption_evidence(artifact_status: dict) -> dict:
    retrieval_status = _retrieval_index_confidentiality_status()
    migration_status = preview_confidentiality_migration_status()
    return {
        "artifact_encryption_posture": {
            "status": artifact_status["status"],
            "summary": (
                "Artifact Encryption Posture: "
                f"{artifact_status['summary']} This covers uploaded Document artifacts in active storage, "
                "not backups, snapshots, logs, or Secure Erase."
            ),
        },
        "retrieval_content_posture": {
            "status": retrieval_status["status"],
            "summary": (
                "Retrieval Content Posture: "
                f"{retrieval_status['summary']} Qdrant should not contain plaintext chunk text for current writes."
            ),
        },
        "confidentiality_migration": {
            "status": migration_status["status"],
            "summary": (
                f"{migration_status['summary']} "
                "it does not claim Secure Erase across Deployment Surfaces."
            ),
        },
        "secure_erase": {
            "claimed": False,
            "summary": "No Secure Erase claim is made for active content encryption or migration evidence.",
        },
    }


def preview_confidentiality_migration() -> dict:
    artifact_status = _artifact_encryption_status()
    artifact_inventory = _active_artifact_confidentiality_inventory()

    artifact_actions = []
    for artifact in artifact_inventory["plaintext_artifacts"]:
        artifact_actions.append({
            **artifact,
            "target": "uploaded_document_artifact",
            "action": "encrypt_artifact",
            "eligible": artifact_status["status"] != "not_configured",
            "skip_reason": None if artifact_status["status"] != "not_configured" else "content_encryption_key_not_configured",
        })

    retrieval_actions = []
    skipped = []

    documents: dict[str, dict] = {}
    for action in artifact_actions + retrieval_actions:
        job_id = action.get("job_id")
        if not job_id:
            continue
        documents.setdefault(job_id, {
            "job_id": job_id,
            "filename": action.get("filename") or action.get("source_file"),
            "actions": [],
        })["actions"].append(action)

    support_removal_ready = True

    preview = {
        "status": "ready" if artifact_actions or retrieval_actions else "nothing_to_migrate",
        "artifact_encryption": artifact_status,
        "affected_documents": list(documents.values()),
        "artifacts": artifact_actions,
        "retrieval_payloads": retrieval_actions,
        "skipped": skipped,
        "expected_actions": artifact_actions + retrieval_actions,
        "support_removal_ready": support_removal_ready,
        "secure_erase_claimed": False,
        "summary": (
            f"Preview found {len(artifact_actions)} plaintext artifact(s) and "
            f"{len(retrieval_actions)} legacy Retrieval payload(s). No Secure Erase claim is made."
        ),
    }
    _remember_confidentiality_migration_status(preview)
    return preview


def execute_confidentiality_migration(*, actor: str) -> dict:
    preview = preview_confidentiality_migration()
    results = []

    for artifact in preview["artifacts"]:
        if not artifact["eligible"]:
            results.append({**artifact, "status": "skipped", "reason": artifact["skip_reason"]})
            continue
        try:
            path = Path(artifact["file_path"])
            content = path.read_bytes()
            if content_artifacts.is_encrypted_artifact(content):
                status = "skipped"
                reason = "already_encrypted"
            else:
                encrypted_content = content_artifacts.encrypt_bytes(content)
                _atomic_replace_bytes(path, encrypted_content)
                status = "succeeded"
                reason = None
            results.append({**artifact, "status": status, "reason": reason})
        except Exception as exc:
            results.append({**artifact, "status": "failed", "reason": str(exc)})

    database.update_setting_with_audit(
        "confidentiality_migration_last_run",
        json.dumps({"actor": actor, "result_count": len(results), "secure_erase_claimed": False}),
        changed_by=actor,
    )

    return {
        "status": "completed_with_errors" if any(result["status"] == "failed" for result in results) else "completed",
        "results": results,
        "summary": "Confidentiality migration completed without making any Secure Erase claim.",
        "secure_erase_claimed": False,
        "post_migration_status": get_lifecycle_status(),
    }


def _update_retention_policy(data_class_key: str, request: RetentionPolicyUpdateRequest, *, changed_by: str) -> dict:
    if data_class_key not in _data_class_keys():
        raise HTTPException(status_code=404, detail="Lifecycle Data Class not found")
    policies = _stored_retention_policies()
    policies[data_class_key] = {
        "lifecycle_data_class": data_class_key,
        "enabled": request.enabled,
        "retention_window_days": request.retention_window_days,
        "scheduled_enforcement_enabled": request.scheduled_enforcement_enabled,
    }
    database.update_setting_with_audit(
        "lifecycle_retention_policies",
        json.dumps(policies, sort_keys=True),
        changed_by=changed_by,
    )
    _mark_lifecycle_readiness_stale("retention_policy_changed", changed_by=changed_by)
    return _retention_policy_for(data_class_key)


def _acknowledged_unsupported_surface_keys() -> set[str]:
    raw_value = database.get_setting("lifecycle_unsupported_surface_acknowledgements")
    if not raw_value:
        return set()
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return set()
    if not isinstance(parsed, list):
        return set()
    valid_keys = {surface["key"] for surface in UNSUPPORTED_DEPLOYMENT_SURFACES}
    return {
        key
        for key in parsed
        if isinstance(key, str) and key in valid_keys
    }


def _unsupported_surface_category_acknowledgements() -> dict[str, dict]:
    raw_value = database.get_setting("lifecycle_unsupported_surface_category_acknowledgements")
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    valid_categories = set(UNSUPPORTED_DEPLOYMENT_SURFACE_CATEGORIES)
    return {
        category: value
        for category, value in parsed.items()
        if category in valid_categories and isinstance(value, dict)
    }


def _unsupported_deployment_surfaces() -> list[dict]:
    acknowledged_keys = _acknowledged_unsupported_surface_keys()
    surfaces = []
    for surface in UNSUPPORTED_DEPLOYMENT_SURFACES:
        surfaces.append({
            **deepcopy(surface),
            "status": "unsupported",
            "acknowledged": surface["key"] in acknowledged_keys,
        })
    return surfaces


def _unsupported_deployment_surface_categories() -> list[dict]:
    category_acknowledgements = _unsupported_surface_category_acknowledgements()
    surfaces = _unsupported_deployment_surfaces()
    grouped = []
    for category, metadata in UNSUPPORTED_DEPLOYMENT_SURFACE_CATEGORIES.items():
        acknowledgement = category_acknowledgements.get(category, {})
        grouped.append({
            "category": category,
            "label": metadata["label"],
            "status": "unsupported",
            "guidance": metadata["guidance"],
            "operator_retention_policy": {
                "owner": "operator",
                "summary": metadata["operator_retention_policy"],
                "acknowledgement_effect": (
                    "records_operator_review_not_lifecycle_data_class; product Data Deletion and Retention controls "
                    "do not apply to this Deployment Surface category"
                ),
                "secure_erase_boundary": "Secure Erase is not claimed for unsupported Deployment Surfaces.",
            },
            "acknowledged": bool(acknowledgement.get("acknowledged")),
            "acknowledged_by": acknowledgement.get("acknowledged_by"),
            "acknowledged_at": acknowledgement.get("acknowledged_at"),
            "posture_version": acknowledgement.get("posture_version"),
            "surfaces": [
                surface
                for surface in surfaces
                if surface.get("category") == category
            ],
        })
    return grouped


def _set_unsupported_surface_acknowledgement(surface_key: str, acknowledged: bool, *, changed_by: str) -> list[dict]:
    valid_keys = {surface["key"] for surface in UNSUPPORTED_DEPLOYMENT_SURFACES}
    if surface_key not in valid_keys:
        raise HTTPException(status_code=404, detail="Unsupported deployment surface not found")
    acknowledged_keys = _acknowledged_unsupported_surface_keys()
    if acknowledged:
        acknowledged_keys.add(surface_key)
    else:
        acknowledged_keys.discard(surface_key)
    database.update_setting_with_audit(
        "lifecycle_unsupported_surface_acknowledgements",
        json.dumps(sorted(acknowledged_keys)),
        changed_by=changed_by,
    )
    _mark_lifecycle_readiness_stale("unsupported_surface_acknowledgement_changed", changed_by=changed_by)
    return _unsupported_deployment_surfaces()


def _set_unsupported_surface_category_acknowledgement(category: str, acknowledged: bool, *, changed_by: str) -> list[dict]:
    if category not in UNSUPPORTED_DEPLOYMENT_SURFACE_CATEGORIES:
        raise HTTPException(status_code=404, detail="Unsupported deployment surface category not found")
    acknowledgements = _unsupported_surface_category_acknowledgements()
    if acknowledged:
        acknowledgements[category] = {
            "acknowledged": True,
            "acknowledged_by": changed_by,
            "acknowledged_at": datetime.utcnow().isoformat(),
        }
        acknowledgements[category]["posture_version"] = _readiness_version(acknowledgements)
    else:
        acknowledgements.pop(category, None)
    database.update_setting_with_audit(
        "lifecycle_unsupported_surface_category_acknowledgements",
        json.dumps(acknowledgements, sort_keys=True),
        changed_by=changed_by,
    )
    _mark_lifecycle_readiness_stale("unsupported_surface_category_acknowledgement_changed", changed_by=changed_by)
    return _unsupported_deployment_surface_categories()


def _canonical_surface_category_acknowledgements(acknowledgements: dict | None = None) -> dict:
    raw_acknowledgements = acknowledgements if acknowledgements is not None else _unsupported_surface_category_acknowledgements()
    return {
        category: {"acknowledged": bool(value.get("acknowledged"))}
        for category, value in raw_acknowledgements.items()
        if category in UNSUPPORTED_DEPLOYMENT_SURFACE_CATEGORIES and isinstance(value, dict)
    }


def _readiness_version(acknowledgements: dict | None = None) -> str:
    posture = {
        "retention_policies": _stored_retention_policies(),
        "acknowledged_unsupported_surfaces": sorted(_acknowledged_unsupported_surface_keys()),
        "acknowledged_unsupported_surface_categories": _canonical_surface_category_acknowledgements(acknowledgements),
        "artifact_encryption": _artifact_encryption_status().get("status"),
    }
    return hashlib.sha256(json.dumps(posture, sort_keys=True).encode("utf-8")).hexdigest()


def _unsupported_surface_categories() -> list[str]:
    return sorted(UNSUPPORTED_DEPLOYMENT_SURFACE_CATEGORIES)


def _stored_lifecycle_readiness() -> dict | None:
    raw_value = database.get_setting("lifecycle_readiness")
    if not raw_value:
        return None
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _lifecycle_readiness() -> dict:
    stored = _stored_lifecycle_readiness()
    current_version = _readiness_version()
    if not stored:
        return {
            "status": "needs_review",
            "reviewed": False,
            "posture_version": current_version,
            "reviewed_at": None,
            "reviewed_by": None,
            "stale_reason": None,
            "acknowledged_unsupported_surface_categories": _unsupported_surface_categories(),
            "conversation_blocking_policy": deepcopy(LIFECYCLE_READINESS_CONVERSATION_BLOCKING_POLICY),
            "summary": "Lifecycle Readiness requires Admin review before the Instance is treated as reviewed.",
        }
    reviewed_version = stored.get("posture_version")
    stale_reason = stored.get("stale_reason")
    is_stale = bool(stale_reason) or reviewed_version != current_version
    return {
        "status": "stale" if is_stale else "reviewed",
        "reviewed": not is_stale,
        "posture_version": current_version,
        "reviewed_posture_version": reviewed_version,
        "reviewed_at": stored.get("reviewed_at"),
        "reviewed_by": stored.get("reviewed_by"),
        "stale_reason": stale_reason or ("posture_changed" if is_stale else None),
        "acknowledged_unsupported_surface_categories": stored.get(
            "acknowledged_unsupported_surface_categories",
            _unsupported_surface_categories(),
        ),
        "conversation_blocking_policy": deepcopy(LIFECYCLE_READINESS_CONVERSATION_BLOCKING_POLICY),
        "summary": (
            "Lifecycle Readiness is stale and needs Admin review."
            if is_stale
            else "Lifecycle Readiness has been reviewed for the current posture."
        ),
    }


def _review_lifecycle_readiness(*, changed_by: str) -> dict:
    reviewed = {
        "reviewed_at": datetime.utcnow().isoformat(),
        "reviewed_by": changed_by,
        "posture_version": _readiness_version(),
        "stale_reason": None,
        "acknowledged_unsupported_surface_categories": _unsupported_surface_categories(),
    }
    database.update_setting_with_audit(
        "lifecycle_readiness",
        json.dumps(reviewed, sort_keys=True),
        changed_by=changed_by,
    )
    return _lifecycle_readiness()


def _mark_lifecycle_readiness_stale(reason: str, *, changed_by: str) -> None:
    stored = _stored_lifecycle_readiness()
    if not stored:
        return
    stored["stale_reason"] = reason
    database.update_setting(
        "lifecycle_readiness",
        json.dumps(stored, sort_keys=True),
    )
    database.update_setting_with_audit(
        "lifecycle_readiness_staleness",
        json.dumps(stored, sort_keys=True),
        changed_by=changed_by,
    )


def get_lifecycle_status() -> dict:
    """Return the current Instance data lifecycle posture."""
    policies = _stored_retention_policies()
    enabled_classes = _scheduled_retention_enabled_classes(policies)
    artifact_encryption = _artifact_encryption_status()
    retention_scheduler = deepcopy(RETENTION_SCHEDULER_SCOPE)
    retention_scheduler["observation"] = _retention_scheduler_observation(enabled_classes)
    return {
        "lifecycle_scope": deepcopy(LIFECYCLE_SCOPE),
        "data_classes": _data_classes_with_retention_policies(),
        "content_encryption": content_artifacts.content_encryption_status(),
        "artifact_encryption": artifact_encryption,
        "active_content_encryption": _active_content_encryption_evidence(artifact_encryption),
        "secure_erase": deepcopy(SECURE_ERASE_SCOPE),
        "unsupported_deployment_surfaces": _unsupported_deployment_surfaces(),
        "unsupported_deployment_surface_categories": _unsupported_deployment_surface_categories(),
        "historical_session_log_retention": deepcopy(HISTORICAL_SESSION_LOG_RETENTION),
        "lifecycle_readiness": _lifecycle_readiness(),
        "scheduled_retention": {
            "enabled_classes": enabled_classes,
        },
        "retention_scheduler": retention_scheduler,
        "data_classification": data_classification.get_data_classification_inventory(),
        "audit_coverage": get_audit_coverage_inventory(),
        "deletion_tombstones": database.summarize_deletion_tombstones(),
    }


AUDIT_COVERAGE_INVENTORY = [
    {
        "key": "user_approval_changes",
        "label": "User approval changes",
        "status": "audited",
        "event_family": "user_approval",
        "surface": "ordinary_admin",
    },
    {
        "key": "instance_settings_changes",
        "label": "Instance settings changes",
        "status": "audited",
        "event_family": "instance_settings",
        "surface": "ordinary_admin",
    },
    {
        "key": "user_type_governance",
        "label": "User Type governance",
        "status": "audited",
        "event_family": "user_types",
        "surface": "ordinary_admin",
    },
    {
        "key": "document_actions",
        "label": "Document upload, replacement, access, cleanup, and deletion",
        "status": "audited",
        "event_family": "document_actions",
        "surface": "ordinary_admin",
    },
    {
        "key": "data_deletion_workflows",
        "label": "Lifecycle deletion, retention, and tombstone retry workflows",
        "status": "audited",
        "event_family": "data_deletion",
        "surface": "ordinary_admin",
    },
    {
        "key": "user_memory_admin_actions",
        "label": "Admin-confirmed User Memory create, supersede, and delete",
        "status": "audited",
        "event_family": "user_memories",
        "surface": "ordinary_admin",
    },
    {
        "key": "inference_verification_events",
        "label": "Inference Verification events",
        "status": "audited",
        "event_family": "inference_verification",
        "surface": "operator_security",
    },
    {
        "key": "retrieval_index_internal_writes",
        "label": "Low-level Retrieval Index writes",
        "status": "documented_exception",
        "event_family": None,
        "surface": "internal_reconciliation",
        "exception": "Represented by higher-level Document action and Data Deletion audit events.",
    },
    {
        "key": "read_only_inspection",
        "label": "Constrained read-only inspection paths",
        "status": "documented_exception",
        "event_family": None,
        "surface": "read_only",
        "exception": "Read-only inspection does not mutate Instance state.",
    },
]


def get_audit_coverage_inventory() -> dict:
    items = deepcopy(AUDIT_COVERAGE_INVENTORY)
    missing = [item for item in items if item.get("status") not in {"audited", "documented_exception"}]
    return {
        "items": items,
        "summary": {
            "total": len(items),
            "audited": len([item for item in items if item.get("status") == "audited"]),
            "documented_exceptions": len([
                item for item in items if item.get("status") == "documented_exception"
            ]),
            "missing": len(missing),
            "guardrail_passed": len(missing) == 0,
        },
    }


def _lifecycle_error_category(detail: object) -> str:
    if isinstance(detail, (httpx.TimeoutException, httpx.ConnectError)):
        return "target_unavailable"
    if isinstance(detail, httpx.HTTPStatusError):
        status_code = detail.response.status_code
        if status_code in (401, 403):
            return "unauthorized_internal_contract"
        if status_code == 404:
            return "not_found"
        if status_code in (409, 410):
            return "already_deleted"
        if 500 <= status_code:
            return "target_unavailable"
    text = str(detail or "").lower()
    if "unavailable" in text or "connection refused" in text or "timed out" in text:
        return "target_unavailable"
    if "not found" in text or "404" in text:
        return "not_found"
    if "already" in text and "deleted" in text:
        return "already_deleted"
    if "unauthorized" in text or "forbidden" in text or "401" in text or "403" in text:
        return "unauthorized_internal_contract"
    return "target_error"


def categorize_error(detail: object) -> str:
    return _lifecycle_error_category(detail)


def _sanitize_lifecycle_deletion(deletion: dict) -> dict:
    sanitized = deepcopy(deletion)
    sanitized["results"] = []
    for result in deletion.get("results", []):
        sanitized_result = dict(result)
        if sanitized_result.get("status") == "failed":
            sanitized_result["detail"] = _lifecycle_error_category(sanitized_result.get("detail"))
        sanitized["results"].append(sanitized_result)
    return sanitized


async def delete_sage_user_runtime(user_id: int) -> dict:
    """Validate the new private contract; never retain arbitrary remote metadata."""
    try:
        token = os.getenv("INTERNAL_AGENT_TOKEN", "")
        if not token:
            raise ValueError("Missing internal token")
        sage_url = os.getenv("SAGE_WEB_URL", "http://sage:3000").rstrip("/")
        response = await _get_sage_client().post(
            f"{sage_url}/internal/lifecycle/users/{user_id}/delete",
            headers={"X-Internal-Agent-Token": token},
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Invalid receipt")
        deletion = body.get("deletion")
        if (type(body.get("user_id")) is not int or body["user_id"] != user_id
                or not isinstance(deletion, dict)
                or deletion.get("status") != "succeeded"
                or deletion.get("retryable") is not False
                or any(type(body.get(key)) is not int or body[key] < 0
                       for key in ("conversations_deleted", "identities_deleted"))):
            raise ValueError("Invalid receipt")
        result = deletion_target_succeeded(
            target_kind="user_runtime", target_id=str(user_id),
            action="delete_user_runtime", detail="Deleted Sage Conversations, memory, and User identity.",
        )
        result["count"] = body["conversations_deleted"]
        return summarize_deletion_results([result])
    except Exception as exc:
        detail = "busy" if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409 else categorize_error(exc)
        return summarize_deletion_results([deletion_target_failed(
            target_kind="user_runtime", target_id=str(user_id),
            action="delete_user_runtime", detail=detail, retryable=True,
        )])


async def complete_user_runtime_cleanup(tombstone: dict) -> dict:
    target = tombstone["conversation_id"]
    if not target.startswith("user:") or not target[5:].isdigit():
        raise ValueError("Invalid User cleanup target")
    user_id = int(target[5:])
    deletion = await delete_sage_user_runtime(user_id)
    try:
        updated = database.update_deletion_tombstone_after_retry(
            tombstone["id"], status="completed" if deletion["status"] == "succeeded" else "incomplete",
            deletion=deletion,
        )
        if updated is None:
            raise RuntimeError("Missing cleanup intent")
        # A late failed retry must not undo another retry's committed success.
        return updated["deletion"]
    except Exception:
        return summarize_deletion_results([deletion_target_failed(
            target_kind="user_runtime", target_id=str(user_id), action="delete_user_runtime",
            detail="completion_persistence_failed", retryable=True,
        )])


async def post_sage_session_memory_delete(payload: dict) -> dict:
    global _warned_missing_internal_agent_token
    sage_url = os.getenv("SAGE_WEB_URL", "http://sage:3000").rstrip("/")
    token = os.getenv("INTERNAL_AGENT_TOKEN", "")
    if not token and not _warned_missing_internal_agent_token:
        logger.warning(
            "INTERNAL_AGENT_TOKEN is unset; Sage lifecycle requests will be sent without X-Internal-Agent-Token."
        )
        _warned_missing_internal_agent_token = True
    headers = {"X-Internal-Agent-Token": token} if token else {}
    response = await _get_sage_client().post(
        f"{sage_url}/internal/lifecycle/session-memory/delete",
        json=payload,
        headers=headers,
    )
    response.raise_for_status()
    body = response.json()
    if "deletion" not in body:
        raise ValueError(
            f"Sage session-memory delete response missing deletion key: "
            f"status={response.status_code} body={body}"
        )
    return body["deletion"]


async def delete_session_memory_for_conversation(session: dict) -> dict:
    """Delete Sage Session Memory for a Conversation.

    Sage-backed deletion is the only supported Session Memory lifecycle
    boundary for this prototype.
    """
    session_id = str(session.get("id", "unknown"))
    if session.get("agent_runtime") != "sage":
        return summarize_deletion_results([
            deletion_target_failed(
                target_kind="session_memory",
                target_id=session_id,
                action="delete_session_memory",
                detail="Session Memory deletion is Sage-owned; legacy Python runtime sessions are unsupported.",
                retryable=False,
            )
        ])

    try:
        deletion = await post_sage_session_memory_delete({"conversation_id": session_id})
        return _sanitize_lifecycle_deletion(deletion)
    except Exception as exc:
        return summarize_deletion_results([
            deletion_target_failed(
                target_kind="session_memory",
                target_id=session_id,
                action="delete_session_memory",
                detail=categorize_error(exc),
                retryable=True,
            )
        ])


def _former_subject_ref(session: dict) -> str | None:
    owner_type = session.get("owner_type")
    owner_id = session.get("owner_id")
    if owner_type == "user" and owner_id:
        return f"deleted_user:{owner_id}"
    if owner_type == "admin" and owner_id:
        return f"admin:{owner_id}"
    return None


def create_session_memory_tombstone(
    *,
    session: dict,
    source: str,
    workflow: str,
    deletion: dict,
) -> None:
    database.create_deletion_tombstone(
        lifecycle_data_class="sage_session_memory",
        conversation_id=str(session.get("id", "unknown")),
        former_subject_ref=_former_subject_ref(session),
        status="incomplete",
        source=source,
        workflow=workflow,
        deletion=deletion,
    )


def _parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO timestamp string into a naive UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def _audit_retention_run(*, changed_by: str, deletion: dict) -> dict | None:
    try:
        return database.log_config_audit_event(
            table_name="data_deletion",
            config_key=f"retention:{datetime.utcnow().isoformat()}",
            old_value=None,
            new_value=json.dumps({
                "workflow": "run_retention",
                "status": deletion["status"],
                "retryable": deletion["retryable"],
                "counts": deletion["counts"],
                "results": deletion["results"],
            }, sort_keys=True),
            changed_by=changed_by,
        )
    except Exception as exc:
        logger.warning(
            "Failed to audit retention run changed_by=%s deletion_status=%s: %s",
            changed_by,
            deletion.get("status"),
            exc,
            exc_info=True,
        )
    return None


def _retention_counts_by_class(results: list[dict]) -> dict:
    by_class: dict[str, dict[str, int]] = {}
    target_class_map = {
        "conversation": "sage_session_memory",
        "session_memory": "sage_session_memory",
        "user_memory": "user_memory",
        "audit_log": "audit_log",
        "document_artifact": "uploaded_document_artifacts",
        "document_metadata": "uploaded_document_artifacts",
        "retrieval_index": "uploaded_document_artifacts",
        "runtime_document_state": "uploaded_document_artifacts",
        "lifecycle_data_class": "lifecycle_data_class",
        "retention": "retention",
        "deletion_tombstone": "deletion_tombstone",
    }
    for result in results:
        if result.get("target_kind") == "lifecycle_data_class":
            class_key = str(result.get("target_id") or "lifecycle_data_class")
        else:
            class_key = target_class_map.get(
                str(result.get("target_kind", "")),
                str(result.get("target_kind") or "unknown"),
            )
        status = str(result.get("status") or "unknown")
        if class_key not in by_class:
            by_class[class_key] = {"succeeded": 0, "skipped": 0, "failed": 0}
        if status not in by_class[class_key]:
            by_class[class_key][status] = 0
        by_class[class_key][status] += 1
    return {"by_class": by_class}


def _retention_tombstone_refs(results: list[dict], retry_results: list[dict] | None = None) -> list[dict]:
    refs = []
    for result in retry_results or []:
        tombstone_id = result.get("tombstone_id")
        if tombstone_id is not None:
            refs.append({"tombstone_id": tombstone_id, "status": result.get("status")})
    return refs


def _retention_policy_snapshot(
    *,
    policies: dict[str, dict],
    trigger: str,
    retry_limit: int | None,
    evaluated_at: datetime,
) -> dict:
    safe_policies = {
        key: {
            "lifecycle_data_class": policy.get("lifecycle_data_class", key),
            "enabled": bool(policy.get("enabled")),
            "retention_window_days": int(policy.get("retention_window_days", 0)),
            "scheduled_enforcement_enabled": bool(policy.get("scheduled_enforcement_enabled")),
        }
        for key, policy in sorted(policies.items())
    }
    snapshot = {
        "trigger": trigger,
        "retry_limit": retry_limit,
        "evaluated_at": evaluated_at.isoformat(),
        "enabled_classes": sorted([
            key
            for key, policy in safe_policies.items()
            if policy.get("enabled")
        ]),
        "scheduled_enabled_classes": sorted([
            key
            for key, policy in safe_policies.items()
            if policy.get("enabled") and policy.get("scheduled_enforcement_enabled")
        ]),
        "policies": safe_policies,
    }
    snapshot["policy_hash"] = hashlib.sha256(
        json.dumps(snapshot["policies"], sort_keys=True).encode("utf-8")
    ).hexdigest()
    return snapshot


def _create_retention_run_record(
    *,
    trigger: str,
    actor: str,
    policy_snapshot: dict,
    deletion: dict,
    retry_results: list[dict] | None,
    audit_event: dict | None,
    started_at: datetime,
    finished_at: datetime,
) -> dict:
    results = list(deletion.get("results") or [])
    return database.create_retention_run_record(
        trigger=trigger,
        actor=actor,
        status=str(deletion.get("status") or "unknown"),
        policy_snapshot=policy_snapshot,
        counts=_retention_counts_by_class(results),
        results=results,
        tombstone_refs=_retention_tombstone_refs(results, retry_results),
        audit_log_id=audit_event.get("id") if audit_event else None,
        audit_entry_hash=audit_event.get("entry_hash") if audit_event else None,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
    )


def _audit_deletion_tombstone_retry(*, changed_by: str, tombstone: dict, deletion: dict) -> None:
    try:
        database.log_config_audit_event(
            table_name="data_deletion",
            config_key=f"deletion_tombstone:{tombstone['id']}:retry:{datetime.utcnow().isoformat()}",
            old_value=None,
            new_value=json.dumps({
                "workflow": "retry_deletion_tombstone",
                "status": deletion["status"],
                "retryable": deletion["retryable"],
                "tombstone_id": tombstone["id"],
                "conversation_id": tombstone["conversation_id"],
                "lifecycle_data_class": tombstone["lifecycle_data_class"],
                "counts": deletion["counts"],
                "results": deletion["results"],
            }, sort_keys=True),
            changed_by=changed_by,
        )
    except Exception as exc:
        logger.warning(
            "Failed to audit deletion tombstone retry tombstone_id=%s status=%s: %s",
            tombstone.get("id"),
            deletion.get("status"),
            exc,
            exc_info=True,
        )


def audit_lifecycle_deletion(
    *,
    config_key: str,
    changed_by: str,
    workflow: str,
    target_id: str,
    deletion: dict,
) -> None:
    try:
        database.log_config_audit_event(
            table_name="data_deletion",
            config_key=config_key,
            old_value=None,
            new_value=json.dumps({
                "workflow": workflow,
                "target_id": target_id,
                "status": deletion["status"],
                "retryable": deletion["retryable"],
                "counts": deletion["counts"],
                "results": deletion["results"],
            }, sort_keys=True),
            changed_by=changed_by,
        )
    except Exception as exc:
        logger.warning(
            "Failed to audit lifecycle deletion config_key=%s workflow=%s target_id=%s: %s",
            config_key,
            workflow,
            target_id,
            exc,
            exc_info=True,
        )


def _retention_policy_days(data_class_key: str, fallback_days: int) -> int:
    policy = _retention_policy_for(data_class_key)
    if policy["enabled"]:
        return int(policy.get("retention_window_days") or fallback_days)
    return fallback_days


def _stale_user_memory_ids(cutoff: datetime) -> list[int]:
    with database.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM user_memories
            WHERE (
                (status = 'active' AND retention_class = 'expirable')
                OR status = 'superseded'
                OR retention_class = 'superseded'
            )
              AND datetime(updated_at) <= datetime(?)
            ORDER BY updated_at ASC, id ASC
            LIMIT 1000
            """,
            (cutoff.isoformat(),),
        )
        return [int(row["id"]) for row in cursor.fetchall()]


def _document_artifact_candidates(cutoff: datetime) -> list[dict]:
    candidates: list[dict] = []
    known_artifact_paths: set[str] = set()
    job_limit = 1000
    job_offset = 0
    while True:
        jobs = ingest_db.list_jobs(limit=job_limit, offset=job_offset)
        if not jobs:
            break
        for job in jobs:
            file_path = job.get("file_path")
            if file_path:
                try:
                    known_artifact_paths.add(str(Path(file_path).resolve()))
                except OSError:
                    known_artifact_paths.add(file_path)

            reason = ingest._document_artifact_cleanup_reason(job)
            if not reason:
                continue
            updated_at = _parse_timestamp(job.get("updated_at"))
            if updated_at and updated_at <= cutoff:
                candidates.append({
                    "job_id": job["job_id"],
                    "filename": job.get("filename", "unknown"),
                    "reason": reason,
                    "job": job,
                })
        if len(jobs) < job_limit:
            break
        job_offset += job_limit

    candidates.extend(_orphaned_uploaded_document_artifacts(cutoff, known_artifact_paths))
    return candidates


def _orphaned_uploaded_document_artifacts(cutoff: datetime, known_artifact_paths: set[str]) -> list[dict]:
    uploads_root = ingest.UPLOADS_DIR
    try:
        resolved_root = uploads_root.resolve()
    except OSError:
        return []
    if not resolved_root.exists():
        return []

    candidates: list[dict] = []
    for path in sorted(resolved_root.iterdir()):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        try:
            resolved_path = path.resolve()
            file_stat = resolved_path.stat()
        except OSError:
            continue
        if str(resolved_path) in known_artifact_paths:
            continue
        if stat.S_ISREG(file_stat.st_mode) is False:
            continue
        modified_at = datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).replace(tzinfo=None)
        if modified_at > cutoff:
            continue
        candidates.append({
            "job_id": f"orphaned-upload:{path.name}",
            "filename": path.name,
            "reason": "orphaned_uploaded_artifact",
            "job": {
                "job_id": f"orphaned-upload:{path.name}",
                "filename": path.name,
                "file_path": str(resolved_path),
                "status": "orphaned",
            },
        })
    return candidates


def preview_retention(request: RetentionRunRequest) -> dict:
    now = datetime.utcnow()
    stored_policies = _stored_retention_policies()
    session_memory_policy = _retention_policy_for("sage_session_memory")
    document_artifact_policy = _retention_policy_for("uploaded_document_artifacts")
    user_memory_policy = _retention_policy_for("user_memory")
    audit_log_policy = _retention_policy_for("audit_log")
    document_days = _retention_policy_days("uploaded_document_artifacts", request.document_artifact_days)
    user_memory_days = _retention_policy_days("user_memory", 30)
    audit_log_days = _retention_policy_days("audit_log", 30)
    document_cutoff = now - timedelta(days=document_days)
    user_memory_cutoff = now - timedelta(days=user_memory_days)
    audit_log_cutoff = now - timedelta(days=audit_log_days)

    stale_conversations = []
    skipped_conversations = []

    document_artifacts = []
    if document_artifact_policy["enabled"]:
        document_artifacts = [
            {
                "job_id": candidate["job_id"],
                "filename": candidate["filename"],
                "reason": candidate["reason"],
            }
            for candidate in _document_artifact_candidates(document_cutoff)
        ]

    user_memories = []
    if user_memory_policy["enabled"]:
        user_memories = _stale_user_memory_ids(user_memory_cutoff)

    audit_log_entries = []
    if audit_log_policy["enabled"]:
        audit_log_entries = database.list_config_audit_log_compaction_candidates(
            audit_log_cutoff.isoformat(),
        )

    skipped_classes = []
    if "sage_session_memory" in stored_policies and not session_memory_policy["enabled"]:
        skipped_classes.append("sage_session_memory")
    if "uploaded_document_artifacts" in stored_policies and not document_artifact_policy["enabled"]:
        skipped_classes.append("uploaded_document_artifacts")
    if "user_memory" in stored_policies and not user_memory_policy["enabled"]:
        skipped_classes.append("user_memory")
    if "audit_log" in stored_policies and not audit_log_policy["enabled"]:
        skipped_classes.append("audit_log")

    preview = {
        "status": "preview",
        "destructive": False,
        "eligible": {
            "stale_conversations": stale_conversations,
            "document_artifacts": document_artifacts,
            "user_memories": user_memories,
            "audit_log_entries": audit_log_entries,
        },
        "counts": {
            "stale_conversations": len(stale_conversations),
            "document_artifacts": len(document_artifacts),
            "user_memories": len(user_memories),
            "audit_log_entries": len(audit_log_entries),
            "skipped_classes": len(skipped_classes),
        },
        "skipped_conversations": skipped_conversations,
        "skipped_classes": skipped_classes,
    }
    preview["preview_token"] = _retention_preview_token(request, preview)
    return preview


def _retention_preview_token(request: RetentionRunRequest, preview: dict) -> str:
    token_payload = {
        "request": {
            "stale_conversation_days": request.stale_conversation_days,
            "document_artifact_days": request.document_artifact_days,
        },
        "eligible": preview["eligible"],
        "counts": preview["counts"],
        "skipped_conversations": preview["skipped_conversations"],
        "skipped_classes": preview["skipped_classes"],
        "retention_policies": _stored_retention_policies(),
    }
    serialized = json.dumps(token_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def run_retention(
    request: RetentionRunRequest,
    admin: dict,
    *,
    create_run_record: bool = True,
    trigger: str = "manual",
    require_preview_confirmation: bool = True,
) -> dict:
    started_at = datetime.utcnow()
    if require_preview_confirmation and not request.confirm_current_counts:
        current_preview = preview_retention(request)
        if request.preview_token != current_preview["preview_token"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "Manual Retention Execution requires a fresh preview token "
                        "or explicit current-count confirmation."
                    ),
                    "current_preview": current_preview,
                },
            )

    now = started_at
    stored_policies = _stored_retention_policies()
    session_memory_policy = _retention_policy_for("sage_session_memory")
    document_artifact_policy = _retention_policy_for("uploaded_document_artifacts")
    user_memory_policy = _retention_policy_for("user_memory")
    audit_log_policy = _retention_policy_for("audit_log")
    document_cutoff = now - timedelta(
        days=_retention_policy_days("uploaded_document_artifacts", request.document_artifact_days)
    )
    user_memory_cutoff = now - timedelta(days=_retention_policy_days("user_memory", 30))
    audit_log_cutoff = now - timedelta(days=_retention_policy_days("audit_log", 30))
    results = []
    retained = {
        "stale_conversations": [],
        "skipped_conversations": [],
        "document_artifacts": [],
        "user_memories": [],
        "audit_log_entries": 0,
    }

    if session_memory_policy["enabled"]:
        results.append(deletion_target_skipped(
            target_kind="lifecycle_data_class",
            target_id="sage_session_memory",
            action="retention_skip_sage_owned_discovery",
            detail="Skipped Sage Session Memory retention because Conversation session discovery is owned by Sage.",
        ))
    elif "sage_session_memory" in stored_policies:
        results.append(deletion_target_skipped(
            target_kind="lifecycle_data_class",
            target_id="sage_session_memory",
            action="retention_skip_disabled_policy",
            detail="Skipped Sage Session Memory retention because its Data Retention policy is disabled.",
        ))

    if document_artifact_policy["enabled"]:
        for candidate in _document_artifact_candidates(document_cutoff):
            job = candidate["job"]
            job_id = candidate["job_id"]
            deletion_response = await ingest._delete_document_job_artifacts(job_id, job)
            retained["document_artifacts"].append({
                "job_id": job_id,
                "filename": candidate["filename"],
                "reason": candidate["reason"],
                "deletion": deletion_response["deletion"],
            })
            results.extend(deletion_response["deletion"]["results"])
    elif "uploaded_document_artifacts" in stored_policies:
        results.append(deletion_target_skipped(
            target_kind="lifecycle_data_class",
            target_id="uploaded_document_artifacts",
            action="retention_skip_disabled_policy",
            detail="Skipped Uploaded Document Artifacts retention because its Data Retention policy is disabled.",
        ))

    if user_memory_policy["enabled"]:
        for memory_id in _stale_user_memory_ids(user_memory_cutoff):
            deleted = database.soft_delete_user_memory(
                memory_id,
                deleted_by_actor=f"retention:{admin.get('pubkey', 'unknown')}",
                deletion_reason="Data Retention policy removed stale User Memory.",
            )
            if deleted:
                retained["user_memories"].append(memory_id)
                results.append(deletion_target_succeeded(
                    target_kind="user_memory",
                    target_id=str(memory_id),
                    action="retention_delete_user_memory",
                    detail="Deleted stale User Memory by Data Retention policy.",
                ))
            else:
                results.append(deletion_target_skipped(
                    target_kind="user_memory",
                    target_id=str(memory_id),
                    action="retention_skip_user_memory",
                    detail="Skipped User Memory because it was no longer active.",
                ))
    elif "user_memory" in stored_policies:
        results.append(deletion_target_skipped(
            target_kind="lifecycle_data_class",
            target_id="user_memory",
            action="retention_skip_disabled_policy",
            detail="Skipped User Memory retention because its Data Retention policy is disabled.",
        ))

    if audit_log_policy["enabled"]:
        compaction = database.compact_config_audit_log_before(
            audit_log_cutoff.isoformat(),
            changed_by=admin.get("pubkey", "unknown"),
        )
        retained["audit_log_entries"] = compaction["compacted_entries"]
        results.append(deletion_target_succeeded(
            target_kind="audit_log",
            target_id=str(compaction["compacted_entries"]),
            action="retention_compact_audit_log",
            detail="Compacted old non-lifecycle Audit Log detail while preserving lifecycle evidence.",
        ))
    elif "audit_log" in stored_policies:
        results.append(deletion_target_skipped(
            target_kind="lifecycle_data_class",
            target_id="audit_log",
            action="retention_skip_disabled_policy",
            detail="Skipped Audit Log retention because its Data Retention policy is disabled.",
        ))

    if not results:
        results.append(deletion_target_skipped(
            target_kind="retention",
            target_id="run",
            action="run_retention",
            detail="No supported data classes were eligible for retention cleanup.",
        ))

    deletion = summarize_deletion_results(results)
    audit_event = _audit_retention_run(
        changed_by=admin.get("pubkey", "unknown"),
        deletion=deletion,
    )
    response = {
        "status": deletion["status"],
        "retained": retained,
        "deletion": deletion,
        "audit_event": audit_event,
    }
    if create_run_record:
        actor = admin.get("pubkey") or admin.get("actor") or "unknown"
        response["run_record"] = _create_retention_run_record(
            trigger=trigger,
            actor=actor,
            policy_snapshot=_retention_policy_snapshot(
                policies=stored_policies,
                trigger=trigger,
                retry_limit=None,
                evaluated_at=started_at,
            ),
            deletion=deletion,
            retry_results=[],
            audit_event=audit_event,
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )
    return response


async def run_scheduled_retention(request: ScheduledRetentionRunRequest, admin: dict) -> dict:
    started_at = datetime.utcnow()
    trigger = "machine" if admin.get("type") == "machine" else "manual"
    actor = admin.get("pubkey") or admin.get("actor") or "unknown"
    scheduled_policies = {
        key: policy
        for key, policy in _stored_retention_policies().items()
        if policy.get("enabled") and policy.get("scheduled_enforcement_enabled")
    }
    enabled_classes = sorted(scheduled_policies.keys())
    if not enabled_classes:
        deletion = summarize_deletion_results([
            deletion_target_skipped(
                target_kind="retention",
                target_id="scheduled",
                action="scheduled_retention_skip_no_enabled_classes",
                detail="No Lifecycle Data Classes have scheduled Retention Execution enabled.",
            )
        ])
        audit_event = _audit_retention_run(changed_by=actor, deletion=deletion)
        run_record = _create_retention_run_record(
            trigger=trigger,
            actor=actor,
            policy_snapshot=_retention_policy_snapshot(
                policies=scheduled_policies,
                trigger=trigger,
                retry_limit=request.retry_limit,
                evaluated_at=started_at,
            ),
            deletion=deletion,
            retry_results=[],
            audit_event=audit_event,
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )
        return {
            "status": "skipped",
            "enabled_classes": [],
            "deletion": deletion,
            "retry_results": [],
            "run_record": run_record,
            "retention": {"status": deletion["status"], "deletion": deletion, "retained": {}},
        }

    retention = await run_retention(
        RetentionRunRequest(
            stale_conversation_days=_retention_policy_days("sage_session_memory", 30),
            document_artifact_days=_retention_policy_days("uploaded_document_artifacts", 0),
        ),
        admin,
        create_run_record=False,
        trigger=trigger,
        require_preview_confirmation=False,
    )

    retry_results = []
    attempted_retries = 0
    if request.retry_limit > 0:
        for tombstone in database.list_deletion_tombstones(status="incomplete"):
            if len(retry_results) >= request.retry_limit:
                break
            if tombstone["lifecycle_data_class"] != "sage_session_memory":
                continue
            attempted_retries += 1
            deletion = await delete_session_memory_for_conversation({
                "id": tombstone["conversation_id"],
                "agent_runtime": "sage",
                "owner_type": "tombstone",
                "owner_id": tombstone.get("former_subject_ref"),
            })
            new_status = "completed" if deletion["status"] == "succeeded" else "incomplete"
            updated = database.update_deletion_tombstone_after_retry(
                tombstone["id"],
                status=new_status,
                deletion=deletion,
            )
            if updated is not None:
                _audit_deletion_tombstone_retry(
                    changed_by=admin.get("pubkey", "unknown"),
                    tombstone=updated,
                    deletion=deletion,
                )
                retry_results.append({
                    "tombstone_id": updated["id"],
                    "status": updated["status"],
                    "deletion": deletion,
                })

    final_status = retention["status"]
    if attempted_retries > len(retry_results) or any(
        result.get("status") != "completed"
        for result in retry_results
    ):
        final_status = "incomplete"

    run_deletion = retention["deletion"]
    if final_status != retention["status"]:
        run_deletion = {
            **run_deletion,
            "status": final_status,
        }
    run_record = _create_retention_run_record(
        trigger=trigger,
        actor=actor,
        policy_snapshot=_retention_policy_snapshot(
            policies=scheduled_policies,
            trigger=trigger,
            retry_limit=request.retry_limit,
            evaluated_at=started_at,
        ),
        deletion=run_deletion,
        retry_results=retry_results,
        audit_event=retention.get("audit_event"),
        started_at=started_at,
        finished_at=datetime.utcnow(),
    )
    return {
        "status": final_status,
        "enabled_classes": enabled_classes,
        "deletion": run_deletion,
        "retry_results": retry_results,
        "run_record": run_record,
        "retention": retention,
    }


@router.get("/status", response_model=dict)
async def get_admin_lifecycle_status(response: Response, _admin: dict = Depends(auth.require_admin)) -> dict:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return get_lifecycle_status()


@router.get("/audit-coverage", response_model=dict)
async def get_admin_audit_coverage(_admin: dict = Depends(auth.require_admin)):
    return get_audit_coverage_inventory()


@router.post("/readiness/review", response_model=dict)
async def review_admin_lifecycle_readiness(admin: dict = Depends(auth.require_admin)):
    return {
        "lifecycle_readiness": _review_lifecycle_readiness(
            changed_by=admin.get("pubkey", "unknown"),
        )
    }


@router.post("/unsupported-deployment-surfaces/{surface_key}/acknowledgement", response_model=dict)
async def update_admin_unsupported_deployment_surface_acknowledgement(
    surface_key: str,
    request: UnsupportedSurfaceAcknowledgementRequest,
    admin: dict = Depends(auth.require_admin),
):
    return {
        "unsupported_deployment_surfaces": _set_unsupported_surface_acknowledgement(
            surface_key,
            request.acknowledged,
            changed_by=admin.get("pubkey", "unknown"),
        )
    }


@router.post("/unsupported-deployment-surface-categories/{category}/acknowledgement", response_model=dict)
async def update_admin_unsupported_deployment_surface_category_acknowledgement(
    category: str,
    request: UnsupportedSurfaceAcknowledgementRequest,
    admin: dict = Depends(auth.require_admin),
):
    return {
        "unsupported_deployment_surface_categories": _set_unsupported_surface_category_acknowledgement(
            category,
            request.acknowledged,
            changed_by=admin.get("pubkey", "unknown"),
        )
    }


@router.put("/retention-policies/{data_class_key}", response_model=dict)
async def update_admin_retention_policy(
    data_class_key: str,
    request: RetentionPolicyUpdateRequest,
    admin: dict = Depends(auth.require_admin),
):
    return {
        "policy": _update_retention_policy(
            data_class_key,
            request,
            changed_by=admin.get("pubkey", "unknown"),
        )
    }


@router.put("/artifact-encryption-posture", response_model=dict)
async def update_admin_artifact_encryption_posture(
    request: ArtifactEncryptionPostureUpdateRequest,
    admin: dict = Depends(auth.require_admin),
):
    posture = request.posture.strip().lower()
    if posture not in {"required", "disabled"}:
        raise HTTPException(status_code=400, detail="Unsupported Artifact Encryption Posture")
    database.upsert_deployment_config(
        content_artifacts.ARTIFACT_ENCRYPTION_KEY,
        posture,
        is_secret=False,
        requires_restart=False,
        category="storage",
        description="Artifact Encryption Posture (required or disabled)",
        changed_by=admin.get("pubkey", "unknown"),
    )
    return {"artifact_encryption": _artifact_encryption_status()}


@router.get("/confidentiality-migration/preview", response_model=dict)
async def preview_admin_confidentiality_migration(_admin: dict = Depends(auth.require_admin)):
    return preview_confidentiality_migration()


@router.post("/confidentiality-migration/execute", response_model=dict)
async def execute_admin_confidentiality_migration(admin: dict = Depends(auth.require_admin)):
    return execute_confidentiality_migration(actor=admin.get("pubkey", "unknown"))


@router.post("/retention/preview", response_model=dict)
async def preview_admin_retention(
    request: RetentionRunRequest,
    _admin: dict = Depends(auth.require_admin),
):
    return preview_retention(request)


@router.get("/deletion-tombstones", response_model=dict)
async def list_admin_deletion_tombstones(
    status: str | None = None,
    _admin: dict = Depends(auth.require_admin),
):
    if status is not None and status not in {"incomplete", "completed"}:
        raise HTTPException(status_code=400, detail="Unsupported deletion tombstone status")
    return {"tombstones": database.list_deletion_tombstones(status=status)}


@router.get("/retention-runs", response_model=dict)
async def list_admin_retention_runs(
    limit: int = Query(default=100, ge=1, le=500),
    _admin: dict = Depends(auth.require_admin),
):
    return {"runs": database.list_retention_run_records(limit=limit)}


@router.get("/retention-runs/{run_id}", response_model=dict)
async def get_admin_retention_run(
    run_id: int,
    _admin: dict = Depends(auth.require_admin),
):
    run = database.get_retention_run_record(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Retention Run Record not found")
    return {"run": run}


@router.post("/deletion-tombstones/{tombstone_id}/retry", response_model=dict)
async def retry_admin_deletion_tombstone(
    tombstone_id: int,
    admin: dict = Depends(auth.require_admin),
):
    tombstone = database.get_deletion_tombstone(tombstone_id)
    if not tombstone:
        raise HTTPException(status_code=404, detail="Deletion tombstone not found")
    if tombstone["lifecycle_data_class"] not in {"sage_session_memory", "sage_user_runtime"}:
        raise HTTPException(status_code=400, detail="Unsupported deletion tombstone class")
    if tombstone["status"] == "completed":
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Deletion tombstone is already completed",
                "tombstone": tombstone,
            },
        )
    if tombstone["lifecycle_data_class"] == "sage_user_runtime":
        deletion = await complete_user_runtime_cleanup(tombstone)
        updated = database.get_deletion_tombstone(tombstone_id)
    else:
        deletion = await delete_session_memory_for_conversation({
            "id": tombstone["conversation_id"],
            "agent_runtime": "sage",
            "owner_type": "tombstone",
            "owner_id": tombstone.get("former_subject_ref"),
        })
        updated = database.update_deletion_tombstone_after_retry(
            tombstone_id,
            status="completed" if deletion["status"] == "succeeded" else "incomplete",
            deletion=deletion,
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="Deletion tombstone not found")
    _audit_deletion_tombstone_retry(
        changed_by=admin.get("pubkey", "unknown"),
        tombstone=updated,
        deletion=deletion,
    )
    return {
        "status": deletion["status"],
        "retryable": deletion["retryable"],
        "tombstone": updated,
        "deletion": deletion,
    }


@router.post("/retention/run", response_model=dict)
async def run_admin_retention(
    request: RetentionRunRequest,
    admin: dict = Depends(auth.require_admin),
):
    return await run_retention(request, admin)


@router.post("/retention/scheduled/run", response_model=dict)
async def run_admin_scheduled_retention(
    request: ScheduledRetentionRunRequest,
    admin: dict = Depends(auth.require_admin),
):
    return await run_scheduled_retention(request, admin)


@router.post("/retention/scheduled/automation/run", response_model=dict)
async def run_automation_scheduled_retention(
    request: ScheduledRetentionRunRequest,
    machine_actor: dict = Depends(_require_retention_automation_actor),
):
    return await run_scheduled_retention(request, machine_actor)
