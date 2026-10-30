from __future__ import annotations

"""
Enclave Ingest Router
Handles document upload, chunking, and storage to Qdrant.

Job state is persisted to SQLite (via ingest_db module) to survive container restarts.
Chunks remain in-memory during processing for performance.
"""

import os
import uuid
import json
import hashlib
import logging
import math
import random
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, BackgroundTasks, Depends, Request
from pydantic import BaseModel

import auth
import content_artifacts
from data_deletion import (
    deletion_target_failed,
    deletion_target_skipped,
    deletion_target_succeeded,
    summarize_deletion_results,
)
import database
import ingest_db
from rate_limit import RateLimiter
from rate_limit_key import rate_limit_key as _stable_rate_limit_key
from models import (
    DocumentDefaultItem,
    DocumentDefaultsResponse,
    DocumentDefaultUpdate,
    DocumentDefaultsBatchUpdate,
    DocumentDefaultWithInheritance,
    DocumentDefaultsUserTypeResponse,
    DocumentDefaultOverrideUpdate,
    SuccessResponse,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("enclave.ingest")

router = APIRouter(prefix="/ingest", tags=["ingest"])


# Processing configuration
MAX_CONCURRENT_CHUNKS = int(os.getenv("MAX_CONCURRENT_CHUNKS", "3"))
UPLOAD_RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_UPLOAD_PER_MINUTE", "20"))
MAX_BATCH_FILES = int(os.getenv("MAX_BATCH_FILES", "100"))
MAX_BATCH_BYTES = int(os.getenv("MAX_BATCH_BYTES", str(250 * 1024 * 1024)))
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".txt", ".md"}
ABANDONED_INGESTION_TIMEOUT_MINUTES = int(os.getenv("ABANDONED_INGESTION_TIMEOUT_MINUTES", "60"))

# Valid ontology IDs for document extraction
VALID_ONTOLOGIES = {"general", "bitcoin"}

# Configuration
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", "/uploads"))
CHUNKS_DIR = UPLOADS_DIR / "chunks"
PROCESSED_DIR = UPLOADS_DIR / "processed"

# Ensure directories exist
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# In-memory state for active processing
# Jobs are persisted to SQLite; CHUNKS remain in-memory during processing
JOBS: dict = {}  # Loaded from SQLite on startup
CHUNKS: dict = {}  # In-memory only (not persisted for now)
TASKS: dict[str, asyncio.Task] = {}


def require_admin_pubkey(admin: dict) -> str:
    pubkey = admin.get("pubkey")
    if not pubkey:
        raise HTTPException(status_code=500, detail="Authenticated admin pubkey is missing")
    return str(pubkey)


def _rate_limit_key(request: Request) -> str:
    """Prefer auth identity for rate limiting; fallback to client IP."""
    return _stable_rate_limit_key(request)


upload_limiter = RateLimiter(
    limit=UPLOAD_RATE_LIMIT_PER_MINUTE,
    window_seconds=60,
    key_func=_rate_limit_key,
)


def _load_jobs_from_db() -> dict:
    """Load all jobs from SQLite into memory."""
    jobs = {}
    for job in ingest_db.list_jobs(limit=1000):
        jobs[job["job_id"]] = {
            "job_id": job["job_id"],
            "filename": job["filename"],
            "file_path": job["file_path"],
            "status": job["status"],
            "ontology_id": job.get("ontology_id", "general"),
            "sample_percent": job.get("sample_percent", 100.0),
            "canonical_name": job.get("canonical_name") or job["filename"],
            "replacement_for_job_id": job.get("replacement_for_job_id"),
            "replaced_by_job_id": job.get("replaced_by_job_id"),
            "is_current": bool(job.get("is_current", 1)),
            "content_sha256": job.get("content_sha256"),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "total_chunks": job["total_chunks"],
            "processed_chunks": job["processed_chunks"],
            "failed_chunks": job["failed_chunks"],
            "error": job["error"],
        }
    return jobs


def _sync_job_to_db(job_id: str) -> None:
    """Sync a single job's state to SQLite."""
    job = JOBS.get(job_id)
    if not job:
        return
    
    # Check if job exists in DB
    if not ingest_db.job_exists(job_id):
        # Create new job
        ingest_db.create_job(
            job_id=job_id,
            filename=job["filename"],
            file_path=job["file_path"],
            ontology_id=job.get("ontology_id", "general"),
            sample_percent=job.get("sample_percent", 100.0),
            canonical_name=job.get("canonical_name") or job["filename"],
            replacement_for_job_id=job.get("replacement_for_job_id"),
            content_sha256=job.get("content_sha256"),
            is_current=bool(job.get("is_current", True)),
        )
    
    # Update status
    ingest_db.update_job_status(
        job_id=job_id,
        status=job["status"],
        total_chunks=job.get("total_chunks"),
        processed_chunks=job.get("processed_chunks"),
        failed_chunks=job.get("failed_chunks"),
        error=job.get("error"),
    )


def _clear_job_chunks(job_id: str) -> None:
    """Remove in-memory chunks for a job (used on resume)."""
    to_delete = [cid for cid, c in CHUNKS.items() if c.get("job_id") == job_id]
    for cid in to_delete:
        CHUNKS.pop(cid, None)


class IngestionJob(TypedDict, total=False):
    id: str
    job_id: str
    status: str
    filename: str
    file_path: str
    total_chunks: int
    processed_chunks: int
    failed_chunks: int
    error: str | None
    updated_at: str
    replaced_by_job_id: str | None
    is_current: int | bool
    sample_percent: float
    metadata: dict[str, Any]


def _document_artifact_cleanup_reason(job: IngestionJob) -> str | None:
    """Return why a job is eligible for lifecycle artifact cleanup."""
    status = job.get("status")
    job_id = str(job.get("job_id") or job.get("id") or "")
    active_task = TASKS.get(job_id)
    if active_task is not None and not active_task.done():
        return None
    if status == "failed":
        return "failed_ingestion"
    if status in {"pending", "processing"} and _is_abandoned_ingestion_job(job):
        return "abandoned_ingestion"
    if job.get("replaced_by_job_id") and not bool(job.get("is_current", 1)):
        return "superseded_document"
    return None


def _is_abandoned_ingestion_job(job: IngestionJob) -> bool:
    job_id = str(job.get("job_id") or job.get("id") or "")
    active_task = TASKS.get(job_id)
    if active_task is not None and not active_task.done():
        return False
    updated_at = job.get("updated_at")
    if not updated_at:
        return False
    try:
        updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    else:
        updated = updated.astimezone(timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ABANDONED_INGESTION_TIMEOUT_MINUTES)
    return updated <= cutoff


