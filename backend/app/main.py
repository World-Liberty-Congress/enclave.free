from __future__ import annotations

"""
Enclave Backend - FastAPI Application
RAG system with Qdrant vector search.
Also provides user/admin management via SQLite.
"""

import asyncio
import json
import os
import uuid
import logging
import time
import re
import math
import tempfile
import sqlite3
import secrets
import html
import ipaddress
from urllib.parse import urlparse
from user_lookup import router as user_lookup_router
from audit_insights import router as audit_insights_router
from usage_insights import router as usage_insights_router
from conversation_lookup import router as conversation_lookup_router
from fastapi import FastAPI, HTTPException, Query, Depends, Request, BackgroundTasks, Response, Header, Cookie
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import QdrantClient
from pydantic import BaseModel
from typing import Optional, List, Dict

from llm import get_sage_provider
from inference_repair import current_inference_repair_status
from store import embed_texts
from seed_status import read_seed_status
import database
from data_deletion import (
    deletion_target_failed,
    deletion_target_skipped,
    deletion_target_succeeded,
    summarize_deletion_results,
)
from sql_safety import ALLOWED_TABLES, validate_sql_allowed_tables
from models import (
    AdminAuth, AdminResponse, AdminListResponse,
    AdminAuthRequest, AdminAuthResponse,
    InstanceSettings, InstanceSettingsResponse, InstanceStatusResponse,
    # User Type models
    UserTypeCreate, UserTypeUpdate, UserTypeResponse, UserTypeListResponse,
    # Field Definition models
    FieldDefinitionCreate, FieldDefinitionUpdate, FieldDefinitionResponse, FieldDefinitionListResponse,
    FieldEncryptionRequest, FieldEncryptionResponse,
    UserCreate, UserUpdate, UserResponse, UserListResponse,
    UserRosterExportAuditRequest,
    UserTypeMigrationRequest, UserTypeMigrationResponse,
    UserTypeMigrationBatchRequest, UserTypeMigrationBatchResponse, UserTypeMigrationBatchResult,
    SuccessResponse,
    # Database Explorer models
    ColumnInfo, TableInfo, TablesListResponse,
    TableDataResponse, DBQueryRequest, DBQueryResponse,
    RowMutationRequest, RowMutationResponse,
    # Magic Link Auth models
    MagicLinkRequest, MagicLinkResponse, VerifyTokenRequest,
    VerifyTokenResponse, AuthUserResponse, SessionUserResponse,
    OnboardingStatusResponse,
    # Session defaults
    SessionDefaultsResponse,
    # Test email
    TestEmailRequest, TestEmailResponse,
    # Reachout
    ReachoutRequest, ReachoutResponse,
    # Resource Referral Directory
    ResourceCreate, ResourceUpdate, ResourceResponse, ResourceListResponse,
    # Session Logs (Test & Feedback)
    SessionLogCreate, SessionLogSaveTranscript, SessionLogMetadata,
    SessionLogListResponse, SessionLogDetail, SessionMessageFeedbackRequest,
    SessionLogMessageFeedback, TestUserProvisionResponse,
)
import session_logs
import impersonation
import region_data
from nostr import verify_auth_event, get_pubkey_from_event
import auth
import lifecycle
from rate_limit import RateLimiter
from rate_limit import rate_limit_backend_status
from rate_limit_key import rate_limit_key as _stable_rate_limit_key

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("enclave.main")

# Import routers
from ingest import load_jobs_and_resume, router as ingest_router
from ai_config import router as ai_config_router
from deployment_config import internal_router as deployment_config_internal_router
from deployment_config import router as deployment_config_router
import deployment_config
from key_migration import router as key_migration_router
from internal_agent import router as internal_agent_router

logger.info("Starting Enclave API...")


def _audit_data_deletion_event(
    *,
    config_key: str,
    changed_by: str,
    workflow: str,
    target_id: str,
    deletion: dict,
) -> None:
    _best_effort_config_audit_event(
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


def _best_effort_config_audit_event(**kwargs) -> None:
    try:
        database.log_config_audit_event(**kwargs)
    except Exception as exc:
        logger.error(
            "Failed to write config audit event table=%s config_key=%s: %s",
            kwargs.get("table_name"),
            kwargs.get("config_key"),
            exc,
            exc_info=True,
        )


app = FastAPI(
    title="Enclave API",
    description="Privacy-first RAG system for curated knowledge",
    version="0.2.0"
)

def _normalize_origin(origin: str) -> str:
    """
    Normalize an origin value for CORS matching.
    Keeps only scheme + host[:port], strips paths/trailing slashes, and rejects wildcard.
    """
    raw = origin.strip()
    if not raw or raw == "*":
        return ""

    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    logger.warning(
        "Ignoring schemeless/invalid origin %r; expected format 'scheme://host[:port]'",
        raw,
    )
    return ""


def _get_cors_allow_origins() -> list[str]:
    """
    Resolve explicit CORS allowlist.
    Wildcard origins are intentionally rejected for authenticated deployments.
    """
    configured = os.getenv("CORS_ALLOW_ORIGINS") or os.getenv("CORS_ORIGINS", "")
    raw_origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if "*" in raw_origins:
        logger.warning("Ignoring '*' in CORS allowlist; explicit origins are required for credentialed auth")

    origins: list[str] = []
    for raw_origin in raw_origins:
        normalized = _normalize_origin(raw_origin)
        if normalized and normalized not in origins:
            origins.append(normalized)

    frontend_origin = _normalize_origin(os.getenv("FRONTEND_URL", ""))
    if frontend_origin and frontend_origin not in origins:
        origins.append(frontend_origin)

    if not origins:
        origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]

    return origins


def _csrf_allowed_origins() -> set[str]:
    """Origins trusted for cookie-authenticated state-changing requests."""
    return set(_get_cors_allow_origins())


def _request_origin(request: Request) -> str | None:
    """Resolve origin from Origin header, with Referer fallback."""
    origin = _normalize_origin(request.headers.get("origin", ""))
    if origin:
        return origin

    referer = request.headers.get("referer")
    if referer:
        return _normalize_origin(referer)

    return None


def _is_secure_request(request: Request) -> bool:
    """Detect HTTPS requests (direct or behind trusted proxy)."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    if forwarded_proto == "https":
        return True
    return request.url.scheme == "https"


def _api_content_security_policy() -> str:
    """
    CSP for API responses.
    Keep strict defaults while allowing explicit override for deployments.
    """
    configured = os.getenv("SECURITY_CSP", "").strip()
    if configured:
        return configured
    return "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


def _apply_security_headers(request: Request, response: Response) -> None:
    """Attach baseline security headers to backend responses."""
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")

    # Keep docs usable by not forcing strict CSP on docs assets.
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers.setdefault("Content-Security-Policy", _api_content_security_policy())

    if _is_secure_request(request):
        max_age = os.getenv("HSTS_MAX_AGE", "31536000").strip() or "31536000"
        response.headers.setdefault("Strict-Transport-Security", f"max-age={max_age}; includeSubDomains")


def _has_cookie_session(request: Request) -> bool:
    """Whether request carries Enclave auth cookies."""
    return bool(
        request.cookies.get(auth.USER_SESSION_COOKIE_NAME)
        or request.cookies.get(auth.ADMIN_SESSION_COOKIE_NAME)
    )


def _has_bearer_auth(request: Request) -> bool:
    """Whether request carries Authorization bearer token."""
    header = request.headers.get("authorization", "")
    return header.startswith("Bearer ")


def _should_enforce_csrf(request: Request) -> bool:
    """CSRF applies to unsafe methods when cookie auth is used."""
    if request.method.upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return False

    # Token/header-based clients are not subject to cookie-CSRF checks.
    if _has_bearer_auth(request):
        return False

    return _has_cookie_session(request)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """
    Unified security middleware:
    - Enforces CSRF for cookie-authenticated unsafe requests
    - Applies standard security headers
    """
    if _should_enforce_csrf(request):
        origin = _request_origin(request)
        if not origin or origin not in _csrf_allowed_origins():
            return JSONResponse(status_code=403, content={"detail": "Invalid request origin"})

        csrf_cookie = request.cookies.get(auth.CSRF_COOKIE_NAME)
        csrf_header = request.headers.get("X-CSRF-Token")
        if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
            return JSONResponse(status_code=403, content={"detail": "CSRF validation failed"})

    response = await call_next(request)
    _apply_security_headers(request, response)
    return response

# CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(ingest_router)
app.include_router(ai_config_router)
app.include_router(deployment_config_router)
app.include_router(deployment_config_internal_router)
app.include_router(key_migration_router)
app.include_router(internal_agent_router)
app.include_router(user_lookup_router)
app.include_router(audit_insights_router)
app.include_router(usage_insights_router)
app.include_router(conversation_lookup_router)
app.include_router(lifecycle.router)


@app.on_event("startup")
async def startup_event():
    """Run startup checks"""
    # Verify SMTP configuration (connection test, no email sent)
    smtp_status = auth.verify_smtp_config()
    if smtp_status["configured"] and not smtp_status["mock_mode"] and not smtp_status["connection_ok"]:
        logger.warning("SMTP is configured but connection test failed - email sending may not work")
    await load_jobs_and_resume()
    deployment_config.run_startup_inference_verification()


@app.on_event("shutdown")
async def shutdown_event():
    """Close shared outbound clients."""
    await lifecycle.close_sage_client()

# Rate limiters for auth endpoints
magic_link_limiter = RateLimiter(limit=5, window_seconds=60)   # 5 per minute
admin_auth_limiter = RateLimiter(limit=10, window_seconds=60)  # 10 per minute


def _rate_limit_key(request: Request) -> str:
    """
    Stable key for API rate limiting.
    Prefer auth identity (bearer/cookie), fallback to client IP.
    """
    return _stable_rate_limit_key(request)


def _safe_int_env(key: str, default: int) -> int:
    """Read an env var as int, falling back to *default* on bad values."""
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("Invalid integer for env %s=%r, using default %d", key, raw, default)
        return default


chat_limiter = RateLimiter(
    limit=_safe_int_env("RATE_LIMIT_CHAT_PER_MINUTE", 120),
    window_seconds=60,
    key_func=_rate_limit_key,
)

vector_search_limiter = RateLimiter(
    limit=_safe_int_env("RATE_LIMIT_VECTOR_SEARCH_PER_MINUTE", 30),
    window_seconds=60,
    key_func=_rate_limit_key,
)

def _safe_int_setting(key: str, default: int) -> int:
    """Read an instance setting as int, falling back to default."""
    raw = database.get_setting(key)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return default


def _send_html_email_smtp(
    smtp: dict,
    to_email: str,
    subject: str,
    html_body: str,
    reply_to: str | None = None,
) -> None:
    """
    Send a single HTML email using the configured SMTP dict.

    This is intentionally synchronous (smtplib is blocking); call it via asyncio.to_thread
    from async endpoints to avoid blocking the event loop.
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    msg_obj = MIMEMultipart("alternative")
    msg_obj["Subject"] = subject
    msg_obj["From"] = smtp["from_address"]
    msg_obj["To"] = to_email
    if reply_to:
        msg_obj["Reply-To"] = reply_to
    msg_obj.attach(MIMEText(html_body, "html"))

    if smtp["port"] == 465:
        with smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=smtp["timeout"]) as server:
            server.login(smtp["user"], smtp["password"])
            server.sendmail(smtp["from_address"], [to_email], msg_obj.as_string())
        return

    with smtplib.SMTP(smtp["host"], smtp["port"], timeout=smtp["timeout"]) as server:
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls()
            server.ehlo()
        server.login(smtp["user"], smtp["password"])
        server.sendmail(smtp["from_address"], [to_email], msg_obj.as_string())


def _reachout_limit_per_hour() -> int:
    return _safe_int_setting("reachout_rate_limit_per_hour", 3)


def _reachout_limit_per_day() -> int:
    return _safe_int_setting("reachout_rate_limit_per_day", 10)


reachout_hour_limiter = RateLimiter(
    limit=_reachout_limit_per_hour,
    window_seconds=3600,
    key_func=_rate_limit_key,
)

reachout_day_limiter = RateLimiter(
    limit=_reachout_limit_per_day,
    window_seconds=86400,
    key_func=_rate_limit_key,
)