def _audit_document_action(
    *,
    config_key: str,
    action: str,
    changed_by: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    try:
        database.log_config_audit_event(
            table_name="document_actions",
            config_key=config_key,
            old_value=json.dumps(old_value, sort_keys=True) if old_value is not None else None,
            new_value=json.dumps({"action": action, **(new_value or {})}, sort_keys=True),
            changed_by=changed_by,
        )
    except Exception as exc:
        logger.error(
            "Failed to audit document action config_key=%s action=%s: %s",
            config_key,
            action,
            exc,
            exc_info=True,
        )


def _audit_data_deletion(
    *,
    config_key: str,
    changed_by: str,
    deletion: dict,
    workflow: str,
    target_id: str,
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
        logger.error(
            "Failed to audit data deletion config_key=%s workflow=%s target_id=%s: %s",
            config_key,
            workflow,
            target_id,
            exc,
            exc_info=True,
        )


def _missing_document_deletion_response(job_id: str) -> dict:
    return {
        "status": "deleted",
        "job_id": job_id,
        "deletion": summarize_deletion_results([
            deletion_target_skipped(
                target_kind="document_metadata",
                target_id=job_id,
                action="delete_document_metadata",
                detail="Document metadata was already absent.",
            ),
            deletion_target_skipped(
                target_kind="uploaded_document_artifact",
                target_id=job_id,
                action="delete_uploaded_document_artifact",
                detail="Uploaded artifact path is unavailable because the document is already absent.",
            ),
            deletion_target_skipped(
                target_kind="retrieval_index",
                target_id=job_id,
                action="delete_retrieval_index",
                detail="Retrieval entries were already absent or cannot be located for an absent document.",
            ),
            deletion_target_skipped(
                target_kind="retrieval_chunk_text",
                target_id=job_id,
                action="delete_retrieval_chunk_text",
                detail="Encrypted retrieval chunk text was already absent.",
            ),
            deletion_target_skipped(
                target_kind="runtime_document_state",
                target_id=job_id,
                action="delete_runtime_document_state",
                detail="Runtime document state was already absent.",
            ),
        ]),
    }


async def _delete_document_job_artifacts(job_id: str, job: dict) -> dict:
    """Delete all lifecycle-managed artifacts for one non-processing document job."""
    logger.info(f"[{job_id}] Starting document deletion...")
    results = []

    try:
        deleted_count = await delete_document_chunks(job_id)
        results.append(deletion_target_succeeded(
            target_kind="retrieval_index",
            target_id=job_id,
            action="delete_retrieval_index",
            detail=f"Deleted {deleted_count} retrieval entries.",
        ))
        logger.info(f"[{job_id}] Deleted {deleted_count} chunks from Qdrant")
    except Exception as e:
        logger.error(f"[{job_id}] Failed to delete from Qdrant: {e}")
        results.append(deletion_target_failed(
            target_kind="retrieval_index",
            target_id=job_id,
            action="delete_retrieval_index",
            retryable=True,
            detail=f"Failed to delete document chunks from vector database: {e}",
        ))

    try:
        deleted_text_rows = ingest_db.delete_retrieval_chunks_for_job(job_id)
        results.append(deletion_target_succeeded(
            target_kind="retrieval_chunk_text",
            target_id=job_id,
            action="delete_retrieval_chunk_text",
            detail=f"Deleted {deleted_text_rows} encrypted retrieval chunk rows.",
        ))
    except Exception as e:
        logger.error(f"[{job_id}] Failed to delete encrypted retrieval chunk text: {e}")
        results.append(deletion_target_failed(
            target_kind="retrieval_chunk_text",
            target_id=job_id,
            action="delete_retrieval_chunk_text",
            retryable=True,
            detail=f"Failed to delete encrypted retrieval chunk text: {e}",
        ))

    file_path_value = job.get("file_path")
    file_path = Path(file_path_value) if file_path_value else None
    if file_path:
        try:
            resolved_file_path = file_path.resolve()
            uploads_root = UPLOADS_DIR.resolve()
            if not resolved_file_path.is_relative_to(uploads_root):
                logger.warning(
                    "[%s] Refusing to delete uploaded artifact outside uploads root: %s",
                    job_id,
                    resolved_file_path,
                )
                results.append(deletion_target_failed(
                    target_kind="uploaded_document_artifact",
                    target_id=str(resolved_file_path),
                    action="delete_uploaded_document_artifact",
                    retryable=False,
                    detail="Uploaded artifact path is outside the configured uploads directory.",
                ))
            elif resolved_file_path.exists():
                resolved_file_path.unlink()
                results.append(deletion_target_succeeded(
                    target_kind="uploaded_document_artifact",
                    target_id=str(resolved_file_path),
                    action="delete_uploaded_document_artifact",
                    detail="Deleted uploaded document artifact.",
                ))
                logger.info(f"[{job_id}] Deleted file: {resolved_file_path}")
            else:
                logger.debug(f"[{job_id}] File not found (already deleted?): {resolved_file_path}")
                results.append(deletion_target_skipped(
                    target_kind="uploaded_document_artifact",
                    target_id=str(resolved_file_path),
                    action="delete_uploaded_document_artifact",
                    detail="Uploaded document artifact was already absent.",
                ))
        except Exception as e:
            logger.error(f"[{job_id}] Failed to delete file {file_path}: {e}")
            results.append(deletion_target_failed(
                target_kind="uploaded_document_artifact",
                target_id=str(file_path),
                action="delete_uploaded_document_artifact",
                retryable=True,
                detail=f"Failed to delete uploaded document artifact: {e}",
            ))
    else:
        results.append(deletion_target_skipped(
            target_kind="uploaded_document_artifact",
            target_id=job_id,
            action="delete_uploaded_document_artifact",
            detail="Uploaded document artifact was already absent.",
        ))

    has_failures = any(result["status"] == "failed" for result in results)
    try:
        if has_failures:
            results.append(deletion_target_skipped(
                target_kind="document_metadata",
                target_id=job_id,
                action="delete_document_metadata",
                detail="Document metadata retained because at least one deletion target failed.",
            ))
        else:
            db_deleted = ingest_db.delete_job(job_id)
            if db_deleted:
                results.append(deletion_target_succeeded(
                    target_kind="document_metadata",
                    target_id=job_id,
                    action="delete_document_metadata",
                    detail="Deleted document metadata and access defaults.",
                ))
            else:
                results.append(deletion_target_skipped(
                    target_kind="document_metadata",
                    target_id=job_id,
                    action="delete_document_metadata",
                    detail="Document metadata was already absent.",
                ))
            logger.info(f"[{job_id}] Deleted from SQLite: {db_deleted}")
    except Exception as e:
        logger.error(f"[{job_id}] Failed to delete from SQLite: {e}")
        results.append(deletion_target_failed(
            target_kind="document_metadata",
            target_id=job_id,
            action="delete_document_metadata",
            retryable=True,
            detail=f"Failed to delete job from database: {e}",
        ))

    has_failures = any(result["status"] == "failed" for result in results)
    if has_failures:
        results.append(deletion_target_skipped(
            target_kind="runtime_document_state",
            target_id=job_id,
            action="delete_runtime_document_state",
            detail="Runtime document state retained because at least one deletion target failed.",
        ))
    else:
        runtime_job_deleted = JOBS.pop(job_id, None) is not None
        chunks_to_delete = [cid for cid, c in CHUNKS.items() if c.get("job_id") == job_id]
        for cid in chunks_to_delete:
            CHUNKS.pop(cid, None)
        logger.info(f"[{job_id}] Cleared {len(chunks_to_delete)} in-memory chunks")
        if runtime_job_deleted or chunks_to_delete:
            results.append(deletion_target_succeeded(
                target_kind="runtime_document_state",
                target_id=job_id,
                action="delete_runtime_document_state",
                detail=f"Cleared runtime job state and {len(chunks_to_delete)} in-memory chunks.",
            ))
        else:
            results.append(deletion_target_skipped(
                target_kind="runtime_document_state",
                target_id=job_id,
                action="delete_runtime_document_state",
                detail="Runtime document state was already absent.",
            ))

    logger.info(f"[{job_id}] Document deletion complete")
    deletion = summarize_deletion_results(results)
    return {
        "status": "deleted" if deletion["status"] == "succeeded" else deletion["status"],
        "job_id": job_id,
        "filename": job.get("filename", "unknown"),
        "deletion": deletion,
    }


def schedule_document_processing(job_id: str, file_path: Path, sample_percent: float) -> None:
    """Schedule background document processing."""
    task = asyncio.create_task(process_document(job_id, file_path, sample_percent))
    TASKS[job_id] = task

    def _finish_task(done_task: asyncio.Task) -> None:
        TASKS.pop(job_id, None)
        try:
            done_task.result()
        except asyncio.CancelledError:
            logger.info(f"[{job_id}] Background document processing task was cancelled")
        except Exception as exc:
            logger.error(f"[{job_id}] Background document processing task failed: {exc}", exc_info=True)
            if job_id in JOBS:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = str(exc)
                JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
                _sync_job_to_db(job_id)

    task.add_done_callback(_finish_task)


async def load_jobs_and_resume() -> None:
    """Load jobs from SQLite on startup and resume incomplete jobs."""
    global JOBS

    # Load from SQLite
    JOBS = _load_jobs_from_db()
    logger.info(f"Loaded {len(JOBS)} jobs from SQLite")
    
    # Resume incomplete jobs
    for job_id, job in list(JOBS.items()):
        status = job.get("status")
        if status in ("pending", "processing"):
            file_path = Path(job.get("file_path", ""))
            if not file_path.exists():
                job["status"] = "failed"
                job["error"] = "Source file missing; cannot resume"
                job["updated_at"] = datetime.utcnow().isoformat()
                _sync_job_to_db(job_id)
                continue
            logger.warning(f"[{job_id}] Resuming job after restart")
            _clear_job_chunks(job_id)
            schedule_document_processing(
                job_id=job_id,
                file_path=file_path,
                sample_percent=float(job.get("sample_percent", 100.0)),
            )


# =============================================================================
# MODELS
# =============================================================================

class UploadResponse(BaseModel):
    job_id: str
    filename: str
    status: str
    message: str
    replacement_for_job_id: Optional[str] = None
    replacement_for_filename: Optional[str] = None


class BatchRejectedItem(BaseModel):
    filename: str
    reason: str


class BatchUploadResponse(BaseModel):
    accepted: list[UploadResponse]
    rejected: list[BatchRejectedItem]


class JobStatus(BaseModel):
    job_id: str
    filename: str
    status: str  # pending, processing, completed, failed
    ontology_id: str
    created_at: str
    updated_at: str
    total_chunks: int
    processed_chunks: int
    error: Optional[str] = None
    canonical_name: Optional[str] = None
    replacement_for_job_id: Optional[str] = None
    replacement_for_filename: Optional[str] = None
    replaced_by_job_id: Optional[str] = None
    is_current: bool = True


class OntologiesResponse(BaseModel):
    ontologies: list[str]
    default: str


class ChunkInfo(BaseModel):
    chunk_id: str
    job_id: str
    index: int
    text: str
    char_count: int
    status: str  # pending, stored
    source_file: str


class ChunkListResponse(BaseModel):
    total: int
    pending: int
    extracted: int
    stored: int
    chunks: list[ChunkInfo]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def generate_job_id(filename: str) -> str:
    """Generate a unique job ID"""
    timestamp = datetime.utcnow().isoformat()
    content = f"{filename}:{timestamp}:{uuid.uuid4()}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def generate_chunk_id(job_id: str, index: int) -> str:
    """Generate a unique chunk ID"""
    return f"{job_id}_chunk_{index:04d}"


def sanitize_document_name(name: str) -> str:
    """Normalize an operator-facing document name without trusting it as a path."""
    raw = (name or "").replace("\\", "/").strip()
    if raw.startswith("/") or raw.startswith("~"):
        raise ValueError("Document path must be relative")

    parts: list[str] = []
    for part in raw.split("/"):
        clean = part.strip()
        if not clean or clean in (".", ".."):
            if clean == "..":
                raise ValueError("Document path cannot contain '..'")
            continue
        parts.append(clean)

    normalized = "/".join(parts)
    if not normalized:
        raise ValueError("Document name cannot be empty")
    return normalized


def canonical_name_for_upload(filename: str, relative_path: str | None = None) -> str:
    """Resolve the canonical document name used for deduplication."""
    source_name = relative_path or Path(filename).name
    return sanitize_document_name(source_name)


def safe_storage_filename(filename: str) -> str:
    """Return a filesystem-safe basename while preserving the original extension."""
    name = Path((filename or "document").replace("\\", "/")).name
    sanitized = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in name)
    return sanitized or "document"


def replacement_filename(job: dict) -> str | None:
    """Resolve the filename of the document being replaced, if any."""
    old_job_id = job.get("replacement_for_job_id")
    if not old_job_id:
        return None
    old_job = JOBS.get(old_job_id) or ingest_db.get_job(old_job_id)
    return old_job.get("filename") if old_job else None


def validate_ingest_options(ontology_id: str, sample_percent: float) -> None:
    """Validate shared upload form options."""
    if sample_percent <= 0 or sample_percent > 100:
        raise HTTPException(
            status_code=400,
            detail="sample_percent must be > 0 and <= 100"
        )

    if ontology_id not in VALID_ONTOLOGIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ontology_id: {ontology_id}. Valid options: {sorted(VALID_ONTOLOGIES)}"
        )