# Configuration from environment
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))

# Collection name for smoke test
COLLECTION_NAME = "enclave_smoke_test"


class SmokeTestResult(BaseModel):
    """Response model for smoke test endpoint"""
    qdrant: dict
    message: str
    success: bool


class HealthResponse(BaseModel):
    """Response model for health endpoint"""
    status: str
    services: dict


class LLMTestResult(BaseModel):
    """Response model for LLM smoke test endpoint"""
    success: bool
    provider: str
    health: bool
    response: Optional[str] = None
    model: Optional[str] = None
    error: Optional[str] = None


class VectorSearchRequest(BaseModel):
    """Request model for vector search endpoint"""
    query: str
    top_k: int = 5
    collection: str = "enclave_smoke_test"


class VectorSearchResultItem(BaseModel):
    """Single vector search result"""
    id: str
    score: float
    payload: dict


class VectorSearchResponse(BaseModel):
    """Response model for vector search endpoint"""
    results: List[VectorSearchResultItem]
    query_embedding_dim: int
    collection: str


def get_qdrant_client():
    """Create Qdrant client connection"""
    return QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "Enclave API",
        "version": "0.2.0",
        "status": "running"
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Check health of all services"""
    services = {
        "qdrant": "unknown",
        "shared_rate_limit_store": "unknown",
    }

    # Check Qdrant
    try:
        client = get_qdrant_client()
        client.get_collections()
        services["qdrant"] = "healthy"
    except Exception as e:
        services["qdrant"] = f"unhealthy: {str(e)}"

    try:
        rate_limit_status = await rate_limit_backend_status()
        if rate_limit_status["status"] == "healthy":
            services["shared_rate_limit_store"] = "healthy"
        elif rate_limit_status["status"] == "local_only":
            services["shared_rate_limit_store"] = "unknown"
        else:
            services["shared_rate_limit_store"] = f"unhealthy: {rate_limit_status['summary']}"
    except Exception:
        logger.exception("Shared rate limit store health probe failed")
        services["shared_rate_limit_store"] = "unhealthy: error checking shared rate limit store"

    all_healthy = all(s in {"healthy", "unknown"} for s in services.values())

    return HealthResponse(
        status="healthy" if all_healthy else "degraded",
        services=services
    )


@app.get("/test", response_model=SmokeTestResult)
async def smoke_test():
    """
    Smoke test endpoint that verifies Qdrant contains seeded data.
    """
    qdrant_result = {"status": "error", "vector_id": None, "payload": None}

    # Test Qdrant - retrieve the seeded embedding
    try:
        client = get_qdrant_client()

        # Check if collection exists
        collections = client.get_collections().collections
        collection_exists = any(c.name == COLLECTION_NAME for c in collections)

        if collection_exists:
            # Retrieve the seeded point using UUID derived from claim ID
            claim_id = "claim_knowledge_sharing"
            point_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, claim_id))
            points = client.retrieve(
                collection_name=COLLECTION_NAME,
                ids=[point_uuid],
                with_vectors=True
            )

            if points:
                point = points[0]
                qdrant_result = {
                    "status": "ok",
                    "vector_id": point.id,
                    "payload": point.payload,
                    "vector_dimension": len(point.vector) if point.vector else 0
                }
            else:
                qdrant_result = {
                    "status": "error",
                    "message": "Seeded embedding not found"
                }
        else:
            seed_status = read_seed_status()
            if seed_status and seed_status.get("status") == "degraded":
                qdrant_result = {
                    "status": "degraded",
                    "reason": seed_status.get("reason"),
                    "message": seed_status.get(
                        "message",
                        f"Collection '{COLLECTION_NAME}' does not exist. Run seed script."
                    ),
                }
            else:
                qdrant_result = {
                    "status": "error",
                    "message": f"Collection '{COLLECTION_NAME}' does not exist. Run seed script."
                }
    except Exception as e:
        qdrant_result = {
            "status": "error",
            "message": f"Qdrant error: {str(e)}"
        }

    # Determine overall success
    success = qdrant_result.get("status") == "ok"
    message = "Smoke test passed!" if success else "Smoke test failed. Check Qdrant status."

    return SmokeTestResult(
        qdrant=qdrant_result,
        message=message,
        success=success
    )


@app.get("/llm/test", response_model=LLMTestResult)
async def llm_smoke_test():
    """
    Smoke test Sage/Tinfoil Model Provider connectivity.

    Tests the OpenAI-compatible service endpoint:
    - Checks Sage/Tinfoil Model Provider health endpoint
    - Sends a simple test prompt
    - Returns the response
    """
    provider_name = "sage"

    try:
        provider = get_sage_provider()

        # Check health first
        health = provider.health_check()
        if not health:
            return LLMTestResult(
                success=False,
                provider=provider.name,
                health=False,
                error=f"Sage/Tinfoil health check failed (provider='{provider.name}')"
            )

        # Send a simple test prompt
        result = provider.complete("Say 'hello' and nothing else.")

        return LLMTestResult(
            success=True,
            provider=provider.name,
            health=True,
            response=result.content,
            model=result.model
        )

    except Exception as e:
        return LLMTestResult(
            success=False,
            provider=provider_name,
            health=False,
            error=str(e)
        )


@app.post("/vector-search", response_model=VectorSearchResponse)
async def vector_search(
    request: VectorSearchRequest,
    admin: dict = Depends(auth.require_admin),
    _: None = Depends(vector_search_limiter),
):
    """
    Direct vector search endpoint (no LLM generation).

    Useful for debugging:
    1. Embeds the query text
    2. Searches Qdrant for similar vectors
    3. Returns results with similarity scores

    This lets you test embedding + search separately from LLM generation.
    """
    try:
        # 1. Embed the query
        query_embedding = embed_texts([f"query: {request.query}"])[0]

        # 2. Search Qdrant
        qdrant = get_qdrant_client()
        search_result = qdrant.query_points(
            collection_name=request.collection,
            query=query_embedding,
            limit=request.top_k,
            with_payload=True
        )
        results = search_result.points

        # 3. Format results
        search_results = [
            VectorSearchResultItem(
                id=str(r.id),
                score=r.score,
                payload=r.payload or {}
            )
            for r in results
        ]

        return VectorSearchResponse(
            results=search_results,
            query_embedding_dim=len(query_embedding),
            collection=request.collection
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Magic Link Authentication Endpoints
# =============================================================================

@app.post("/auth/magic-link", response_model=MagicLinkResponse)
async def send_magic_link(
    request: Request,
    body: MagicLinkRequest,
    _: None = Depends(magic_link_limiter),
    __: None = Depends(auth.require_instance_setup_complete)
):
    """
    Send a magic link to the user's email for authentication.
    Creates a signed, time-limited token and sends it via email.
    Rate limited to 5 requests per minute per IP.
    
    Requires instance setup to be complete (admin must authenticate first).
    """
    email = body.email.strip().lower()
    name = body.name.strip()

    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    # Require admin to be configured before onboarding
    if not database.list_admins():
        raise HTTPException(
            status_code=503,
            detail="Instance not configured. An admin must be registered before users can sign up."
        )

    # Generate token
    token = auth.create_magic_link_token(email, name)

    # Send email (or log in mock mode). Keep the two-argument call for requests
    # without a locale so existing delivery adapters remain compatible; an
    # explicit value is passed through and normalized by the renderer.
    locale = auth.normalize_magic_link_locale(body.locale)
    if body.locale is None:
        success = await asyncio.to_thread(auth.send_magic_link_email, email, token)
    else:
        success = await asyncio.to_thread(auth.send_magic_link_email, email, token, locale)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to send magic link email")

    return MagicLinkResponse(
        success=True,
        message="If this address can sign in, we'll send a magic link."
    )


def _compute_onboarding_flags(user: dict) -> tuple[bool, bool]:
    """Return (needs_onboarding, needs_user_type) for the given user."""
    status = _build_onboarding_status(user)
    return status["needs_onboarding"], status["needs_user_type"]


def _resolve_effective_field_definitions(user_type_id: int | None) -> tuple[int | None, bool, list[dict]]:
    """
    Resolve effective field definitions for onboarding completeness checks.

    Returns:
        (effective_user_type_id, needs_user_type, effective_fields)
    """
    user_types = database.list_user_types()
    needs_user_type = len(user_types) > 1 and not user_type_id
    if needs_user_type:
        return None, True, []

    # If only one type exists, treat it as effective even if not persisted on user yet.
    effective_type_id = user_type_id
    if effective_type_id is None and len(user_types) == 1:
        effective_type_id = user_types[0]["id"]

    if effective_type_id is None:
        raw_fields = [
            field for field in database.get_field_definitions()
            if field.get("user_type_id") is None
        ]
    else:
        raw_fields = database.get_field_definitions(
            user_type_id=effective_type_id,
            include_global=True
        )

    # Collapse global + type-specific duplicates by field_name with type-specific precedence.
    effective_by_name: dict[str, dict] = {}
    for field in raw_fields:
        field_name = field.get("field_name")
        if not field_name:
            continue
        existing = effective_by_name.get(field_name)
        if existing is None:
            effective_by_name[field_name] = field
            continue

        # Prefer type-specific over global when both exist for the same field_name.
        existing_type_id = existing.get("user_type_id")
        incoming_type_id = field.get("user_type_id")
        if existing_type_id is None and incoming_type_id is not None:
            effective_by_name[field_name] = field

    effective_fields = sorted(
        effective_by_name.values(),
        key=lambda item: (item.get("display_order", 0), item.get("id", 0))
    )
    return effective_type_id, False, effective_fields


def _is_field_completed_for_user(field_def: dict, user: dict) -> bool:
    """Determine whether the user has a non-empty answer for a field definition."""
    field_name = field_def.get("field_name")
    if not field_name:
        return False

    fields = user.get("fields") or {}
    fields_encrypted = user.get("fields_encrypted") or {}

    # Encrypted presence indicates a stored answer.
    if field_name in fields_encrypted:
        return True

    # Missing key means unanswered.
    if field_name not in fields:
        return False

    value = fields.get(field_name)

    # Required checkbox semantics: explicit false is still a valid answer.
    if field_def.get("field_type") == "checkbox":
        return value is not None

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _build_onboarding_status(user: dict | None) -> dict:
    """Build canonical onboarding completeness status for a user."""
    if not user:
        return {
            "user_id": -1,
            "user_type_id": None,
            "effective_user_type_id": None,
            "needs_user_type": False,
            "needs_onboarding": False,
            "total_fields": 0,
            "required_fields": 0,
            "completed_required_fields": 0,
            "missing_required_fields": [],
            "missing_optional_fields": [],
        }

    user_type_id = user.get("user_type_id")
    effective_type_id, needs_user_type, effective_fields = _resolve_effective_field_definitions(user_type_id)

    if needs_user_type:
        return {
            "user_id": int(user.get("id", -1)),
            "user_type_id": user_type_id,
            "effective_user_type_id": None,
            "needs_user_type": True,
            "needs_onboarding": True,
            "total_fields": 0,
            "required_fields": 0,
            "completed_required_fields": 0,
            "missing_required_fields": [],
            "missing_optional_fields": [],
        }

    missing_required: list[dict] = []
    missing_optional: list[dict] = []
    completed_required = 0
    any_completed = False

    for field in effective_fields:
        completed = _is_field_completed_for_user(field, user)
        any_completed = any_completed or completed
        if field.get("required"):
            if completed:
                completed_required += 1
            else:
                missing_required.append(field)
        elif not completed:
            missing_optional.append(field)

    has_fields = len(effective_fields) > 0
    # Preserve existing onboarding behavior for optional-only schemas:
    # if fields exist and user has never answered anything, onboarding is needed.
    needs_onboarding = bool(missing_required) or (has_fields and not any_completed)

    return {
        "user_id": int(user.get("id", -1)),
        "user_type_id": user_type_id,
        "effective_user_type_id": effective_type_id,
        "needs_user_type": False,
        "needs_onboarding": needs_onboarding,
        "total_fields": len(effective_fields),
        "required_fields": sum(1 for field in effective_fields if field.get("required")),
        "completed_required_fields": completed_required,
        "missing_required_fields": missing_required,
        "missing_optional_fields": missing_optional,
    }


def _missing_required_field_names_for_type(user: dict, target_user_type_id: int) -> list[str]:
    """Compute missing required field names if the user were assigned target_user_type_id."""
    simulated_user = dict(user)
    simulated_user["user_type_id"] = target_user_type_id
    simulated_status = _build_onboarding_status(simulated_user)

    names: list[str] = []
    for field in simulated_status.get("missing_required_fields", []):
        field_name = field.get("field_name")
        if field_name:
            names.append(field_name)
    return names


async def _verify_magic_link_and_create_session(token: str, response: Response) -> VerifyTokenResponse:
    """
    Verify a magic link token, create/return the user, and set session cookie.
    """
    # Verify token
    data = auth.verify_magic_link_token(token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired magic link")

    email = data["email"]
    name = data.get("name", "")

    # Get or create user
    user = database.get_user_by_email(email)
    if not user:
        # Create new user
        user_id = database.create_user(email=email, name=name)
        user = database.get_user(user_id)

    # Create session token
    session_token = auth.create_session_token(user["id"], email)
    auth.set_user_session_cookie(response, session_token)

    needs_onboarding, needs_user_type = _compute_onboarding_flags(user)

    return VerifyTokenResponse(
        success=True,
        user=AuthUserResponse(
            id=user["id"],
            email=email,
            name=name or user.get("name"),
            user_type_id=user.get("user_type_id"),
            approved=bool(user.get("approved", 1)),
            created_at=user.get("created_at"),
            needs_onboarding=needs_onboarding,
            needs_user_type=needs_user_type
        ),
        session_token=session_token
    )


@app.post("/auth/verify", response_model=VerifyTokenResponse)
async def verify_magic_link(
    body: VerifyTokenRequest,
    response: Response,
    _: None = Depends(auth.require_instance_setup_complete),
):
    """
    Verify a magic link token and create/return the user.
    Active clients should use this POST endpoint (token in JSON body).
    """
    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")
    return await _verify_magic_link_and_create_session(token, response)


@app.get("/auth/me", response_model=SessionUserResponse)
async def get_current_user(
    authorization: Optional[str] = Header(None),
    session_cookie: Optional[str] = Cookie(None, alias=auth.USER_SESSION_COOKIE_NAME),
    _: None = Depends(auth.require_instance_setup_complete)
):
    """
    Get the current authenticated user from session token.
    Returns authenticated: false if no valid session.
    
    Requires instance setup to be complete (admin must authenticate first).
    """
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token:
        token = session_cookie

    if not token:
        return SessionUserResponse(authenticated=False)

    # Verify session
    data = auth.verify_session_token(token)
    if not data:
        return SessionUserResponse(authenticated=False)

    # Get user
    user = database.get_user(data["user_id"])
    if not user:
        return SessionUserResponse(authenticated=False)

    needs_onboarding, needs_user_type = _compute_onboarding_flags(user)

    return SessionUserResponse(
        authenticated=True,
        user=AuthUserResponse(
            id=user["id"],
            email=user.get("email") or data["email"],
            name=user.get("name"),
            user_type_id=user.get("user_type_id"),
            approved=bool(user.get("approved", 1)),
            created_at=user.get("created_at"),
            needs_onboarding=needs_onboarding,
            needs_user_type=needs_user_type
        )
    )


@app.post("/auth/logout", response_model=SuccessResponse)
async def logout_user(response: Response):
    """Clear auth session cookies for the current browser."""
    auth.clear_auth_cookies(response)
    return SuccessResponse(success=True, message="Logged out")


@app.post("/reachout", response_model=ReachoutResponse)
async def submit_reachout(
    body: ReachoutRequest,
    request: Request,
    user: dict = Depends(auth.get_current_user),
    _: None = Depends(reachout_hour_limiter),
    __: None = Depends(reachout_day_limiter),
):
    """
    Authenticated-only user reachout submission.

    Sends an email to the admin-configured destination using existing SMTP settings.
    Rate-limited per session (cookie/bearer digest), with IP fallback.
    """
    enabled = (database.get_setting("reachout_enabled") or "").strip().lower() == "true"
    if not enabled:
        # Hide feature existence when disabled.
        raise HTTPException(status_code=404, detail="Not found")

    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Message is required")
    if len(msg) > 5000:
        raise HTTPException(status_code=400, detail="Message is too long")

    to_email = (database.get_setting("reachout_to_email") or "").strip()
    if not to_email:
        raise HTTPException(status_code=503, detail="Reachout is not configured")

    mode = (database.get_setting("reachout_mode") or "support").strip().lower()
    if mode not in {"feedback", "help", "support"}:
        mode = "support"

    instance_name = (database.get_setting("instance_name") or "Enclave").strip() or "Enclave"
    subject_prefix = (database.get_setting("reachout_subject_prefix") or "").strip()

    subject = f"[{instance_name}] {mode.title()}: user reachout"
    if subject_prefix:
        subject = f"{subject_prefix} {subject}"

    # Best-effort reply-to: extract email from session token payload.
    reply_to = None
    try:
        token = auth._resolve_auth_token(
            request.headers.get("authorization"),
            request.cookies.get(auth.USER_SESSION_COOKIE_NAME),
        )
        token_data = auth.verify_session_token(token) if token else None
        email = (token_data or {}).get("email")
        if isinstance(email, str) and "@" in email and len(email) <= 254:
            reply_to = email.strip()
    except Exception:
        reply_to = None

    smtp = auth._get_smtp_config()
    if not smtp.get("host") and not smtp.get("mock_mode"):
        raise HTTPException(status_code=503, detail="Email service is not configured")

    # Build email (HTML, with plaintext-ish formatting via pre-wrap)
    safe_msg = html.escape(msg)
    ua = request.headers.get("user-agent", "")
    user_id = user.get("id")
    client_ip = request.client.host if request.client else "unknown"

    def _truthy(value: str | None) -> bool:
        if value is None:
            return False
        return value.strip().lower() in ("true", "1", "yes", "on")

    def _mask_client_ip(value: str) -> str:
        """
        Return a privacy-preserving representation.
        - IPv4: mask to /24 (x.y.z.0)
        - IPv6: mask to /64 (network/64)
        """
        raw = (value or "").strip()
        if not raw or raw == "unknown":
            return "unknown"
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return "unknown"

        if isinstance(addr, ipaddress.IPv4Address):
            net = ipaddress.IPv4Network(f"{addr}/24", strict=False)
            return str(net.network_address)

        net = ipaddress.IPv6Network(f"{addr}/64", strict=False)
        return str(net.network_address)

    include_ip = _truthy(database.get_setting("reachout_include_ip")) or _truthy(os.getenv("REACHOUT_INCLUDE_IP"))
    ip_line = ""
    if include_ip:
        masked_client_ip = _mask_client_ip(client_ip)
        ip_line = f"<div><strong>IP (masked):</strong> {html.escape(masked_client_ip)}</div>"

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; color: #111;">
      <div style="max-width: 680px; margin: 0 auto;">
        <h2 style="margin: 0 0 16px 0;">User reachout ({mode})</h2>
        <div style="border: 1px solid #e5e7eb; border-radius: 12px; padding: 14px 16px; background: #fafafa;">
          <div style="white-space: pre-wrap; line-height: 1.45;">{safe_msg}</div>
        </div>
        <div style="margin-top: 14px; font-size: 12px; color: #6b7280;">
          <div><strong>Instance:</strong> {html.escape(instance_name)}</div>
          <div><strong>User ID:</strong> {html.escape(str(user_id))}</div>
          {ip_line}
          <div><strong>User-Agent:</strong> {html.escape(ua[:300])}</div>
        </div>
      </div>
    </body>
    </html>
    """

    if smtp.get("mock_mode"):
        logger.info("[REACHOUT - Mock Mode] Would send reachout email")
        logger.info("To: %s", to_email)
        logger.info("Subject: %s", subject)
        logger.info("User ID: %s", user_id)
        return ReachoutResponse(success=True, message="Message sent")

    try:
        await asyncio.to_thread(_send_html_email_smtp, smtp, to_email, subject, html_body, reply_to)
        return ReachoutResponse(success=True, message="Message sent")
    except Exception as e:
        logger.exception("Failed to send reachout email (to=%s user_id=%s)", to_email, user_id)
        raise HTTPException(status_code=503, detail="Failed to send message") from e