async def queue_document_ingestion(
    *,
    original_filename: str,
    canonical_name: str,
    content: bytes,
    ontology_id: str,
    sample_percent: float,
    changed_by: str,
) -> UploadResponse:
    """Create a durable ingest job and start background processing."""
    current_job = ingest_db.get_current_job_by_canonical_name(canonical_name)
    inflight_replacement = ingest_db.get_inflight_replacement_by_canonical_name(canonical_name)
    if inflight_replacement:
        raise ValueError("Replacement already in progress for this document")

    replacement_for_job_id = current_job["job_id"] if current_job else None
    replacement_for_filename = current_job["filename"] if current_job else None

    content_sha256 = hashlib.sha256(content).hexdigest()
    job_id = generate_job_id(canonical_name)
    file_path = UPLOADS_DIR / f"{job_id}_{safe_storage_filename(original_filename)}"
    try:
        storage_content = content_artifacts.encode_for_storage(content)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        await asyncio.to_thread(file_path.write_bytes, storage_content)
    except OSError as exc:
        logger.exception("Failed to write uploaded artifact for %s to %s", job_id, file_path)
        _audit_document_action(
            config_key=f"document:{job_id}:write_artifact",
            action="write_artifact_failed",
            changed_by=changed_by,
            old_value=None,
            new_value={"filename": canonical_name, "error": str(exc)},
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    now = datetime.utcnow().isoformat()
    JOBS[job_id] = {
        "job_id": job_id,
        "filename": canonical_name,
        "file_path": str(file_path),
        "status": "pending",
        "ontology_id": ontology_id,
        "sample_percent": sample_percent,
        "canonical_name": canonical_name,
        "replacement_for_job_id": replacement_for_job_id,
        "replaced_by_job_id": None,
        "is_current": replacement_for_job_id is None,
        "content_sha256": content_sha256,
        "created_at": now,
        "updated_at": now,
        "total_chunks": 0,
        "processed_chunks": 0,
        "failed_chunks": 0,
        "error": None,
    }
    _sync_job_to_db(job_id)
    if replacement_for_job_id is None:
        try:
            database.upsert_document_defaults(
                job_id=job_id,
                is_available=True,
                is_default_active=True,
                display_order=0,
                changed_by=changed_by,
            )
        except Exception as exc:
            logger.error(
                "[%s] Failed to initialize document defaults for upload by %s",
                job_id,
                changed_by,
                exc_info=True,
            )
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = str(exc)
            JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
            _sync_job_to_db(job_id)
            raise

    schedule_document_processing(job_id, file_path, sample_percent)
    _audit_document_action(
        config_key=f"document:{job_id}:queue",
        action="replace_document" if replacement_for_job_id else "upload_document",
        changed_by=changed_by,
        old_value={
            "job_id": replacement_for_job_id,
            "filename": replacement_for_filename,
        } if replacement_for_job_id else None,
        new_value={
            "job_id": job_id,
            "filename": canonical_name,
            "ontology_id": ontology_id,
            "sample_percent": sample_percent,
            "replacement_for_job_id": replacement_for_job_id,
            "content_sha256": content_sha256,
        },
    )

    return UploadResponse(
        job_id=job_id,
        filename=canonical_name,
        status="pending",
        message=(
            "Document replacement queued for processing"
            if replacement_for_job_id
            else "Document queued for processing"
        ),
        replacement_for_job_id=replacement_for_job_id,
        replacement_for_filename=replacement_for_filename,
    )


def chunk_text(text: str, chunk_size: int = 1500, overlap: int = 200) -> list[str]:
    """
    Simple chunking by character count with overlap.
    In production, use semantic chunking based on document structure.
    """
    chunks = []
    start = 0
    
    while start < len(text):
        end = start + chunk_size
        
        # Try to break at paragraph or sentence boundary
        if end < len(text):
            # Look for paragraph break
            para_break = text.rfind('\n\n', start, end)
            if para_break > start + chunk_size // 2:
                end = para_break
            else:
                # Look for sentence break
                for sep in ['. ', '.\n', '? ', '?\n', '! ', '!\n']:
                    sent_break = text.rfind(sep, start, end)
                    if sent_break > start + chunk_size // 2:
                        end = sent_break + len(sep)
                        break
        
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        
        # Move start with overlap
        start = end - overlap if end < len(text) else len(text)
    
    return chunks


async def store_chunk(chunk_id: str, chunk_text_content: str, source_file: str) -> dict:
    """Store chunk directly to Qdrant."""
    from store import store_chunks_to_qdrant

    result = await store_chunks_to_qdrant(
        chunk_id=chunk_id,
        source_text=chunk_text_content,
        source_file=source_file,
    )
    return result


async def verify_embedding_provider_available() -> None:
    """Fail fast before starting per-chunk storage when embeddings are unavailable."""
    from store import get_embedding_dimension

    await asyncio.to_thread(get_embedding_dimension)


async def delete_document_chunks(job_id: str) -> int:
    """Delete stored vector chunks for a document job."""
    from store import delete_chunks_from_qdrant

    return await delete_chunks_from_qdrant(job_id)


async def process_document(job_id: str, file_path: Path, sample_percent: float):
    """
    Process an uploaded document: convert to text, chunk, and store to Qdrant.
    This runs as a background task.
    """
    logger.info(f"[{job_id}] Starting document processing: {file_path}")
    try:
        JOBS[job_id]["status"] = "processing"
        JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
        _sync_job_to_db(job_id)

        with content_artifacts.processing_path(file_path, temp_dir=UPLOADS_DIR) as processing_file_path:
            # Get file extension
            suffix = processing_file_path.suffix.lower()
            logger.debug(f"[{job_id}] File type: {suffix}")

            # Extract text based on file type
            # read_text/extract_pdf_text return a self-contained str; chunking happens after
            # content_artifacts.processing_path removes any temporary decrypted file.
            if suffix == ".pdf":
                logger.info(f"[{job_id}] Extracting text from PDF...")
                text = await asyncio.to_thread(extract_pdf_text, processing_file_path)
            elif suffix == ".txt":
                logger.info(f"[{job_id}] Reading text file...")
                text = processing_file_path.read_text(encoding="utf-8")
            elif suffix == ".md":
                logger.info(f"[{job_id}] Reading markdown file...")
                text = processing_file_path.read_text(encoding="utf-8")
            else:
                raise ValueError(f"Unsupported file type: {suffix}")

        logger.info(f"[{job_id}] Extracted {len(text)} characters")

        # Chunk the text
        logger.info(f"[{job_id}] Chunking text...")
        chunks = chunk_text(text)
        logger.info(f"[{job_id}] Created {len(chunks)} chunks")

        # Optionally sample a percentage of chunks for faster testing
        if sample_percent < 100:
            target_count = max(1, math.ceil(len(chunks) * (sample_percent / 100.0)))
            chunks = random.sample(chunks, k=min(target_count, len(chunks)))
            logger.info(
                f"[{job_id}] Sampling {sample_percent}% -> {len(chunks)} chunks selected"
            )

        # Store chunks metadata
        source_file = JOBS[job_id].get("filename") or file_path.name
        for i, chunk_text_content in enumerate(chunks):
            chunk_id = generate_chunk_id(job_id, i)
            CHUNKS[chunk_id] = {
                "chunk_id": chunk_id,
                "job_id": job_id,
                "index": i,
                "text": chunk_text_content,
                "char_count": len(chunk_text_content),
                "status": "pending",
                "source_file": source_file,
                "created_at": datetime.utcnow().isoformat(),
            }

        # Update job status
        JOBS[job_id]["total_chunks"] = len(chunks)
        JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
        _sync_job_to_db(job_id)
        logger.info(f"[{job_id}] Chunking complete: {len(chunks)} chunks created, starting storage...")

        try:
            await verify_embedding_provider_available()
        except Exception as e:
            message = f"Embedding provider unavailable: {e}"
            logger.error("[%s] %s", job_id, message, exc_info=True)
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = message
            JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
            _sync_job_to_db(job_id)
            return

        # Process chunks with limited concurrency
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHUNKS)
        processed = 0
        failed = 0

        async def run_chunk(chunk_id: str):
            nonlocal processed, failed
            async with semaphore:
                chunk = CHUNKS[chunk_id]
                try:
                    result = await store_chunk(
                        chunk_id=chunk_id,
                        chunk_text_content=chunk["text"],
                        source_file=chunk["source_file"],
                    )
                    # Store encrypted chunk text only after Qdrant accepts the point to avoid orphan rows.
                    ingest_db.upsert_retrieval_chunk(
                        chunk_id=chunk_id,
                        job_id=chunk["job_id"],
                        chunk_index=chunk["index"],
                        source_file=chunk["source_file"],
                        text=chunk["text"],
                    )
                    chunk["status"] = "stored"
                    chunk["store_result"] = result
                    processed += 1
                except Exception as e:
                    logger.exception("[%s] Failed to persist chunk %s", job_id, chunk_id)
                    chunk["status"] = "failed"
                    chunk["error"] = str(e)
                    failed += 1
                JOBS[job_id]["processed_chunks"] = processed
                JOBS[job_id]["failed_chunks"] = failed
                JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
                # Sync every 10 chunks to reduce DB writes
                if processed % 10 == 0:
                    _sync_job_to_db(job_id)

        await asyncio.gather(*(run_chunk(cid) for cid in list(CHUNKS.keys()) if CHUNKS[cid]["job_id"] == job_id))
        JOBS[job_id]["status"] = "completed_with_errors" if failed > 0 else "completed"
        JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
        _sync_job_to_db(job_id)
        if failed == 0 and JOBS[job_id].get("replacement_for_job_id"):
            try:
                await promote_replacement(job_id)
            except Exception as e:
                logger.error(f"[{job_id}] Document replacement promotion failed: {e}", exc_info=True)
                JOBS[job_id]["promotion_error"] = str(e)
                JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
                _sync_job_to_db(job_id)

    except Exception as e:
        logger.error(f"[{job_id}] Document processing failed: {e}", exc_info=True)
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)
        JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
        _sync_job_to_db(job_id)


async def promote_replacement(job_id: str) -> None:
    """Promote a successful replacement job and retire its previous current job."""
    job = JOBS.get(job_id) or ingest_db.get_job(job_id)
    if not job:
        return

    old_job_id = job.get("replacement_for_job_id")
    if not old_job_id:
        return

    old_job = JOBS.get(old_job_id) or ingest_db.get_job(old_job_id)
    if not old_job:
        return

    try:
        database.transfer_document_access(old_job_id, job_id, changed_by="system:document-replacement")
    except Exception as e:
        logger.error(f"[{job_id}] Failed to transfer document access from {old_job_id}: {e}", exc_info=True)
        now = datetime.utcnow().isoformat()
        error_message = f"Failed to transfer document access: {e}"
        if old_job_id in JOBS:
            JOBS[old_job_id]["transfer_failed"] = True
            JOBS[old_job_id]["error"] = error_message
            JOBS[old_job_id]["updated_at"] = now
        if job_id in JOBS:
            JOBS[job_id]["transfer_failed"] = True
            JOBS[job_id]["error"] = error_message
            JOBS[job_id]["updated_at"] = now
            _sync_job_to_db(job_id)
        if old_job_id in JOBS:
            _sync_job_to_db(old_job_id)
        raise

    logger.info(f"[{job_id}] Promoting replacement for {old_job_id}")
    try:
        promoted = ingest_db.promote_replacement(job_id, old_job_id)
        if not promoted:
            raise RuntimeError("promote_replacement returned False")
    except Exception as e:
        logger.error(f"[{job_id}] Failed to promote replacement for {old_job_id}: {e}", exc_info=True)
        now = datetime.utcnow().isoformat()
        promotion_error = str(e)
        rollback_message = promotion_error
        try:
            database.transfer_document_access(job_id, old_job_id, changed_by="system:rollback")
        except Exception as rollback_error:
            logger.error(
                f"[{job_id}] Failed to roll back document access transfer to {old_job_id}: {rollback_error}",
                exc_info=True,
            )
            rollback_message = str(rollback_error)
        if job_id in JOBS:
            JOBS[job_id]["promotion_error"] = promotion_error
            JOBS[job_id]["error"] = rollback_message
            JOBS[job_id]["updated_at"] = now
            _sync_job_to_db(job_id)
        if old_job_id in JOBS:
            JOBS[old_job_id]["promotion_error"] = promotion_error
            JOBS[old_job_id]["error"] = rollback_message
            JOBS[old_job_id]["updated_at"] = now
            _sync_job_to_db(old_job_id)
        raise

    try:
        await delete_document_chunks(old_job_id)
    except Exception as e:
        logger.error(f"[{job_id}] Failed to delete replaced chunks for {old_job_id}: {e}")

    old_file_path = Path(old_job.get("file_path", ""))
    if old_file_path.exists():
        try:
            old_file_path.unlink()
        except Exception as e:
            logger.error(f"[{job_id}] Failed to delete replaced file {old_file_path}: {e}")

    if old_job_id in JOBS:
        JOBS[old_job_id]["is_current"] = False
        JOBS[old_job_id]["replaced_by_job_id"] = job_id
        JOBS[old_job_id]["updated_at"] = datetime.utcnow().isoformat()
        _sync_job_to_db(old_job_id)
    if job_id in JOBS:
        JOBS[job_id]["is_current"] = True
        JOBS[job_id]["updated_at"] = datetime.utcnow().isoformat()
        _sync_job_to_db(job_id)
    _audit_document_action(
        config_key=f"document:{job_id}:promote_replacement",
        action="promote_replacement",
        changed_by="system:document-replacement",
        old_value={
            "job_id": old_job_id,
            "filename": old_job.get("filename"),
            "is_current": True,
        },
        new_value={
            "job_id": job_id,
            "filename": job.get("filename"),
            "replacement_for_job_id": old_job_id,
            "is_current": True,
        },
    )