@app.post("/auth/test-email", response_model=TestEmailResponse)
async def send_test_email(
    body: TestEmailRequest,
    admin: dict = Depends(auth.require_admin)
):
    """
    Send a test email to verify SMTP configuration.
    Requires admin authentication.
    """
    import smtplib
    import socket

    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email address is required")

    # Get SMTP config
    smtp = auth._get_smtp_config()

    # Check if SMTP is configured
    if not smtp["host"]:
        return TestEmailResponse(
            success=False,
            message="SMTP not configured",
            error="SMTP_HOST is not set. Configure SMTP settings first."
        )

    # Check if mock mode
    if smtp["mock_mode"]:
        logger.info("[TEST EMAIL - Mock Mode] Would send test email")
        return TestEmailResponse(
            success=True,
            message=f"Mock mode enabled. Test email would be sent to {email}."
        )

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; color: #333;">
        <div style="max-width: 480px; margin: 0 auto;">
            <h2 style="color: #333; margin-bottom: 24px;">Enclave Test Email</h2>
            <p style="margin-bottom: 24px;">
                This is a test email from your Enclave instance.
                If you received this, your SMTP configuration is working correctly.
            </p>
            <p style="margin-top: 24px; font-size: 14px; color: #666;">
                You can safely delete this email.
            </p>
        </div>
    </body>
    </html>
    """
    subject = "Enclave Test Email"

    try:
        await asyncio.to_thread(_send_html_email_smtp, smtp, email, subject, html)

        logger.info("Test email sent successfully")

        # Store successful test status in database for health checks
        # Wrapped in try/except - email was sent, so primary operation succeeded
        # even if status tracking fails
        try:
            from datetime import datetime, timezone
            database.upsert_deployment_config(
                key="SMTP_LAST_TEST_AT",
                value=datetime.now(timezone.utc).isoformat(),
                is_secret=False,
                requires_restart=False,
                category="email",
                description="Timestamp of last successful SMTP test",
            )
            database.upsert_deployment_config(
                key="SMTP_LAST_TEST_SUCCESS",
                value="true",
                is_secret=False,
                requires_restart=False,
                category="email",
                description="Whether last SMTP test was successful",
            )
        except Exception as db_err:
            logger.warning(f"Failed to update SMTP test status in database: {db_err}")

        return TestEmailResponse(
            success=True,
            message=f"Test email sent to {email}"
        )

    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication failed: {e}")
        return TestEmailResponse(
            success=False,
            message="SMTP authentication failed",
            error=str(e)
        )
    except smtplib.SMTPConnectError as e:
        logger.error(f"SMTP connection failed: {e}")
        return TestEmailResponse(
            success=False,
            message="Could not connect to SMTP server",
            error=str(e)
        )
    except TimeoutError:
        logger.error(f"SMTP connection timed out after {smtp['timeout']}s")
        return TestEmailResponse(
            success=False,
            message="SMTP connection timed out",
            error=f"Connection timed out after {smtp['timeout']} seconds"
        )
    except socket.gaierror as e:
        # DNS / hostname resolution failure
        logger.error("SMTP hostname resolution failed for host=%r: %s", smtp.get("host"), e)
        return TestEmailResponse(
            success=False,
            message="Could not resolve SMTP host",
            error=f"DNS lookup failed for SMTP_HOST={smtp.get('host')!r}: {e}",
        )
    except Exception as e:
        logger.error(f"Failed to send test email: {e}")
        return TestEmailResponse(
            success=False,
            message="Failed to send test email",
            error=str(e)
        )


# =============================================================================
# Admin & User Management Endpoints (SQLite)
# =============================================================================

# --- Admin Authentication ---

@app.post("/admin/auth", response_model=AdminAuthResponse)
async def admin_auth(
    response: Response,
    request: Request,
    body: AdminAuthRequest,
    _: None = Depends(admin_auth_limiter)
):
    """
    Authenticate or register an admin by verifying a signed Nostr event.

    The event must:
    - Be kind 22242 (Enclave auth event)
    - Have action tag = "admin_auth"
    - Have valid BIP-340 Schnorr signature
    - Be signed within the last 5 minutes

    Rate limited to 10 requests per minute per IP.
    """
    # Convert Pydantic model to dict for verification
    event = body.event.model_dump()

    # Verify the signed event
    valid, error = verify_auth_event(event)
    if not valid:
        raise HTTPException(status_code=401, detail=error)

    # Extract pubkey from verified event
    pubkey = get_pubkey_from_event(event)
    if not pubkey:
        raise HTTPException(status_code=401, detail="Missing pubkey in event")

    # Check if admin exists
    existing = database.get_admin_by_pubkey(pubkey)

    # ==========================================================================
    # SECURITY: Single-admin restriction (ENFORCED)
    #
    # Only ONE admin can exist per instance. The first person to authenticate
    # via NIP-07 becomes the admin. Subsequent attempts are rejected.
    # ==========================================================================
    
    is_new = existing is None
    instance_has_admin = database.has_admin()

    if is_new and instance_has_admin:
        # Someone is trying to register as admin but an admin already exists
        raise HTTPException(
            status_code=403,
            detail="Admin registration is closed. This instance already has an admin."
        )

    if is_new:
        # First admin creation - use our single admin constraint
        database.add_admin(pubkey)
        admin = database.get_admin_by_pubkey(pubkey)
    else:
        admin = existing

    # Mark instance setup as complete after successful admin authentication
    database.mark_instance_setup_complete()

    # Create session token for subsequent authenticated requests
    session_token = auth.create_admin_session_token(
        admin["id"],
        pubkey,
        int(admin.get("session_nonce", 0) or 0),
    )
    auth.set_admin_session_cookie(response, session_token)

    return AdminAuthResponse(
        admin=AdminResponse(**admin),
        is_new=is_new,
        instance_initialized=instance_has_admin or is_new,  # True if had admin or just created one
        session_token=session_token
    )


@app.post("/admin/logout", response_model=SuccessResponse)
async def logout_admin(
    response: Response,
    authorization: Optional[str] = Header(None),
    admin_session_cookie: Optional[str] = Cookie(None, alias=auth.ADMIN_SESSION_COOKIE_NAME),
):
    """
    Clear auth session cookies for the current browser and revoke active admin tokens.
    Revocation is best-effort: if a valid admin token is present, rotate session nonce.
    """
    token = auth._resolve_auth_token(authorization, admin_session_cookie)
    if token:
        token_data = auth.verify_admin_session_token(token)
        if token_data:
            admin_pubkey = token_data.get("pubkey")
            if admin_pubkey:
                admin_record = database.get_admin_by_pubkey(admin_pubkey)
                if admin_record and auth.is_admin_session_current(admin_record, token_data):
                    try:
                        database.increment_admin_session_nonce(admin_pubkey)
                    except Exception as e:
                        logger.warning(f"Failed to rotate admin session nonce on logout: {e}")

    auth.clear_auth_cookies(response)
    return SuccessResponse(success=True, message="Logged out")


@app.get("/admin/list", response_model=AdminListResponse)
async def list_admins(admin: dict = Depends(auth.require_admin)):
    """List all admins (requires admin auth)"""
    admins = database.list_admins()
    return AdminListResponse(admins=[AdminResponse(**a) for a in admins])


@app.delete("/admin/{pubkey}", response_model=SuccessResponse)
async def remove_admin(pubkey: str, admin: dict = Depends(auth.require_admin)):
    """Remove an admin by pubkey (requires admin auth)"""
    from nostr_keys import normalize_pubkey
    try:
        pubkey = normalize_pubkey(pubkey)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if database.remove_admin(pubkey):
        return SuccessResponse(success=True, message="Admin removed")
    raise HTTPException(status_code=404, detail="Admin not found")


@app.get("/admin/session", response_model=SuccessResponse)
async def validate_admin_session(admin: dict = Depends(auth.require_admin)):
    """Validate the current admin session token."""
    return SuccessResponse(success=True, message="Admin session is valid")


# --- Instance Settings ---

# Settings safe to expose publicly (branding only)
SAFE_PUBLIC_SETTINGS = {
    "instance_name",
    "primary_color",
    "description",
    "logo_url",
    "favicon_url",
    "apple_touch_icon_url",
    "icon",
    "assistant_icon",
    "user_icon",
    "assistant_name",
    "user_label",
    "header_layout",
    "header_tagline",
    "chat_bubble_style",
    "chat_bubble_shadow",
    "surface_style",
    "status_icon_set",
    "typography_preset",
    "default_language",
    "default_theme",

    # User reachout (public UI controls only)
    "reachout_enabled",
    "reachout_mode",
    "reachout_title",
    "reachout_description",
    "reachout_button_label",
    "reachout_success_message",
}


def filter_public_settings(settings: dict) -> dict:
    """Filter settings to only include safe public keys."""
    return {k: v for k, v in settings.items() if k in SAFE_PUBLIC_SETTINGS}


@app.get("/instance/status", response_model=InstanceStatusResponse)
async def get_instance_status():
    """
    Public endpoint: Check if instance is initialized and ready for users.

    Used by frontend to determine whether to show:
    - Admin setup flow (if not initialized)
    - User waiting page (if admin exists but setup incomplete)
    - User registration (if setup complete)
    """
    settings = filter_public_settings(database.get_all_settings())
    return InstanceStatusResponse(
        initialized=database.has_admin(),
        setup_complete=database.is_instance_setup_complete(),
        ready_for_users=database.is_instance_setup_complete(),
        settings=settings,
        protected_inference=current_inference_repair_status(),
    )


@app.get("/settings/public", response_model=InstanceSettingsResponse)
async def get_public_settings():
    """Public endpoint: Get instance settings for branding (name, color, etc.)"""
    settings = filter_public_settings(database.get_all_settings())
    return InstanceSettingsResponse(settings=settings)


# Import for public config
from models import PublicConfigResponse


@app.get("/config/public", response_model=PublicConfigResponse)
async def get_public_config() -> PublicConfigResponse:
    """get_public_config returns an intentionally minimal PublicConfigResponse; no runtime settings are exposed."""
    return PublicConfigResponse()


@app.get("/admin/settings", response_model=InstanceSettingsResponse)
async def get_settings(admin: dict = Depends(auth.require_admin)):
    """Get all instance settings (requires admin auth)"""
    settings = database.get_all_settings()
    return InstanceSettingsResponse(settings=settings)


@app.put("/admin/settings", response_model=InstanceSettingsResponse)
async def update_settings(settings: InstanceSettings, admin: dict = Depends(auth.require_admin)):
    """Update instance settings (requires admin auth)"""
    settings_dict = settings.model_dump(exclude_unset=True)
    existing_settings = database.get_all_settings()
    database.update_settings(settings_dict)
    # Record which keys the operator explicitly set so guided onboarding can tell
    # an intentional choice from an untouched default (even if they match).
    database.mark_onboarding_configured_keys(settings_dict.keys())
    if "auto_approve_users" in settings_dict:
        updated_settings = database.get_all_settings()
        old_value = str(existing_settings.get("auto_approve_users", "true")).lower()
        new_value = str(updated_settings.get("auto_approve_users", settings_dict["auto_approve_users"])).lower()
        if old_value != new_value:
            _best_effort_config_audit_event(
                table_name="instance_settings",
                config_key="auto_approve_users",
                old_value=old_value,
                new_value=new_value,
                changed_by=admin.get("pubkey", "unknown"),
            )
    return InstanceSettingsResponse(settings=database.get_all_settings())


# --- User Types ---

@app.get("/user-types", response_model=UserTypeListResponse)
async def get_user_types_public():
    """Public endpoint: Get all user types for onboarding UI"""
    types = database.list_user_types()
    return UserTypeListResponse(types=[UserTypeResponse(**t) for t in types])


@app.get("/user-fields", response_model=FieldDefinitionListResponse)
async def get_user_fields_public(
    user_type_id: Optional[int] = Query(None),
    include_global: bool = Query(True)
):
    """
    Public endpoint: Get user field definitions for onboarding UI.
    If user_type_id is provided, returns type-specific fields.
    If include_global is True (default), also includes global fields.
    """
    fields = database.get_field_definitions(user_type_id=user_type_id, include_global=include_global)
    return FieldDefinitionListResponse(
        fields=[FieldDefinitionResponse(**f) for f in fields]
    )


@app.get("/admin/user-types", response_model=UserTypeListResponse)
async def list_user_types(admin: dict = Depends(auth.require_admin)):
    """Get all user types (requires admin auth)"""
    types = database.list_user_types()
    return UserTypeListResponse(types=[UserTypeResponse(**t) for t in types])


@app.post("/admin/user-types", response_model=UserTypeResponse)
async def create_user_type(user_type: UserTypeCreate, admin: dict = Depends(auth.require_admin)):
    """Create a new user type (requires admin auth)"""
    try:
        type_id = database.create_user_type(
            name=user_type.name,
            description=user_type.description,
            icon=user_type.icon,
            display_order=user_type.display_order
        )
        created = database.get_user_type(type_id)
        _best_effort_config_audit_event(
            table_name="user_types",
            config_key=f"user_type:{type_id}:create",
            old_value=None,
            new_value=str({
                "name": created.get("name"),
                "description": created.get("description"),
                "icon": created.get("icon"),
                "display_order": created.get("display_order"),
            }),
            changed_by=admin.get("pubkey", "unknown"),
        )
        return UserTypeResponse(**created)
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(status_code=400, detail="User type name already exists")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/admin/user-types/{type_id}", response_model=UserTypeResponse)
async def update_user_type(type_id: int, user_type: UserTypeUpdate, admin: dict = Depends(auth.require_admin)):
    """Update a user type (requires admin auth)"""
    existing = database.get_user_type(type_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User type not found")

    database.update_user_type(
        type_id,
        name=user_type.name,
        description=user_type.description,
        icon=user_type.icon,
        display_order=user_type.display_order
    )
    updated = database.get_user_type(type_id)
    _best_effort_config_audit_event(
        table_name="user_types",
        config_key=f"user_type:{type_id}:update",
        old_value=str({
            "name": existing.get("name"),
            "description": existing.get("description"),
            "icon": existing.get("icon"),
            "display_order": existing.get("display_order"),
        }),
        new_value=str({
            "name": updated.get("name"),
            "description": updated.get("description"),
            "icon": updated.get("icon"),
            "display_order": updated.get("display_order"),
        }),
        changed_by=admin.get("pubkey", "unknown"),
    )
    return UserTypeResponse(**updated)


@app.delete("/admin/user-types/{type_id}", response_model=SuccessResponse)
async def delete_user_type(type_id: int, admin: dict = Depends(auth.require_admin)):
    """Delete a user type (requires admin auth, cascades to field definitions)"""
    existing = database.get_user_type(type_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User type not found")
    if database.delete_user_type(type_id):
        _best_effort_config_audit_event(
            table_name="user_types",
            config_key=f"user_type:{type_id}:delete",
            old_value=str({
                "name": existing.get("name"),
                "description": existing.get("description"),
                "icon": existing.get("icon"),
                "display_order": existing.get("display_order"),
            }),
            new_value=None,
            changed_by=admin.get("pubkey", "unknown"),
        )
        return SuccessResponse(success=True, message="User type deleted")
    raise HTTPException(status_code=500, detail="Failed to delete user type")


# --- Resource Referral Directory ---


def _resource_slug(name: Optional[str]) -> str:
    """Derive a stable resource_id slug from a name, falling back to a random id."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    if not base:
        return f"resource-{uuid.uuid4().hex[:12]}"
    return base[:100]