# PDF extraction mode: "fast" (PyMuPDF) or "quality" (Docling)
# Docling gives better structure but is VERY slow on CPU (~2-3 min for 100 pages)
# PyMuPDF is ~100x faster but loses some formatting
PDF_EXTRACT_MODE = os.getenv("PDF_EXTRACT_MODE", "fast")


def extract_pdf_text(file_path: Path) -> str:
    """
    Extract text from PDF.
    Mode controlled by PDF_EXTRACT_MODE env var:
      - "fast": PyMuPDF (~1 second for 100 pages)
      - "quality": Docling (~2-3 minutes for 100 pages on CPU)
    """
    logger.debug(f"Extracting PDF text from: {file_path} (mode={PDF_EXTRACT_MODE})")
    
    if PDF_EXTRACT_MODE == "fast":
        return _extract_pdf_pymupdf(file_path)
    else:
        return _extract_pdf_docling(file_path)


def _extract_pdf_pymupdf(file_path: Path) -> str:
    """Fast PDF extraction using PyMuPDF (~1 second for 100 pages)"""
    import fitz  # PyMuPDF
    
    logger.debug("Using PyMuPDF extraction (fast mode)...")
    doc = fitz.open(str(file_path))
    text_parts = []
    for page in doc:
        text_parts.append(page.get_text())
    doc.close()
    text = "\n\n".join(text_parts)
    logger.info(f"PyMuPDF extraction successful: {len(text)} chars")
    return text


def _extract_pdf_docling(file_path: Path) -> str:
    """Quality PDF extraction using Docling (slow on CPU, ~2-3 min for 100 pages)"""
    try:
        logger.debug("Attempting Docling extraction...")
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.datamodel.base_models import InputFormat
        
        # Use lightweight pipeline - no OCR, no table structure
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = False
        pipeline_options.do_table_structure = False
        logger.debug("Docling config: do_ocr=False, do_table_structure=False")
        
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
        
        logger.debug("Running Docling converter...")
        result = converter.convert(str(file_path))
        markdown = result.document.export_to_markdown()
        logger.info(f"Docling extraction successful: {len(markdown)} chars")
        return markdown
        
    except Exception as e:
        # Fallback to PyMuPDF
        logger.warning(f"Docling failed ({e}), falling back to PyMuPDF", exc_info=True)
        return _extract_pdf_pymupdf(file_path)


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.post("/wipe")
async def wipe_datastores(admin: dict = Depends(auth.require_admin)):
    """
    Wipe all entries in Qdrant collections.
    This is destructive and intended for local development resets.
    """
    logger.warning("Wipe requested: clearing Qdrant data")
    result = {
        "qdrant": {"status": "pending"},
    }

    # Qdrant: delete collections if they exist
    try:
        from store import get_qdrant_client, COLLECTION_NAME
        client = get_qdrant_client()
        collections = {c.name for c in client.get_collections().collections}
        deleted = []
        for name in (COLLECTION_NAME, "enclave_smoke_test"):
            if name in collections:
                client.delete_collection(name)
                deleted.append(name)
        result["qdrant"] = {"status": "ok", "deleted_collections": deleted}
        logger.info(f"Qdrant wipe complete: {deleted}")
    except Exception as e:
        result["qdrant"] = {"status": "error", "message": str(e)}
        logger.error(f"Qdrant wipe failed: {e}")

    return result


@router.get("/stats")
async def get_datastore_stats(admin: dict = Depends(auth.require_admin)):
    """
    Get quick stats for Qdrant.
    """
    stats = {
        "qdrant": {"status": "pending"},
    }

    # Qdrant: count points per collection
    try:
        from store import get_qdrant_client
        client = get_qdrant_client()
        collections = client.get_collections().collections
        collection_stats = {}
        for c in collections:
            info = client.get_collection(c.name)
            collection_stats[c.name] = {
                "points": info.points_count,
                "status": info.status,
                "vector_size": info.config.params.vectors.size if info.config and info.config.params else None,
            }
        stats["qdrant"] = {
            "status": "ok",
            "collections": collection_stats,
        }
    except Exception as e:
        stats["qdrant"] = {"status": "error", "message": str(e)}

    return stats


@router.get("/ontologies", response_model=OntologiesResponse)
async def list_ontologies() -> OntologiesResponse:
    """List valid ontology IDs for document extraction."""
    return OntologiesResponse(
        ontologies=sorted(VALID_ONTOLOGIES),
        default="general",
    )