def _resource_audit_value(resource: dict | None) -> Optional[str]:
    if resource is None:
        return None
    pointers = resource.get("pointers") or []
    safe_pointer_facts = [
        {
            "type": pointer.get("type"),
            "label": pointer.get("label"),
        }
        for pointer in pointers
        if isinstance(pointer, dict)
    ]
    return json.dumps(
        {
            "resource_id": resource.get("resource_id"),
            "name": resource.get("name"),
            "kind": resource.get("kind"),
            "tags": resource.get("tags"),
            "pointer_count": len(pointers),
            "pointers": safe_pointer_facts,
            "regions": resource.get("regions"),
            "provenance": resource.get("provenance"),
            "display_order": resource.get("display_order"),
            "status": resource.get("status"),
        },
        sort_keys=True,
    )


def _validate_resource_scope(scope_level: Optional[str], scope_code: Optional[str]) -> None:
    if scope_level is None:
        return
    if not region_data.is_valid_scope(scope_level, scope_code):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid coverage scope for level '{scope_level}': unknown or missing scope_code",
        )


def _validate_resource_regions(regions: Optional[list]) -> None:
    for region in regions or []:
        level = region.level if hasattr(region, "level") else region.get("level")
        code = region.code if hasattr(region, "code") else region.get("code")
        _validate_resource_scope(level, code)