@router.post("/upload", response_model=UploadResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    ontology_id: str = Form(default="general"),
    sample_percent: float = Form(default=100.0),
    admin: dict = Depends(auth.require_admin),
    _: None = Depends(upload_limiter),
):
    """
    Upload a document for processing.

    - Accepts PDF, TXT, MD files
    - Converts to text (PyMuPDF for PDFs)
    - Chunks and stores embeddings to Qdrant
    - Returns job_id to track progress
    """
    original_filename = file.filename or "document"
    try:
        canonical_name = canonical_name_for_upload(original_filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    suffix = Path(canonical_name).suffix.lower()

    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {suffix}. Allowed: {ALLOWED_UPLOAD_EXTENSIONS}"
        )

    validate_ingest_options(ontology_id, sample_percent)
    content = await file.read()
    changed_by = require_admin_pubkey(admin)
    try:
        return await queue_document_ingestion(
            original_filename=original_filename,
            canonical_name=canonical_name,
            content=content,
            ontology_id=ontology_id,
            sample_percent=sample_percent,
            changed_by=changed_by,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/upload/batch", response_model=BatchUploadResponse)
async def upload_document_batch(
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(default=[]),
    ontology_id: str = Form(default="general"),
    sample_percent: float = Form(default=100.0),
    admin: dict = Depends(auth.require_admin),
    _: None = Depends(upload_limiter),
):
    """
    Upload multiple documents for processing.

    Valid files are queued independently. Invalid files are reported in the
    rejected list so a batch can partially succeed.
    """
    validate_ingest_options(ontology_id, sample_percent)

    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Batch may contain at most {MAX_BATCH_FILES} files"
        )

    accepted: list[UploadResponse] = []
    rejected: list[BatchRejectedItem] = []
    seen_names: set[str] = set()
    total_bytes = 0
    staged: list[tuple[UploadFile, str, str]] = []

    for index, upload in enumerate(files):
        original_filename = upload.filename or "document"
        relative_path = relative_paths[index] if index < len(relative_paths) else None
        try:
            canonical_name = canonical_name_for_upload(original_filename, relative_path)
        except ValueError as e:
            rejected.append(BatchRejectedItem(filename=original_filename, reason=str(e)))
            continue

        if canonical_name in seen_names:
            rejected.append(BatchRejectedItem(
                filename=canonical_name,
                reason="Duplicate document name in this batch",
            ))
            continue
        seen_names.add(canonical_name)

        suffix = Path(canonical_name).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
            rejected.append(BatchRejectedItem(
                filename=canonical_name,
                reason=f"Unsupported file type: {suffix}. Allowed: {ALLOWED_UPLOAD_EXTENSIONS}",
            ))
            continue

        try:
            upload.file.seek(0, os.SEEK_END)
            size = upload.file.tell()
            upload.file.seek(0)
        except (AttributeError, OSError):
            content = await upload.read()
            size = len(content)
            await upload.seek(0)

        total_bytes += size
        if total_bytes > MAX_BATCH_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Batch upload exceeds {MAX_BATCH_BYTES} bytes"
            )
        staged.append((upload, original_filename, canonical_name))

    changed_by = require_admin_pubkey(admin)
    for upload, original_filename, canonical_name in staged:
        try:
            await upload.seek(0)
            content = await upload.read()
            accepted.append(await queue_document_ingestion(
                original_filename=original_filename,
                canonical_name=canonical_name,
                content=content,
                ontology_id=ontology_id,
                sample_percent=sample_percent,
                changed_by=changed_by,
            ))
        except ValueError as e:
            rejected.append(BatchRejectedItem(filename=canonical_name, reason=str(e)))

    return BatchUploadResponse(
        accepted=accepted,
        rejected=rejected,
    )