@app.get("/admin/regions")
async def list_regions_admin(admin: dict = Depends(auth.require_admin)) -> dict:
    """Coverage taxonomy for the Resource Directory coverage picker.

    Returns countries (with their subregion/region ancestors), subregions, and
    regions, all with M49/ISO codes + names, so the UI can offer a type-to-search
    picker where searching a country also surfaces its subregion and region.
    """
    countries = []
    subregions: dict[str, dict] = {}
    for (
        country_code,
        country_name,
        subregion_code,
        subregion_name,
        region_code,
        region_name,
    ) in region_data.iter_country_region_rows():
        countries.append(
            {
                "level": "country",
                "code": country_code,
                "name": country_name,
                "subregion_code": subregion_code,
                "subregion_name": subregion_name,
                "region_code": region_code,
                "region_name": region_name,
            }
        )
        if subregion_code and subregion_code not in subregions:
            subregions[subregion_code] = {
                "level": "subregion",
                "code": subregion_code,
                "name": subregion_name,
                "region_code": region_code,
                "region_name": region_name,
            }

    regions = [
        {"level": "region", "code": code, "name": name}
        for code, name in region_data.REGION_NAMES.items()
    ]

    return {
        "countries": sorted(countries, key=lambda c: c["name"]),
        "subregions": sorted(subregions.values(), key=lambda s: s["name"]),
        "regions": sorted(regions, key=lambda r: r["name"]),
    }


@app.get("/admin/resources", response_model=ResourceListResponse)
async def list_resources_admin(
    status: Optional[str] = Query(None),
    query: Optional[str] = Query(None, max_length=500),
    kind: Optional[str] = Query(None),
    tags: Optional[list[str]] = Query(None),
    admin: dict = Depends(auth.require_admin),
) -> ResourceListResponse:
    """List directory resources (requires admin auth)."""
    resources = database.list_resources(status=status, query=query, kind=kind, tags=tags)
    return ResourceListResponse(resources=[ResourceResponse(**r) for r in resources])


@app.post("/admin/resources", response_model=ResourceResponse)
async def create_resource_admin(resource: ResourceCreate, admin: dict = Depends(auth.require_admin)) -> ResourceResponse:
    """Create a directory resource (requires admin auth). Status is auto-computed:
    a resource stays `pending` until all required fields are present, then becomes `ready`."""
    _validate_resource_regions(resource.regions)

    resource_id = resource.resource_id or _resource_slug(resource.name)
    if database.get_resource(resource_id) is not None:
        raise HTTPException(status_code=400, detail=f"Resource id already exists: {resource_id}")

    provenance = {}
    if resource.provenance:
        provenance.update(resource.provenance.model_dump(exclude_unset=True))
    if resource.verified is not None:
        provenance["verified_at"] = (
            provenance.get("verified_at") or database.utc_timestamp_z()
            if resource.verified
            else None
        )

    created = database.create_resource(
        resource_id=resource_id,
        name=resource.name,
        kind=resource.kind,
        tags=resource.tags,
        pointers=[pointer.model_dump() for pointer in resource.pointers]
        if resource.pointers is not None
        else None,
        regions=[region.model_dump() for region in resource.regions]
        if resource.regions is not None
        else None,
        provenance=provenance,
        description=resource.description,
        languages=resource.languages,
        display_order=resource.display_order,
        archived=resource.archived,
    )
    _best_effort_config_audit_event(
        table_name="resources",
        config_key=f"resource:{resource_id}:create",
        old_value=None,
        new_value=_resource_audit_value(created),
        changed_by=admin.get("pubkey", "unknown"),
    )
    return ResourceResponse(**created)