@router.get("/status/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str, admin: dict = Depends(auth.require_admin)):
    """Get the status of an ingest job"""
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    job = JOBS[job_id]

    return JobStatus(
        job_id=job["job_id"],
        filename=job["filename"],
        status=job["status"],
        ontology_id=job.get("ontology_id", "general"),
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        total_chunks=job["total_chunks"],
        processed_chunks=job.get("processed_chunks", 0),
        error=job.get("error"),
        canonical_name=job.get("canonical_name") or job.get("filename"),
        replacement_for_job_id=job.get("replacement_for_job_id"),
        replacement_for_filename=replacement_filename(job),
        replaced_by_job_id=job.get("replaced_by_job_id"),
        is_current=bool(job.get("is_current", True)),
    )


@router.get("/jobs")
async def list_jobs(user: dict = Depends(auth.require_admin_or_approved_user)) -> dict:
    """List ingest jobs available to the current user"""
    # Read directly from SQLite to ensure we get persisted data
    jobs_from_db = ingest_db.list_jobs(limit=500)

    # Admins see current jobs, in-flight replacements, and retired replacement history.
    if user.get("type") == "admin":
        filtered_jobs = [
            j for j in jobs_from_db
            if bool(j.get("is_current", 1))
            or j.get("replacement_for_job_id")
            or j.get("replaced_by_job_id")
        ]
    else:
        # Regular users only see available documents for their type
        user_type_id = user.get("user_type_id")
        if user_type_id is None:
            # User without assigned type sees no documents
            filtered_jobs = []
        else:
            available_job_ids = set(database.get_available_documents_for_user_type(user_type_id))
            filtered_jobs = [
                j for j in jobs_from_db
                if j["job_id"] in available_job_ids
                and bool(j.get("is_current", 1))
                and j.get("status") in ("completed", "completed_with_errors")
            ]

    return {
        "total": len(filtered_jobs),
        "jobs": [
            {
                "job_id": j["job_id"],
                "filename": j["filename"],
                "status": j["status"],
                "total_chunks": j["total_chunks"],
                "created_at": j["created_at"],
                "canonical_name": j.get("canonical_name") or j["filename"],
                "replacement_for_job_id": j.get("replacement_for_job_id"),
                "replacement_for_filename": replacement_filename(j),
                "replaced_by_job_id": j.get("replaced_by_job_id"),
                "is_current": bool(j.get("is_current", 1)),
            }
            for j in filtered_jobs
        ]
    }


@router.delete("/jobs/{job_id}")
async def delete_document(job_id: str, admin: dict = Depends(auth.require_admin)) -> dict:
    """
    Delete a document and all its associated data.
    Requires admin authentication.

    This removes:
    1. Chunks from Qdrant vector database
    2. Uploaded file from filesystem
    3. Job record from SQLite (CASCADE handles document_defaults tables)
    4. In-memory job/chunk entries

    Cannot delete documents that are currently processing.
    """
    changed_by = require_admin_pubkey(admin)
    # 1. Check if job exists
    job = ingest_db.get_job(job_id)
    if not job:
        # Also check in-memory (for jobs not yet synced)
        if job_id not in JOBS:
            response = _missing_document_deletion_response(job_id)
            _audit_data_deletion(
                config_key=f"document:{job_id}:delete",
                changed_by=changed_by,
                deletion=response["deletion"],
                workflow="delete_document",
                target_id=job_id,
            )
            return response
        job = JOBS[job_id]

    # 2. Block deletion if job is currently processing
    status = job.get("status", "")
    if status in ("pending", "processing"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete document while it is {status}. Wait for processing to complete."
        )

    response = await _delete_document_job_artifacts(job_id, job)
    _audit_document_action(
        config_key=f"document:{job_id}:delete",
        action="delete_document",
        changed_by=changed_by,
        old_value={
            "job_id": job_id,
            "filename": job.get("filename"),
            "status": status,
        },
        new_value={
            "job_id": job_id,
            "filename": job.get("filename"),
            "deletion_status": response["deletion"]["status"],
        },
    )
    _audit_data_deletion(
        config_key=f"document:{job_id}:delete",
        changed_by=changed_by,
        deletion=response["deletion"],
        workflow="delete_document",
        target_id=job_id,
    )
    return response


@router.post("/admin/documents/artifacts/cleanup")
async def cleanup_document_artifacts(admin: dict = Depends(auth.require_admin)) -> dict:
    """
    Delete lifecycle-eligible failed and superseded Document ingestion artifacts.

    Current completed Documents and in-flight replacements are intentionally retained.
    """
    changed_by = require_admin_pubkey(admin)
    page_size = 1000
    offset = 0
    jobs_to_check = []
    eligible_jobs = []
    results = []

    while True:
        jobs = ingest_db.list_jobs(limit=page_size, offset=offset)
        if not jobs:
            break
        jobs_to_check.extend(jobs)
        if len(jobs) < page_size:
            break
        offset += page_size

    for job in jobs_to_check:
        reason = _document_artifact_cleanup_reason(job)
        if not reason:
            continue
        job_id = job["job_id"]
        deletion_response = await _delete_document_job_artifacts(job_id, job)
        _audit_data_deletion(
            config_key=f"document:{job_id}:cleanup",
            changed_by=changed_by,
            deletion=deletion_response["deletion"],
            workflow=f"cleanup_document_artifacts:{reason}",
            target_id=job_id,
        )
        eligible_jobs.append({
            "job_id": job_id,
            "filename": job.get("filename", "unknown"),
            "reason": reason,
            "deletion": deletion_response["deletion"],
        })
        results.extend(deletion_response["deletion"]["results"])

    summary = summarize_deletion_results(results)
    return {
        "status": summary["status"],
        "cleaned_jobs": len(eligible_jobs),
        "eligible_jobs": eligible_jobs,
        "deletion": summary,
    }


@router.get("/pending", response_model=ChunkListResponse)
async def list_pending_chunks(job_id: Optional[str] = None, admin: dict = Depends(auth.require_admin)):
    """
    List chunks and their storage status.
    Optionally filter by job_id.
    """
    chunks = list(CHUNKS.values())

    if job_id:
        chunks = [c for c in chunks if c["job_id"] == job_id]

    pending = [c for c in chunks if c["status"] == "pending"]
    stored = [c for c in chunks if c["status"] == "stored"]

    return ChunkListResponse(
        total=len(chunks),
        pending=len(pending),
        extracted=0,  # No longer used
        stored=len(stored),
        chunks=[
            ChunkInfo(
                chunk_id=c["chunk_id"],
                job_id=c["job_id"],
                index=c["index"],
                text=c["text"],
                char_count=c["char_count"],
                status=c["status"],
                source_file=c["source_file"],
            )
            for c in chunks
        ]
    )


@router.get("/chunk/{chunk_id}")
async def get_chunk(chunk_id: str, admin: dict = Depends(auth.require_admin)):
    """
    Get a specific chunk details.
    """
    if chunk_id not in CHUNKS:
        persisted_chunk = ingest_db.get_retrieval_chunk(chunk_id)
        if not persisted_chunk:
            raise HTTPException(status_code=404, detail=f"Chunk not found: {chunk_id}")
        return {
            "chunk_id": persisted_chunk["chunk_id"],
            "job_id": persisted_chunk["job_id"],
            "index": persisted_chunk["chunk_index"],
            "source_file": persisted_chunk["source_file"],
            "status": "stored",
            "text": persisted_chunk["text"],
            "char_count": persisted_chunk["char_count"],
        }

    chunk = CHUNKS[chunk_id]

    return {
        "chunk_id": chunk["chunk_id"],
        "job_id": chunk["job_id"],
        "index": chunk["index"],
        "source_file": chunk["source_file"],
        "status": chunk["status"],
        "text": chunk["text"],
        "char_count": chunk["char_count"],
    }


@router.get("/pipeline-stats")
async def get_ingest_pipeline_stats(admin: dict = Depends(auth.require_admin)):
    """Get overall ingest pipeline statistics"""
    total_jobs = len(JOBS)
    total_chunks = len(CHUNKS)

    job_statuses = {}
    for j in JOBS.values():
        status = j["status"]
        job_statuses[status] = job_statuses.get(status, 0) + 1

    chunk_statuses = {}
    for c in CHUNKS.values():
        status = c["status"]
        chunk_statuses[status] = chunk_statuses.get(status, 0) + 1

    return {
        "jobs": {
            "total": total_jobs,
            "by_status": job_statuses,
        },
        "chunks": {
            "total": total_chunks,
            "by_status": chunk_statuses,
        },
    }


# =============================================================================
# DOCUMENT DEFAULTS ENDPOINTS (Admin)
# =============================================================================

@router.get("/admin/documents/defaults", response_model=DocumentDefaultsResponse)
async def get_document_defaults(admin: dict = Depends(auth.require_admin)):
    """
    List all documents with their availability/default status.
    Also includes completed jobs that don't have defaults set yet.
    Requires admin authentication.
    """
    # Get existing defaults
    defaults = database.list_document_defaults()
    defaults_by_job = {d["job_id"]: d for d in defaults}

    # Get all completed jobs
    completed_jobs = ingest_db.list_completed_jobs()

    documents = []
    for job in completed_jobs:
        job_id = job["job_id"]
        if job_id in defaults_by_job:
            d = defaults_by_job[job_id]
            documents.append(DocumentDefaultItem(
                job_id=job_id,
                filename=job["filename"],
                status=job["status"],
                total_chunks=job["total_chunks"],
                is_available=bool(d["is_available"]),
                is_default_active=bool(d["is_default_active"]),
                display_order=d["display_order"],
                updated_at=d.get("updated_at"),
            ))
        else:
            # Job exists but no defaults set - treat as available and active
            documents.append(DocumentDefaultItem(
                job_id=job_id,
                filename=job["filename"],
                status=job["status"],
                total_chunks=job["total_chunks"],
                is_available=True,
                is_default_active=True,
                display_order=0,
            ))

    return DocumentDefaultsResponse(documents=documents)


@router.put("/admin/documents/{job_id}/defaults", response_model=DocumentDefaultItem)
async def update_document_defaults(
    job_id: str,
    update: DocumentDefaultUpdate,
    admin: dict = Depends(auth.require_admin)
):
    """
    Update document availability and default state.
    Requires admin authentication.
    """
    # Verify job exists
    job = ingest_db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    # Get current defaults
    current = database.get_document_defaults(job_id)

    # Merge updates with current values
    is_available = update.is_available if update.is_available is not None else (
        bool(current["is_available"]) if current else True
    )
    is_default_active = update.is_default_active if update.is_default_active is not None else (
        bool(current["is_default_active"]) if current else True
    )
    display_order = update.display_order if update.display_order is not None else (
        current["display_order"] if current else 0
    )

    # Get admin pubkey for audit log
    admin_pubkey = admin.get("pubkey", "unknown")

    # Update/create defaults
    database.upsert_document_defaults(
        job_id=job_id,
        is_available=is_available,
        is_default_active=is_default_active,
        display_order=display_order,
        changed_by=admin_pubkey,
    )

    return DocumentDefaultItem(
        job_id=job_id,
        filename=job["filename"],
        status=job["status"],
        total_chunks=job["total_chunks"],
        is_available=is_available,
        is_default_active=is_default_active,
        display_order=display_order,
        updated_at=datetime.utcnow().isoformat(),
    )


@router.put("/admin/documents/defaults/batch", response_model=SuccessResponse)
async def batch_update_document_defaults(
    batch: DocumentDefaultsBatchUpdate,
    admin: dict = Depends(auth.require_admin)
):
    """
    Batch update multiple documents' defaults.
    Requires admin authentication.
    """
    admin_pubkey = admin.get("pubkey", "unknown")
    updated = 0
    skipped: list[str] = []

    for item in batch.updates:
        job_id = item.job_id
        if not job_id:
            skipped.append("(empty job_id)")
            continue

        # Verify job exists
        job = ingest_db.get_job(job_id)
        if not job:
            skipped.append(job_id)
            continue

        # Get current defaults for merge (like single update endpoint)
        current = database.get_document_defaults(job_id)

        # Merge updates with current values (same pattern as single update endpoint)
        is_available = item.is_available if item.is_available is not None else (
            bool(current["is_available"]) if current else True
        )
        is_default_active = item.is_default_active if item.is_default_active is not None else (
            bool(current["is_default_active"]) if current else True
        )
        display_order = item.display_order if item.display_order is not None else (
            current["display_order"] if current else 0
        )

        database.upsert_document_defaults(
            job_id=job_id,
            is_available=is_available,
            is_default_active=is_default_active,
            display_order=display_order,
            changed_by=admin_pubkey,
        )
        updated += 1

    message = f"Updated {updated} document defaults"
    if skipped:
        message += f", skipped {len(skipped)}: {', '.join(skipped[:5])}"
        if len(skipped) > 5:
            message += f" and {len(skipped) - 5} more"

    return SuccessResponse(
        success=True,
        message=message
    )


@router.get("/admin/documents/defaults/active")
async def get_default_active_documents(admin: dict = Depends(auth.require_admin)):
    """
    Get list of job_ids that are default active for new sessions.
    Requires admin authentication.
    """
    job_ids = database.get_default_active_documents()
    return {"job_ids": job_ids}


@router.get("/admin/documents/defaults/available")
async def get_available_documents(admin: dict = Depends(auth.require_admin)):
    """
    Get list of job_ids that are available for use.
    Requires admin authentication.
    """
    job_ids = database.get_available_documents()
    return {"job_ids": job_ids}


# =============================================================================
# DOCUMENT DEFAULTS USER-TYPE OVERRIDE ENDPOINTS (Admin)
# =============================================================================

@router.get("/admin/documents/defaults/user-type/{user_type_id}", response_model=DocumentDefaultsUserTypeResponse)
async def get_document_defaults_for_user_type(
    user_type_id: int,
    admin: dict = Depends(auth.require_admin)
):
    """
    Get document defaults with inheritance applied for a user type.
    Shows which values are overridden vs inherited from global defaults.
    Requires admin authentication.
    """
    # Verify user type exists
    user_type = database.get_user_type(user_type_id)
    if not user_type:
        raise HTTPException(status_code=404, detail=f"User type not found: {user_type_id}")

    # Get effective defaults with inheritance (only includes jobs WITH defaults)
    effective_defaults = database.get_effective_document_defaults(user_type_id)
    effective_by_job = {d["job_id"]: d for d in effective_defaults}

    # Get all completed jobs to include ones without defaults (same pattern as global endpoint)
    completed_jobs = ingest_db.list_completed_jobs()

    # Prefetch all overrides for this user type to avoid N+1 queries
    all_overrides = database.get_document_defaults_overrides_by_type(user_type_id)
    overrides_by_job = {o["job_id"]: o for o in all_overrides}

    documents = []
    for job in completed_jobs:
        job_id = job["job_id"]

        if job_id in effective_by_job:
            # Job has defaults (possibly with override)
            doc = effective_by_job[job_id]
            documents.append(DocumentDefaultWithInheritance(
                job_id=job_id,
                filename=job["filename"],
                status=job["status"],
                total_chunks=job["total_chunks"],
                is_available=bool(doc.get("is_available", True)),
                is_default_active=bool(doc.get("is_default_active", True)),
                display_order=doc.get("display_order", 0),
                updated_at=doc.get("updated_at"),
                is_override=doc.get("is_override", False),
                override_user_type_id=doc.get("override_user_type_id"),
                override_updated_at=doc.get("override_updated_at"),
            ))
        else:
            # Job exists but no defaults set - check for user-type override only
            override = overrides_by_job.get(job_id)
            if override:
                # Has override but no global default
                documents.append(DocumentDefaultWithInheritance(
                    job_id=job_id,
                    filename=job["filename"],
                    status=job["status"],
                    total_chunks=job["total_chunks"],
                    is_available=bool(override["is_available"]) if override["is_available"] is not None else True,
                    is_default_active=bool(override["is_default_active"]) if override["is_default_active"] is not None else True,
                    display_order=0,
                    updated_at=None,
                    is_override=True,
                    override_user_type_id=user_type_id,
                    override_updated_at=override.get("updated_at"),
                ))
            else:
                # No defaults and no override - use sensible defaults
                documents.append(DocumentDefaultWithInheritance(
                    job_id=job_id,
                    filename=job["filename"],
                    status=job["status"],
                    total_chunks=job["total_chunks"],
                    is_available=True,
                    is_default_active=True,
                    display_order=0,
                    updated_at=None,
                    is_override=False,
                    override_user_type_id=None,
                    override_updated_at=None,
                ))

    return DocumentDefaultsUserTypeResponse(
        user_type_id=user_type_id,
        user_type_name=user_type.get("name"),
        documents=documents
    )


@router.put("/admin/documents/{job_id}/defaults/user-type/{user_type_id}", response_model=DocumentDefaultWithInheritance)
async def set_document_defaults_override(
    job_id: str,
    user_type_id: int,
    update: DocumentDefaultOverrideUpdate,
    admin: dict = Depends(auth.require_admin)
):
    """
    Set a document defaults override for a user type.
    This value will override the global default for users of this type.
    Requires admin authentication.
    """
    # Verify user type exists
    user_type = database.get_user_type(user_type_id)
    if not user_type:
        raise HTTPException(status_code=404, detail=f"User type not found: {user_type_id}")

    # Verify job exists
    job = ingest_db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    # Get admin pubkey for audit log - fail if missing (auth integrity check)
    admin_pubkey = admin.get("pubkey")
    if not admin_pubkey:
        logger.error("Admin pubkey missing from authenticated context")
        raise HTTPException(status_code=500, detail="Authentication context incomplete")

    # Create/update the override
    database.upsert_document_defaults_override(
        job_id=job_id,
        user_type_id=user_type_id,
        is_available=update.is_available,
        is_default_active=update.is_default_active,
        changed_by=admin_pubkey,
    )
    # Get the effective state after update
    override = database.get_document_defaults_override(job_id, user_type_id)
    global_defaults = database.get_document_defaults(job_id)

    # Calculate effective values
    is_available = (
        bool(override["is_available"]) if override and override["is_available"] is not None
        else (bool(global_defaults["is_available"]) if global_defaults else True)
    )
    is_default_active = (
        bool(override["is_default_active"]) if override and override["is_default_active"] is not None
        else (bool(global_defaults["is_default_active"]) if global_defaults else True)
    )

    return DocumentDefaultWithInheritance(
        job_id=job_id,
        filename=job["filename"],
        status=job["status"],
        total_chunks=job["total_chunks"],
        is_available=is_available,
        is_default_active=is_default_active,
        display_order=global_defaults["display_order"] if global_defaults else 0,
        is_override=True,
        override_user_type_id=user_type_id,
        override_updated_at=override["updated_at"] if override else None,
    )


@router.delete("/admin/documents/{job_id}/defaults/user-type/{user_type_id}")
async def delete_document_defaults_override(
    job_id: str,
    user_type_id: int,
    admin: dict = Depends(auth.require_admin)
):
    """
    Remove a document defaults override for a user type (revert to global default).
    Requires admin authentication.
    """
    # Verify user type exists
    user_type = database.get_user_type(user_type_id)
    if not user_type:
        raise HTTPException(status_code=404, detail=f"User type not found: {user_type_id}")

    # Verify job exists
    job = ingest_db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    # Get admin pubkey for audit log - fail if missing (auth integrity check)
    admin_pubkey = admin.get("pubkey")
    if not admin_pubkey:
        logger.error("Admin pubkey missing from authenticated context")
        raise HTTPException(status_code=500, detail="Authentication context incomplete")

    # Delete the override
    deleted = database.delete_document_defaults_override(job_id, user_type_id, changed_by=admin_pubkey)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"No override found for job '{job_id}' and user type {user_type_id}"
        )

    return {"success": True, "message": f"Override for document '{job_id}' reverted to global default"}


@router.get("/admin/documents/defaults/user-type/{user_type_id}/active")
async def get_active_documents_for_user_type(
    user_type_id: int,
    admin: dict = Depends(auth.require_admin)
):
    """
    Get list of job_ids that are default active for a user type.
    Uses inheritance: user-type overrides take precedence over global defaults.
    Requires admin authentication.
    """
    # Verify user type exists
    user_type = database.get_user_type(user_type_id)
    if not user_type:
        raise HTTPException(status_code=404, detail=f"User type not found: {user_type_id}")

    job_ids = database.get_active_documents_for_user_type(user_type_id)
    return {"job_ids": job_ids}