@app.put("/admin/resources/{resource_id}", response_model=ResourceResponse)
async def update_resource_admin(
    resource_id: str,
    resource: ResourceUpdate,
    admin: dict = Depends(auth.require_admin),
) -> ResourceResponse:
    """Update a directory resource (requires admin auth). Status is recomputed."""
    existing = database.get_resource(resource_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Resource not found")

    provided = resource.model_dump(exclude_unset=True)
    if "regions" in provided:
        _validate_resource_regions(provided.get("regions"))

    # Map the `verified` boolean onto a verified_at timestamp.
    kwargs = {k: v for k, v in provided.items() if k not in {"verified", "provenance"}}
    provenance = dict(existing.get("provenance") or {})
    if "verified" in provided:
        provenance["verified_at"] = (
            provenance.get("verified_at") or database.utc_timestamp_z()
            if provided["verified"]
            else None
        )
    if "provenance" in provided and provided["provenance"] is not None:
        provenance.update(provided["provenance"])
    if "provenance" in provided or "verified" in provided:
        kwargs["provenance"] = provenance

    updated = database.update_resource(resource_id, **kwargs)
    _best_effort_config_audit_event(
        table_name="resources",
        config_key=f"resource:{resource_id}:update",
        old_value=_resource_audit_value(existing),
        new_value=_resource_audit_value(updated),
        changed_by=admin.get("pubkey", "unknown"),
    )
    return ResourceResponse(**updated)


@app.delete("/admin/resources/{resource_id}", response_model=SuccessResponse)
async def delete_resource_admin(resource_id: str, admin: dict = Depends(auth.require_admin)) -> SuccessResponse:
    """Delete a directory resource (requires admin auth)."""
    existing = database.get_resource(resource_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Resource not found")
    if database.delete_resource(resource_id):
        _best_effort_config_audit_event(
            table_name="resources",
            config_key=f"resource:{resource_id}:delete",
            old_value=_resource_audit_value(existing),
            new_value=None,
            changed_by=admin.get("pubkey", "unknown"),
        )
        return SuccessResponse(success=True, message="Resource deleted")
    raise HTTPException(status_code=500, detail="Failed to delete resource")


# --- Session Logs (Test & Feedback) ---
# Captured chat transcripts the admin can review and rate. Transcripts are NIP-04
# encrypted to the admin pubkey at rest (see session_logs.py); the backend only
# ever returns ciphertext, which the admin decrypts client-side via NIP-07.

@app.get("/admin/session-logs", response_model=SessionLogListResponse)
async def list_session_logs_admin(
    source: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    admin: dict = Depends(auth.require_admin),
) -> SessionLogListResponse:
    """List session-log metadata (no transcript content), newest first."""
    logs = session_logs.list_session_logs(source=source, status=status)
    return SessionLogListResponse(
        session_logs=[SessionLogMetadata(**log) for log in logs]
    )


@app.post("/admin/session-logs", response_model=SessionLogMetadata)
async def create_session_log_admin(
    payload: SessionLogCreate,
    admin: dict = Depends(auth.require_admin),
) -> SessionLogMetadata:
    """Open a new (active) session log; the transcript is saved on completion."""
    try:
        created = session_logs.create_session_log(
            source=payload.source,
            title=payload.title,
            subject_user_id=payload.subject_user_id,
            user_type_id=payload.user_type_id,
            sage_session_id=payload.sage_session_id,
            created_by=admin.get("pubkey"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SessionLogMetadata(**created)


@app.post("/admin/session-logs/{log_id}/transcript", response_model=SessionLogMetadata)
async def save_session_log_transcript_admin(
    log_id: str,
    payload: SessionLogSaveTranscript,
    admin: dict = Depends(auth.require_admin),
) -> SessionLogMetadata:
    """Encrypt + persist the transcript and mark the log completed.

    Fails closed with 409 if no admin pubkey is configured to encrypt to, so a
    transcript is never written in plaintext.
    """
    turns = [turn.model_dump() for turn in payload.turns]
    try:
        updated = session_logs.save_transcript(
            log_id, turns, created_by=admin.get("pubkey")
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Session log not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return SessionLogMetadata(**updated)


@app.get("/admin/session-logs/{log_id}", response_model=SessionLogDetail)
async def get_session_log_admin(
    log_id: str,
    admin: dict = Depends(auth.require_admin),
) -> SessionLogDetail:
    """Full log for review: metadata + encrypted transcript + message feedback."""
    detail = session_logs.get_session_log(log_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Session log not found")
    return SessionLogDetail(**detail)


@app.get("/admin/session-logs/{log_id}/export")
async def export_session_log_admin(
    log_id: str,
    admin: dict = Depends(auth.require_admin),
) -> Response:
    """Download an encrypted evidence bundle for one Test & Feedback session."""
    try:
        filename, payload = session_logs.export_session_log_zip(
            log_id,
            changed_by=admin.get("pubkey"),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Session log not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post(
    "/admin/session-logs/{log_id}/plaintext-export",
    response_model=SuccessResponse,
)
async def record_plaintext_session_log_export_admin(
    log_id: str,
    admin: dict = Depends(auth.require_admin),
) -> SuccessResponse:
    """Audit a browser-generated decrypted export without receiving plaintext."""
    filename = f"beta-session-log-{log_id}.json"
    try:
        session_logs.record_plaintext_export(
            log_id,
            filename=filename,
            changed_by=admin.get("pubkey"),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Session log not found")
    return SuccessResponse(success=True, message="Session log export recorded")


@app.put(
    "/admin/session-logs/{log_id}/messages/{message_id}/feedback",
    response_model=SessionLogMessageFeedback,
)
async def set_session_log_feedback_admin(
    log_id: str,
    message_id: str,
    payload: SessionMessageFeedbackRequest,
    admin: dict = Depends(auth.require_admin),
) -> SessionLogMessageFeedback:
    """Set an assistant message's thumbs up/down + optional (encrypted) comment."""
    try:
        feedback = session_logs.set_message_feedback(
            log_id,
            message_id,
            payload.rating,
            comment=payload.comment,
            created_by=admin.get("pubkey"),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Session log not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SessionLogMessageFeedback(**feedback)


@app.delete("/admin/session-logs/{log_id}", response_model=SuccessResponse)
async def delete_session_log_admin(
    log_id: str,
    admin: dict = Depends(auth.require_admin),
) -> SuccessResponse:
    """Delete a session log, its feedback, and any legacy transcript file."""
    if session_logs.delete_session_log(log_id):
        return SuccessResponse(success=True, message="Session log deleted")
    raise HTTPException(status_code=404, detail="Session log not found")


# --- Test users + impersonation seam (Test as User) ---

def _test_user_email(user_type_id: Optional[int]) -> str:
    """Deterministic, reserved address so provisioning is idempotent per persona."""
    return (
        f"test-user+type{user_type_id}@enclave.test"
        if user_type_id is not None
        else "test-user@enclave.test"
    )


@app.post("/admin/test-users/provision", response_model=TestUserProvisionResponse)
async def provision_test_user_admin(
    user_type_id: Optional[int] = Query(None),
    admin: dict = Depends(auth.require_admin),
) -> TestUserProvisionResponse:
    """Ensure an approved, non-admin test user exists for the given persona
    (user type) so an admin can impersonate and chat as them. Idempotent.

    The test user is given a deterministic, instance-owned Nostr subkey derived
    from the admin pubkey + persona, so it transparently belongs to this admin
    and impersonation can mint a real, scoped session for it.
    """
    from test_user_provenance import record_provisioning

    admin_pubkey = admin.get("pubkey")
    if user_type_id is not None and not database.get_user_type(user_type_id):
        raise HTTPException(status_code=404, detail="User type not found")

    derived_pubkey = impersonation.derive_test_user_pubkey(admin_pubkey, user_type_id)
    email = _test_user_email(user_type_id)

    existing = database.get_user_by_email(email)
    if existing:
        # A type migration may have moved this account. Restore the reserved
        # persona so token validation derives the same key as provisioning.
        if existing.get("user_type_id") != user_type_id:
            database.update_user_type_id(existing["id"], user_type_id)
        if not existing.get("approved"):
            database.update_user_approval(existing["id"], True)
        # Backfill / correct the derived subkey (e.g. for users created before this).
        if (existing.get("pubkey") or "") != derived_pubkey:
            database.set_user_pubkey(existing["id"], derived_pubkey)
        record_provisioning(existing["id"], admin_pubkey, user_type_id)
        return TestUserProvisionResponse(
            user_id=existing["id"], user_type_id=user_type_id, created=False
        )
    try:
        user_id = database.create_user(
            pubkey=derived_pubkey,
            email=email,
            name="Test User",
            user_type_id=user_type_id,
        )
    except ValueError as exc:
        # create_user encrypts to the admin pubkey; fail closed if none exists.
        raise HTTPException(status_code=409, detail=str(exc))
    record_provisioning(user_id, admin_pubkey, user_type_id)
    database.update_user_approval(user_id, True)
    return TestUserProvisionResponse(
        user_id=user_id, user_type_id=user_type_id, created=True
    )


@app.get("/admin/impersonation/status")
async def impersonation_status_admin(
    admin: dict = Depends(auth.require_admin),
) -> dict:
    """Whether admin-as-test-user impersonation is wired in this deployment.

    Lets the UI gate cleanly instead of probing the token endpoint and getting a
    501 on every session start.
    """
    return {"available": impersonation.is_available()}


@app.post("/admin/test-users/{user_id}/impersonation-token")
async def issue_impersonation_token_admin(
    user_id: int,
    admin: dict = Depends(auth.require_admin),
) -> dict:
    """Mint a scoped session handle to chat AS the given test user.

    Delegates to the impersonation seam; returns 501 until the impersonation
    script is wired in (see impersonation.py).
    """
    user = database.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Test user not found")
    if not impersonation.is_available():
        raise HTTPException(
            status_code=501,
            detail="Impersonation is not configured on this deployment yet.",
        )
    try:
        return impersonation.issue_session_token(
            user_id=user_id, issued_by_pubkey=admin.get("pubkey")
        )
    except impersonation.ImpersonationUnavailable as exc:
        raise HTTPException(status_code=403, detail=str(exc))


# --- User Field Definitions ---

@app.get("/admin/user-fields", response_model=FieldDefinitionListResponse)
async def get_field_definitions(user_type_id: Optional[int] = Query(None), admin: dict = Depends(auth.require_admin)):
    """Get user field definitions (requires admin auth).
    If user_type_id is provided, returns global fields + type-specific fields.
    """
    fields = database.get_field_definitions(user_type_id=user_type_id, include_global=True)
    return FieldDefinitionListResponse(
        fields=[FieldDefinitionResponse(**f) for f in fields]
    )


@app.post("/admin/user-fields", response_model=FieldDefinitionResponse)
async def create_field_definition(field: FieldDefinitionCreate, admin: dict = Depends(auth.require_admin)):
    """Create a new user field definition (requires admin auth).
    user_type_id: null = global field (shown for all types), or ID for type-specific
    """
    # Validate user_type_id if provided
    if field.user_type_id is not None:
        if not database.get_user_type(field.user_type_id):
            raise HTTPException(status_code=400, detail="User type not found")

    # Validation: Cannot include encrypted fields in chat context
    if field.include_in_chat and field.encryption_enabled:
        raise HTTPException(
            status_code=400,
            detail="Cannot include encrypted fields in chat context. Encrypted fields require admin's private key to decrypt."
        )

    try:
        field_id = database.create_field_definition(
            field_name=field.field_name,
            field_type=field.field_type,
            required=field.required,
            display_order=field.display_order,
            user_type_id=field.user_type_id,
            placeholder=field.placeholder,
            options=field.options,
            encryption_enabled=field.encryption_enabled,
            include_in_chat=field.include_in_chat
        )
        created = database.get_field_definition_by_id(field_id)
        return FieldDefinitionResponse(**created)
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(status_code=400, detail="Field name already exists for this type")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/admin/user-fields/{field_id}", response_model=FieldDefinitionResponse)
async def update_field_definition(field_id: int, field: FieldDefinitionUpdate, admin: dict = Depends(auth.require_admin)):
    """Update a field definition (requires admin auth)"""
    existing = database.get_field_definition_by_id(field_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Field definition not found")

    # Validate user_type_id if provided
    if field.user_type_id is not None and field.user_type_id != 0:
        if not database.get_user_type(field.user_type_id):
            raise HTTPException(status_code=400, detail="User type not found")

    # Determine effective encryption state (use existing if not specified)
    effective_encryption = field.encryption_enabled if field.encryption_enabled is not None else existing.get("encryption_enabled", 1)
    effective_include_in_chat = field.include_in_chat if field.include_in_chat is not None else existing.get("include_in_chat", 0)

    # Validation: Cannot include encrypted fields in chat context
    if effective_include_in_chat and effective_encryption:
        raise HTTPException(
            status_code=400,
            detail="Cannot include encrypted fields in chat context. Encrypted fields require admin's private key to decrypt."
        )

    database.update_field_definition(
        field_id,
        field_name=field.field_name,
        field_type=field.field_type,
        required=field.required,
        display_order=field.display_order,
        user_type_id=field.user_type_id if field.user_type_id != 0 else None,
        placeholder=field.placeholder,
        options=field.options,
        encryption_enabled=field.encryption_enabled,
        include_in_chat=field.include_in_chat
    )
    updated = database.get_field_definition_by_id(field_id)
    return FieldDefinitionResponse(**updated)


@app.delete("/admin/user-fields/{field_id}", response_model=SuccessResponse)
async def delete_field_definition(field_id: int, admin: dict = Depends(auth.require_admin)):
    """Delete a user field definition (requires admin auth)"""
    if database.delete_field_definition(field_id):
        return SuccessResponse(success=True, message="Field definition deleted")
    raise HTTPException(status_code=404, detail="Field definition not found")


@app.put("/admin/user-fields/{field_id}/encryption", response_model=FieldEncryptionResponse)
async def update_field_encryption(
    field_id: int,
    encryption_request: FieldEncryptionRequest,
    _admin: dict = Depends(auth.require_admin)
) -> FieldEncryptionResponse:
    """Update encryption setting for a field definition (requires admin auth).

    WARNING: Changing encryption settings may affect existing data.
    - Enabling encryption: Future values will be encrypted, existing plaintext remains
    - Disabling encryption: Future values stored as plaintext, existing encrypted data remains

    Use force=true to bypass warnings about data migration complexity.

    Note: Enabling encryption will auto-disable include_in_chat since encrypted
    fields cannot be included in AI chat context.
    """
    # Check if field exists
    field_def = database.get_field_definition_by_id(field_id)
    if not field_def:
        raise HTTPException(status_code=404, detail="Field definition not found")

    current_encryption = field_def.get("encryption_enabled", 1)
    new_encryption = encryption_request.encryption_enabled

    warning = None

    # Check for potential data impact
    if current_encryption != int(new_encryption):
        # TODO: Check if field has existing values that would be affected
        # For now, provide general warning
        if not new_encryption:
            warning = "⚠️ Disabling encryption will store future values as plaintext. Existing encrypted data remains encrypted."
        else:
            warning = "Enabling encryption will encrypt future values. Existing plaintext data remains unencrypted."

        if not encryption_request.force and warning:
            raise HTTPException(
                status_code=400,
                detail=f"{warning} Use force=true to confirm this change."
            )

    # When enabling encryption, auto-disable include_in_chat
    include_in_chat_update = None
    if new_encryption and field_def.get("include_in_chat", 0):
        include_in_chat_update = False
        if warning:
            warning += " Also disabled 'include in chat' since encrypted fields cannot be included in AI chat context."
        else:
            warning = "Disabled 'include in chat' since encrypted fields cannot be included in AI chat context."

    # Update encryption setting (and include_in_chat if needed)
    success = database.update_field_definition(
        field_id,
        encryption_enabled=new_encryption,
        include_in_chat=include_in_chat_update
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to update field encryption")

    return FieldEncryptionResponse(
        field_id=field_id,
        encryption_enabled=new_encryption,
        warning=warning,
        migrated_values=0  # TODO: Implement data migration counting
    )


# --- Users ---

@app.get("/admin/users", response_model=UserListResponse)
async def list_users(admin: dict = Depends(auth.require_admin)):
    """List all users with their field values (requires admin auth)"""
    users = database.list_users()
    return UserListResponse(users=[UserResponse(**u) for u in users])


@app.post("/admin/users/roster-export", response_model=SuccessResponse)
async def record_user_roster_export(
    request: UserRosterExportAuditRequest,
    admin: dict = Depends(auth.require_admin),
):
    """
    Record a copied User Roster Export event.

    The roster workbook is generated in the Admin browser so locally decrypted
    User Profile values never need to be posted back to the backend. This
    endpoint records only export metadata/counts for Audit Log visibility.
    """
    exported_at = database.utc_timestamp_z()
    database.log_config_audit_event(
        table_name="data_deletion",
        config_key="copied_export:user_roster",
        old_value=None,
        new_value=json.dumps({
            "workflow": "copied_export",
            "target": "user_roster",
            "lifecycle_posture": "outside_active_storage_lifecycle",
            "filename": request.filename,
            "user_count": request.user_count,
            "pending_count": request.pending_count,
            "includes_decrypted_browser_values": request.includes_decrypted_browser_values,
            "plaintext_contents_received_by_backend": False,
            "exported_at": exported_at,
        }, sort_keys=True),
        changed_by=admin.get("pubkey", "unknown"),
    )
    return SuccessResponse(success=True, message="User roster export recorded")


@app.post("/admin/users/{user_id}/migrate-type", response_model=UserTypeMigrationResponse)
async def migrate_user_type(
    user_id: int,
    migration: UserTypeMigrationRequest,
    admin: dict = Depends(auth.require_admin)
):
    """Migrate a user to a target user type (admin only)."""
    target_type = database.get_user_type(migration.target_user_type_id)
    if not target_type:
        raise HTTPException(status_code=400, detail="Target user type not found")

    user = database.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    previous_user_type_id = user.get("user_type_id")
    missing_required_fields = _missing_required_field_names_for_type(user, migration.target_user_type_id)

    if missing_required_fields and not migration.allow_incomplete:
        raise HTTPException(
            status_code=400,
            detail=f"Migration would leave missing required fields: {', '.join(missing_required_fields)}"
        )

    if previous_user_type_id != migration.target_user_type_id:
        updated = database.update_user_type_id(user_id, migration.target_user_type_id)
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to update user type")
        _best_effort_config_audit_event(
            table_name="user_types",
            config_key=f"user:{user_id}:migrate_type",
            old_value=str({
                "user_id": user_id,
                "user_type_id": previous_user_type_id,
            }),
            new_value=str({
                "user_id": user_id,
                "user_type_id": migration.target_user_type_id,
                "user_type_name": target_type.get("name"),
                "missing_required_count": len(missing_required_fields),
            }),
            changed_by=admin.get("pubkey", "unknown"),
        )

    return UserTypeMigrationResponse(
        success=True,
        user_id=user_id,
        previous_user_type_id=previous_user_type_id,
        target_user_type_id=migration.target_user_type_id,
        missing_required_count=len(missing_required_fields),
        missing_required_fields=missing_required_fields,
    )


@app.post("/admin/users/migrate-type/batch", response_model=UserTypeMigrationBatchResponse)
async def migrate_user_type_batch(
    migration: UserTypeMigrationBatchRequest,
    admin: dict = Depends(auth.require_admin)
):
    """Bulk migrate users to a target user type (admin only)."""
    target_type = database.get_user_type(migration.target_user_type_id)
    if not target_type:
        raise HTTPException(status_code=400, detail="Target user type not found")

    if not migration.user_ids:
        raise HTTPException(status_code=400, detail="user_ids must contain at least one user id")

    migrated = 0
    failed = 0
    results: list[UserTypeMigrationBatchResult] = []

    # Deduplicate while preserving incoming order.
    seen_user_ids: set[int] = set()
    ordered_user_ids: list[int] = []
    for user_id in migration.user_ids:
        if user_id in seen_user_ids:
            continue
        seen_user_ids.add(user_id)
        ordered_user_ids.append(user_id)

    for user_id in ordered_user_ids:
        user = database.get_user(user_id)
        if not user:
            failed += 1
            results.append(UserTypeMigrationBatchResult(
                user_id=user_id,
                success=False,
                error="User not found",
            ))
            continue

        previous_user_type_id = user.get("user_type_id")
        missing_required_fields = _missing_required_field_names_for_type(user, migration.target_user_type_id)

        if missing_required_fields and not migration.allow_incomplete:
            failed += 1
            results.append(UserTypeMigrationBatchResult(
                user_id=user_id,
                success=False,
                previous_user_type_id=previous_user_type_id,
                target_user_type_id=migration.target_user_type_id,
                missing_required_count=len(missing_required_fields),
                missing_required_fields=missing_required_fields,
                error=f"Missing required fields after migration: {', '.join(missing_required_fields)}",
            ))
            continue

        try:
            if previous_user_type_id != migration.target_user_type_id:
                updated = database.update_user_type_id(user_id, migration.target_user_type_id)
                if not updated:
                    raise ValueError("Failed to update user type")
                _best_effort_config_audit_event(
                    table_name="user_types",
                    config_key=f"user:{user_id}:migrate_type",
                    old_value=str({
                        "user_id": user_id,
                        "user_type_id": previous_user_type_id,
                    }),
                    new_value=str({
                        "user_id": user_id,
                        "user_type_id": migration.target_user_type_id,
                        "user_type_name": target_type.get("name"),
                        "missing_required_count": len(missing_required_fields),
                        "batch": True,
                    }),
                    changed_by=admin.get("pubkey", "unknown"),
                )
            migrated += 1
            results.append(UserTypeMigrationBatchResult(
                user_id=user_id,
                success=True,
                previous_user_type_id=previous_user_type_id,
                target_user_type_id=migration.target_user_type_id,
                missing_required_count=len(missing_required_fields),
                missing_required_fields=missing_required_fields,
            ))
        except Exception as e:
            failed += 1
            results.append(UserTypeMigrationBatchResult(
                user_id=user_id,
                success=False,
                previous_user_type_id=previous_user_type_id,
                target_user_type_id=migration.target_user_type_id,
                missing_required_count=len(missing_required_fields),
                missing_required_fields=missing_required_fields,
                error=str(e),
            ))

    return UserTypeMigrationBatchResponse(
        success=failed == 0,
        migrated=migrated,
        failed=failed,
        results=results,
    )


def _is_admin_actor(actor: dict) -> bool:
    """Return True when auth context represents an admin."""
    return actor.get("type") == "admin" or actor.get("is_admin") is True or actor.get("role") == "admin"


def _audit_user_data_deletion(config_key: str, changed_by: str, workflow: str, target_id: str, deletion: dict) -> None:
    try:
        _audit_data_deletion_event(
            config_key=config_key,
            changed_by=changed_by,
            workflow=workflow,
            target_id=target_id,
            deletion=deletion,
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


def _require_self_or_admin(target_user_id: int, actor: dict) -> None:
    """Allow access for admins or the user who owns target_user_id."""
    if _is_admin_actor(actor):
        return

    if actor.get("id") != target_user_id:
        raise HTTPException(status_code=403, detail="Forbidden: cannot access another user")


def _audit_actor_for_requester(requester: dict) -> str:
    pubkey = requester.get("pubkey")
    if isinstance(pubkey, str) and pubkey.strip():
        return pubkey

    actor_type = requester.get("type") or "actor"
    actor_id = requester.get("id")
    if actor_id is not None:
        return f"{actor_type}:{actor_id}"

    return "unknown"


@app.get("/users/me/onboarding-status", response_model=OnboardingStatusResponse)
async def get_my_onboarding_status(
    requester: dict = Depends(auth.require_admin_or_user)
):
    """
    Get canonical onboarding completeness status for the authenticated user.

    Returns missing required/optional fields for the user's effective type,
    plus flags for whether user-type selection or onboarding is still needed.
    """
    if requester.get("type") == "admin":
        raise HTTPException(status_code=403, detail="Admins do not have user onboarding status")

    requester_id = requester.get("id")
    if requester_id is None:
        raise HTTPException(status_code=401, detail="Invalid user session")

    user = database.get_user(requester_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    status = _build_onboarding_status(user)
    return OnboardingStatusResponse(**status)


@app.post("/users", response_model=UserResponse)
async def create_user(
    user: UserCreate,
    requester: dict = Depends(auth.require_admin_or_user)
):
    """Create/onboard a new user.

    Args:
        pubkey: Optional Nostr public key (npub or hex)
        email: Optional email address (encrypted, enables email lookups)
        name: Optional user name (encrypted)
        user_type_id: Optional ID of the user type they selected during onboarding
        fields: Dynamic fields defined by admin for the user type

    Requires authenticated user/admin context and admin to be configured first
    (for encryption to work properly).
    """
    # Check if admin is configured (required for data encryption)
    admins = database.list_admins()
    if not admins:
        raise HTTPException(
            status_code=503,
            detail="Instance not configured. An admin must be registered before users can sign up."
        )

    # Validate user_type_id if provided
    if user.user_type_id is not None:
        if not database.get_user_type(user.user_type_id):
            raise HTTPException(status_code=400, detail="User type not found")

    # Normalize pubkey if provided
    pubkey = None
    if user.pubkey:
        from nostr_keys import normalize_pubkey
        try:
            pubkey = normalize_pubkey(user.pubkey)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # Check for existing user (by pubkey or email) to avoid duplicates
    existing_user = None
    if pubkey:
        existing_user = database.get_user_by_pubkey(pubkey)
    if not existing_user and user.email:
        existing_user = database.get_user_by_email(user.email)

    # Non-admin callers may only operate on their own user record.
    # This blocks anonymous/third-party profile mutation by email/pubkey collision.
    if not _is_admin_actor(requester):
        requester_id = requester.get("id")
        if requester_id is None:
            raise HTTPException(status_code=401, detail="Invalid user session")

        session_user = database.get_user(requester_id)
        if existing_user:
            if existing_user["id"] != requester_id:
                raise HTTPException(status_code=403, detail="Forbidden: cannot modify another user")
        elif session_user:
            existing_user = session_user
        else:
            raise HTTPException(status_code=401, detail="User session is not linked to an account")

    # Resolve effective user_type_id (use existing if not provided)
    effective_user_type_id = user.user_type_id
    if effective_user_type_id is None and existing_user:
        effective_user_type_id = existing_user.get("user_type_id")

    # Get field definitions for this user type (global + type-specific)
    if effective_user_type_id is None:
        field_defs = [
            f for f in database.get_field_definitions()
            if f.get("user_type_id") is None
        ]
    else:
        field_defs = database.get_field_definitions(
            user_type_id=effective_user_type_id,
            include_global=True
        )

    # Validate required fields
    # For partial updates (existing user), consider both existing and provided fields
    required_fields = {f["field_name"] for f in field_defs if f["required"]}
    provided_fields = set(user.fields.keys())

    if existing_user:
        # Union the existing user's fields with provided fields for validation
        existing_fields = set(existing_user.get("fields", {}).keys()) | set(existing_user.get("fields_encrypted", {}).keys())
        all_fields = existing_fields | provided_fields
        missing = required_fields - all_fields
    else:
        # New user: only check provided fields
        missing = required_fields - provided_fields

    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required fields: {', '.join(missing)}"
        )

    # Check for unknown fields (only allow fields defined for this type)
    known_fields = {f["field_name"] for f in field_defs}
    unknown = provided_fields - known_fields
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown fields: {', '.join(unknown)}"
        )

    if existing_user:
        # Update existing user (avoid duplicate accounts)
        if user.user_type_id is not None and user.user_type_id != existing_user.get("user_type_id"):
            database.update_user_type_id(existing_user["id"], user.user_type_id)
            effective_user_type_id = user.user_type_id
        if user.fields:
            database.set_user_fields(existing_user["id"], user.fields, user_type_id=effective_user_type_id)
        return UserResponse(**database.get_user(existing_user["id"]))

    # Create user
    try:
        user_id = database.create_user(
            pubkey=pubkey,
            email=user.email,
            name=user.name,
            user_type_id=user.user_type_id
        )
        if user.fields:
            database.set_user_fields(user_id, user.fields, user_type_id=effective_user_type_id)
        return UserResponse(**database.get_user(user_id))
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(status_code=400, detail="User with this pubkey already exists")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    requester: dict = Depends(auth.require_admin_or_user)
):
    """Get a user by ID (self or admin)."""
    _require_self_or_admin(user_id, requester)
    user = database.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(**user)


@app.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user: UserUpdate,
    requester: dict = Depends(auth.require_admin_or_user)
):
    """Update a user's fields (self or admin)."""
    _require_self_or_admin(user_id, requester)
    existing = database.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    approval_changed = user.approved is not None and bool(existing.get("approved", 1)) != user.approved
    if approval_changed and not _is_admin_actor(requester):
        raise HTTPException(status_code=403, detail="Forbidden: only admins can change user approval")

    # Validate fields
    if user.fields:
        field_defs = database.get_field_definitions()
        known_fields = {f["field_name"] for f in field_defs}
        unknown = set(user.fields.keys()) - known_fields
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown fields: {', '.join(unknown)}"
            )
        database.set_user_fields(user_id, user.fields)

    if approval_changed:
        database.update_user_approval(user_id, user.approved)
        _best_effort_config_audit_event(
            table_name="user_approval",
            config_key=f"user:{user_id}:approved",
            old_value="true" if bool(existing.get("approved", 1)) else "false",
            new_value="true" if user.approved else "false",
            changed_by=requester.get("pubkey", "unknown"),
        )

    return UserResponse(**database.get_user(user_id))


@app.delete("/users/{user_id}", response_model=dict)
async def delete_user(
    user_id: int,
    requester: dict = Depends(auth.require_admin_or_user)
):
    """Delete a user (self or admin)."""
    _require_self_or_admin(user_id, requester)
    existing_user = database.get_user(user_id)
    session_memory_results = []
    if not existing_user:
        conversation_result = deletion_target_skipped(
            target_kind="conversation",
            target_id=str(user_id),
            action="delete_conversations",
            detail="Conversation session discovery is owned by Sage.",
        )
        deletion = summarize_deletion_results([
            deletion_target_skipped(
                target_kind="user_profile",
                target_id=str(user_id),
                action="delete_user_profile",
                detail="User Profile was already absent.",
            ),
            deletion_target_skipped(
                target_kind="user_approval",
                target_id=str(user_id),
                action="delete_user_approval",
                detail="User approval/access state was already absent.",
            ),
            deletion_target_skipped(
                target_kind="user_memory",
                target_id=str(user_id),
                action="delete_user_memory",
                detail="User Memory was already absent.",
            ),
            conversation_result,
            *session_memory_results,
        ])
        _audit_user_data_deletion(
            config_key=f"user:{user_id}:delete",
            changed_by=_audit_actor_for_requester(requester),
            workflow="delete_user",
            target_id=str(user_id),
            deletion=deletion,
        )
        return {
            "success": True,
            "message": "User already deleted",
            "deletion": deletion,
        }

    lifecycle_delete = database.delete_user_lifecycle(user_id)
    memory_count = lifecycle_delete["user_memories_deleted"]
    if memory_count:
        user_memory_result = deletion_target_succeeded(
            target_kind="user_memory",
            target_id=str(user_id),
            action="delete_user_memory",
            detail=f"Deleted {memory_count} Sage-owned User Memory records.",
        )
        user_memory_result["count"] = memory_count
    else:
        user_memory_result = deletion_target_skipped(
            target_kind="user_memory",
            target_id=str(user_id),
            action="delete_user_memory",
            detail="No Sage-owned User Memory records were found for this User.",
        )

    conversation_result = deletion_target_skipped(
        target_kind="conversation",
        target_id=str(user_id),
        action="delete_conversations",
        detail="Conversation session discovery is owned by Sage.",
    )

    deletion = summarize_deletion_results([
        deletion_target_succeeded(
            target_kind="user_profile",
            target_id=str(user_id),
            action="delete_user_profile",
            detail="Deleted User Profile and profile field values.",
        ),
        deletion_target_succeeded(
            target_kind="user_approval",
            target_id=str(user_id),
            action="delete_user_approval",
            detail="Deleted User approval/access state with the User Profile.",
        ),
        user_memory_result,
        conversation_result,
        *session_memory_results,
    ])
    _audit_user_data_deletion(
        config_key=f"user:{user_id}:delete",
        changed_by=_audit_actor_for_requester(requester),
        workflow="delete_user",
        target_id=str(user_id),
        deletion=deletion,
    )
    return {
        "success": bool(lifecycle_delete["user_deleted"]),
        "message": "User deleted",
        "deletion": deletion,
    }


# =============================================================================
# Database Explorer Endpoints (Admin)
# =============================================================================

def get_table_columns(table_name: str) -> list[ColumnInfo]:
    """Get column info for a table using PRAGMA table_info"""
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = []
    for row in cursor.fetchall():
        columns.append(ColumnInfo(
            name=row[1],  # name
            type=row[2] or "TEXT",  # type
            nullable=not row[3],  # notnull (inverted)
            primaryKey=bool(row[5]),  # pk
            defaultValue=row[4]  # dflt_value
        ))
    cursor.close()
    return columns


def get_table_row_count(table_name: str) -> int:
    """Get row count for a table"""
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    count = cursor.fetchone()[0]
    cursor.close()
    return count


@app.get("/admin/db/tables", response_model=TablesListResponse)
async def list_db_tables(admin: dict = Depends(auth.require_admin)):
    """
    List all tables with metadata (requires admin auth).
    Returns table names, column info, and row counts.
    """
    conn = database.get_connection()
    cursor = conn.cursor()

    # Get all user-created tables
    cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """)
    table_names = [row[0] for row in cursor.fetchall()]
    cursor.close()

    tables = []
    for name in table_names:
        if name in ALLOWED_TABLES:
            tables.append(TableInfo(
                name=name,
                columns=get_table_columns(name),
                rowCount=get_table_row_count(name)
            ))

    return TablesListResponse(tables=tables)


@app.get("/admin/db/tables/{table_name}", response_model=TableDataResponse)
async def get_db_table_data(
    table_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    admin: dict = Depends(auth.require_admin)
):
    """
    Get table schema and paginated data (requires admin auth).
    """
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=403, detail=f"Access to table '{table_name}' is not allowed")

    try:
        columns = get_table_columns(table_name)
        total_rows = get_table_row_count(table_name)
        total_pages = math.ceil(total_rows / page_size) if total_rows > 0 else 1

        # Get paginated rows
        offset = (page - 1) * page_size
        conn = database.get_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table_name} LIMIT ? OFFSET ?", (page_size, offset))

        # Convert to list of dicts
        col_names = [col.name for col in columns]
        rows = [dict(zip(col_names, row)) for row in cursor.fetchall()]
        cursor.close()
    except sqlite3.Error as error:
        logger.exception("Database error fetching table data", extra={"table": table_name})
        raise HTTPException(
            status_code=500,
            detail=f"Database error reading table '{table_name}': {error}"
        ) from error

    return TableDataResponse(
        table=table_name,
        columns=columns,
        rows=rows,
        totalRows=total_rows,
        page=page,
        pageSize=page_size,
        totalPages=total_pages
    )


@app.get("/admin/db/tables/{table_name}/schema")
async def get_db_table_schema(table_name: str, admin: dict = Depends(auth.require_admin)):
    """Get just the table schema without data (requires admin auth)"""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=403, detail=f"Access to table '{table_name}' is not allowed")

    return {
        "table": table_name,
        "columns": get_table_columns(table_name)
    }


def _encrypt_row_for_write(table_name: str, data: dict) -> dict:
    """Encrypt PII fields for DB explorer writes."""
    from encryption import encrypt_for_admin_required, compute_blind_index, serialize_field_value
    from nostr_keys import normalize_pubkey

    if not data:
        return data

    updated = dict(data)

    if table_name == "users":
        if "pubkey" in updated:
            pubkey_val = updated["pubkey"]
            trimmed_pubkey = str(pubkey_val).strip() if pubkey_val is not None else ""
            if trimmed_pubkey:
                updated["pubkey"] = normalize_pubkey(trimmed_pubkey)
            else:
                updated["pubkey"] = None

        if "email" in updated:
            email_val = updated["email"]
            email_str = str(email_val).strip() if email_val is not None else ""
            if email_str:
                encrypted_email, eph = encrypt_for_admin_required(email_str)
                updated["encrypted_email"] = encrypted_email
                updated["ephemeral_pubkey_email"] = eph
                updated["email_blind_index"] = compute_blind_index(email_str.lower())
                updated["email"] = None
            else:
                updated["email"] = None
                updated["encrypted_email"] = None
                updated["ephemeral_pubkey_email"] = None
                updated["email_blind_index"] = None

        if "encrypted_email" in updated and updated.get("encrypted_email") and not updated.get("ephemeral_pubkey_email"):
            raise ValueError("ephemeral_pubkey_email required when encrypted_email is provided")

        if "encrypted_email" in updated and updated.get("encrypted_email") and updated.get("email_blind_index") is None:
            raise ValueError("email_blind_index required when encrypted_email is provided")

        if "name" in updated:
            name_val = updated["name"]
            name_str = str(name_val).strip() if name_val is not None else ""
            if name_str:
                encrypted_name, eph = encrypt_for_admin_required(name_str)
                updated["encrypted_name"] = encrypted_name
                updated["ephemeral_pubkey_name"] = eph
                updated["name"] = None
            else:
                updated["name"] = None
                updated["encrypted_name"] = None
                updated["ephemeral_pubkey_name"] = None

        if "encrypted_name" in updated and updated.get("encrypted_name") and not updated.get("ephemeral_pubkey_name"):
            raise ValueError("ephemeral_pubkey_name required when encrypted_name is provided")

    elif table_name == "user_field_values":
        if "value" in updated:
            value_val = updated["value"]
            # Serialize value, use strip() only for emptiness check to preserve whitespace
            value_str = serialize_field_value(value_val) if value_val is not None else ""
            if value_str.strip():
                encrypted_value, eph = encrypt_for_admin_required(value_str)
                updated["encrypted_value"] = encrypted_value
                updated["ephemeral_pubkey"] = eph
                updated["value"] = None
            else:
                updated["value"] = None
                updated["encrypted_value"] = None
                updated["ephemeral_pubkey"] = None

        if "encrypted_value" in updated and updated.get("encrypted_value") and not updated.get("ephemeral_pubkey"):
            raise ValueError("ephemeral_pubkey required when encrypted_value is provided")

    return updated


@app.post("/admin/db/query", response_model=DBQueryResponse)
async def execute_db_query(request: DBQueryRequest, admin: dict = Depends(auth.require_admin)):
    """
    Execute a read-only SQL query (requires admin auth).
    Only SELECT statements are allowed for safety.
    """
    sql = request.sql.strip()

    # Security: Only allow SELECT statements
    if not sql.upper().startswith("SELECT"):
        return DBQueryResponse(
            success=False,
            error="Only SELECT queries are allowed. Use the CRUD endpoints for modifications."
        )

    # Security: Block dangerous patterns
    dangerous_patterns = [
        r'\bDROP\b', r'\bDELETE\b', r'\bINSERT\b', r'\bUPDATE\b',
        r'\bALTER\b', r'\bCREATE\b', r'\bTRUNCATE\b', r'\bATTACH\b',
        r'\bDETACH\b', r'\bPRAGMA\b'
    ]
    for pattern in dangerous_patterns:
        if re.search(pattern, sql, re.IGNORECASE):
            return DBQueryResponse(
                success=False,
                error=f"Query contains forbidden keyword"
            )

    allowed, error = validate_sql_allowed_tables(sql)
    if not allowed:
        return DBQueryResponse(
            success=False,
            error=error,
        )

    start_time = time.time()

    try:
        conn = database.get_connection()
        cursor = conn.cursor()
        cursor.execute(sql)

        # Get column names
        columns = [desc[0] for desc in cursor.description] if cursor.description else []

        # Fetch all rows
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()

        execution_time = int((time.time() - start_time) * 1000)

        return DBQueryResponse(
            success=True,
            columns=columns,
            rows=rows,
            executionTimeMs=execution_time
        )
    except Exception as e:
        return DBQueryResponse(
            success=False,
            error=str(e)
        )


@app.post("/admin/db/tables/{table_name}/rows", response_model=RowMutationResponse)
async def insert_db_row(table_name: str, request: RowMutationRequest, admin: dict = Depends(auth.require_admin)):
    """Insert a new row into a table (requires admin auth)"""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=403, detail=f"Access to table '{table_name}' is not allowed")

    return RowMutationResponse(
        success=False,
        error="Direct database mutations are not supported. Use audited admin product flows instead.",
    )


@app.put("/admin/db/tables/{table_name}/rows/{row_id}", response_model=RowMutationResponse)
async def update_db_row(table_name: str, row_id: int, request: RowMutationRequest, admin: dict = Depends(auth.require_admin)):
    """Update an existing row in a table (requires admin auth)"""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=403, detail=f"Access to table '{table_name}' is not allowed")

    return RowMutationResponse(
        success=False,
        error="Direct database mutations are not supported. Use audited admin product flows instead.",
    )


@app.delete("/admin/db/tables/{table_name}/rows/{row_id}", response_model=RowMutationResponse)
async def delete_db_row(table_name: str, row_id: int, admin: dict = Depends(auth.require_admin)):
    """Delete a row from a table (requires admin auth)"""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=403, detail=f"Access to table '{table_name}' is not allowed")

    return RowMutationResponse(
        success=False,
        error="Direct database mutations are not supported. Use audited admin product flows instead.",
    )


@app.get("/admin/database/export")
async def export_database(background_tasks: BackgroundTasks, _admin: Dict = Depends(auth.require_admin)) -> FileResponse:
    """
    Export the SQLite database as a downloadable backup file.
    
    Creates a consistent snapshot of the database using SQLite's backup mechanism
    to avoid locking the live database during export. The backup file is served
    with a timestamped filename for easy organization.
    
    Args:
        _admin (Dict): Admin authentication dependency (unused but required)
        
    Returns:
        FileResponse: Database backup file with timestamped filename
        
    Raises:
        HTTPException: 
            - 404 if database file not found
            - 500 if backup creation or export fails
    """
    try:
        # Get the database file path
        db_path = database.SQLITE_PATH
        
        # Check if database file exists
        if not os.path.exists(db_path):
            raise HTTPException(status_code=404, detail="Database file not found")
        
        # Generate filename with timestamp
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"enclave_backup_{timestamp}.db"
        
        # Create a temporary file for the backup
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        temp_path = temp_file.name
        temp_file.close()
        
        try:
            # Helper to run blocking backup in thread pool
            def perform_backup() -> None:
                source_conn = sqlite3.connect(db_path)
                try:
                    backup_conn = sqlite3.connect(temp_path)
                    try:
                        source_conn.backup(backup_conn)
                    finally:
                        backup_conn.close()
                finally:
                    source_conn.close()

            # Run blocking backup operation in a thread to avoid stalling event loop
            await asyncio.to_thread(perform_backup)

            # Return the backup file as a download with cleanup
            def cleanup_temp_file() -> None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            
            # Schedule cleanup after response is sent using BackgroundTasks
            background_tasks.add_task(cleanup_temp_file)
            _best_effort_config_audit_event(
                table_name="data_deletion",
                config_key="copied_export:sqlite_database",
                old_value=None,
                new_value=json.dumps({
                    "workflow": "copied_export",
                    "target": "sqlite_database",
                    "lifecycle_posture": "outside_active_storage_lifecycle",
                    "filename": filename,
                }, sort_keys=True),
                changed_by=_admin.get("pubkey", "unknown"),
            )
            
            return FileResponse(
                path=temp_path,
                filename=filename,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
            
        except Exception:
            # Clean up temp file on backup failure
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    except HTTPException:
        # Re-raise HTTPExceptions to preserve status codes
        raise
    except Exception as e:
        logger.exception("Database export failed")
        raise HTTPException(status_code=500, detail=f"Export failed: {e!r}") from e
