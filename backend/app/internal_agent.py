"""
Private Enclave Control Plane endpoints for the Sage Agent Runtime.

These endpoints are intentionally not exposed through the public gateway.
They let Sage reuse Enclave Control Plane facts and actions such as auth
hydration, Document Access, User Profile context, Retrieval, and safe admin DB
reads without reimplementing those product rules in Rust.
"""

import os
import re
import sqlite3
import time
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import database
import ingest_db
from admin_db_schema import describe_admin_database
from models import (
    SESSION_LOG_MESSAGE_ID_PATTERN,
    FieldDefinitionCreate,
    FieldDefinitionUpdate,
    InstanceSettings,
)
from query import _build_context, _build_search_query, _process_search_results
from sql_safety import validate_sql_allowed_tables
from store import embed_texts, COLLECTION_NAME, QDRANT_HOST, QDRANT_PORT

logger = logging.getLogger("enclave.internal_agent")

router = APIRouter(prefix="/internal/agent", tags=["internal-agent"])

INTERNAL_AGENT_TOKEN = os.getenv("INTERNAL_AGENT_TOKEN", "").strip()
DOCUMENT_OVERVIEW_OPENING_CHUNKS_PER_DOCUMENT = 2
MAX_OVERVIEW_DOCS = 5
DEFAULT_RESOURCE_SEARCH_LIMIT = 5
MAX_RESOURCE_SEARCH_LIMIT = 25
READ_ONLY_SELECT_FORBIDDEN_KEYWORDS = (
    "ALTER",
    "ATTACH",
    "CREATE",
    "DELETE",
    "DETACH",
    "DROP",
    "INSERT",
    "PRAGMA",
    "REPLACE",
    "TRUNCATE",
    "UPDATE",
    "VACUUM",
)
INSTANCE_SETTINGS_INTERNAL_KEYS = {
    database.ONBOARDING_CONFIGURED_KEYS_SETTING,
}
INSTANCE_SETTINGS_FIELD_ORDER = (
    "instance_name",
    "public_email_display_name",
    "assistant_name",
    "header_tagline",
    "description",
    "primary_color",
    "default_theme",
    "default_language",
    "auto_approve_users",
    "logo_url",
    "favicon_url",
    "apple_touch_icon_url",
    "icon",
    "assistant_icon",
    "user_icon",
    "user_label",
    "header_layout",
    "chat_bubble_style",
    "chat_bubble_shadow",
    "surface_style",
    "status_icon_set",
    "typography_preset",
    "reachout_enabled",
    "reachout_mode",
    "reachout_title",
    "reachout_description",
    "reachout_button_label",
    "reachout_success_message",
    "reachout_to_email",
    "reachout_subject_prefix",
    "reachout_rate_limit_per_hour",
    "reachout_rate_limit_per_day",
    "reachout_include_ip",
)
INSTANCE_SETTINGS_SUPPORTED_VALUES = {
    "auto_approve_users": ["true", "false"],
    "chat_bubble_shadow": ["true", "false"],
    "default_theme": ["light", "dark", "system"],
    "header_layout": ["icon_name", "centered", "compact"],
    "reachout_enabled": ["true", "false"],
    "reachout_include_ip": ["true", "false"],
    "reachout_mode": ["feedback", "help", "support"],
}
INSTANCE_SETTINGS_LABELS = {
    "instance_name": "Instance name",
    "public_email_display_name": "Public email display name",
    "assistant_name": "Assistant name",
    "header_tagline": "Tagline",
    "primary_color": "Primary color",
    "default_theme": "Default theme",
    "default_language": "Default language",
    "auto_approve_users": "Auto-approve users",
}
GUIDED_BOOTSTRAP_SETTING_KEYS = (
    "instance_name",
    "assistant_name",
    "header_tagline",
    "description",
    "primary_color",
    "default_theme",
    "default_language",
    "auto_approve_users",
)


class InternalActorContext(BaseModel):
    id: int
    type: Literal["admin", "user"]
    approved: bool = True
    pubkey: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    user_type_id: Optional[int] = None
    dev_mode: bool = False


class InternalDocumentSearchRequest(BaseModel):
    query: str
    user: InternalActorContext
    top_k: int = 8
    job_ids: Optional[list[str]] = None
    jurisdiction: Optional[str] = None
    situation_details: Optional[str] = None


class InternalDocumentSearchResponse(BaseModel):
    sources: list[dict]
    context: str
    search_query: str
    top_k: int


class InternalResourceSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Optional[str] = None
    kind: Optional[str] = None
    tags: Optional[list[str]] = None
    region: Optional[str] = None
    language: Optional[str] = None
    limit: int = 5
    offset: int = 0


class InternalResourceSearchResponse(BaseModel):
    resources: list[dict]
    query: Optional[str] = None
    resolved_country_code: Optional[str] = None
    total_count: int = 0
    returned_count: int = 0
    limit: int = 0
    offset: int = 0
    has_more: bool = False
    next_offset: Optional[int] = None


class InternalSessionLogMessage(BaseModel):
    message_id: str = Field(pattern=SESSION_LOG_MESSAGE_ID_PATTERN)
    role: Literal["user", "assistant"]
    content: str
    ts: Optional[str] = None


class InternalSessionLogRequest(BaseModel):
    """Record a real user's Conversation for admin review: Sage's complete
    retained user/assistant history, oldest first, and Sage's sequence number
    for the newest message. The transcript is NIP-04 encrypted to the admin
    pubkey at rest — see session_logs.record_user_conversation."""
    actor: InternalActorContext
    messages: list[InternalSessionLogMessage] = Field(min_length=1)
    through_sequence_id: int
    sage_session_id: str = Field(min_length=1, max_length=200)
    title: Optional[str] = None


class InternalSessionLogResponse(BaseModel):
    log_id: str
    status: str
    turn_count: int


class InternalAdminDbQueryRequest(BaseModel):
    sql: str


class InternalAdminConfigToolRequest(BaseModel):
    actor: InternalActorContext


class InternalUpdateInstanceSettingsRequest(InternalAdminConfigToolRequest):
    conversation_id: str = Field(min_length=1, max_length=255)
    settings: InstanceSettings


class InternalConfigureUserType(BaseModel):
    reference: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    icon: str | None = Field(default=None, max_length=80)
    display_order: int = 0


class InternalConfigureOnboardingQuestion(BaseModel):
    field_name: str = Field(min_length=1, max_length=120)
    field_type: str = Field(min_length=1, max_length=40)
    required: bool = False
    display_order: int = 0
    user_type_reference: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    placeholder: str | None = Field(default=None, max_length=240)
    options: list[str] | None = Field(default=None, max_length=100)
    encryption_enabled: bool = True
    include_in_chat: bool = False


class InternalConfigureInstanceRequest(InternalAdminConfigToolRequest):
    conversation_id: str = Field(min_length=1, max_length=255)
    settings: InstanceSettings
    user_types: list[InternalConfigureUserType] = Field(default_factory=list, max_length=5)
    onboarding_questions: list[InternalConfigureOnboardingQuestion] = Field(
        default_factory=list,
        max_length=10,
    )
    behavior_rules: list[str] = Field(default_factory=list, max_length=8)
    forbidden_topics: list[str] = Field(default_factory=list, max_length=8)


class InternalUpdateDeploymentSettingsRequest(InternalAdminConfigToolRequest):
    conversation_id: str = Field(min_length=1, max_length=255)
    settings: dict[str, str] = Field(min_length=1)


class InternalReadDeploymentSecretRequest(InternalAdminConfigToolRequest):
    conversation_id: str = Field(min_length=1, max_length=255)
    key: str = Field(min_length=1, max_length=255)


class InternalUpdateAgentSettingsRequest(InternalAdminConfigToolRequest):
    conversation_id: str = Field(min_length=1, max_length=255)
    user_type_id: int | None = None
    updates: dict[str, str] = Field(default_factory=dict)
    revert_keys: list[str] = Field(default_factory=list)


class InternalManageUserTypesRequest(InternalAdminConfigToolRequest):
    conversation_id: str = Field(min_length=1, max_length=255)
    operation: Literal["create", "update", "delete"]
    user_type_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    icon: str | None = Field(default=None, max_length=80)
    display_order: int | None = None


class InternalManageOnboardingQuestionsRequest(InternalAdminConfigToolRequest):
    conversation_id: str = Field(min_length=1, max_length=255)
    operation: Literal["create", "update", "delete"]
    question_id: int | None = None
    field_name: str | None = Field(default=None, min_length=1, max_length=120)
    field_type: str | None = Field(default=None, min_length=1, max_length=40)
    required: bool | None = None
    display_order: int | None = None
    user_type_id: int | None = None
    placeholder: str | None = Field(default=None, max_length=240)
    options: list[str] | None = Field(default=None, max_length=100)
    encryption_enabled: bool | None = None
    include_in_chat: bool | None = None


class InternalDocumentAccessUpdate(BaseModel):
    job_id: str = Field(min_length=1, max_length=255)
    is_available: bool | None = None
    is_default_active: bool | None = None
    display_order: int | None = None


class InternalUpdateDocumentAccessRequest(InternalAdminConfigToolRequest):
    conversation_id: str = Field(min_length=1, max_length=255)
    user_type_id: int | None = None
    updates: list[InternalDocumentAccessUpdate] = Field(default_factory=list)
    revert_job_ids: list[str] = Field(default_factory=list)


class InternalAdminConfigSecretPolicy(BaseModel):
    mode: Literal["masked", "explicit_secret"] = "masked"


class InternalAdminConfigToolResponse(BaseModel):
    version: int = 1
    tool: str
    data: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    generated_at: str
    secret_policy: InternalAdminConfigSecretPolicy = Field(
        default_factory=InternalAdminConfigSecretPolicy
    )


class InternalUserRecordResponse(BaseModel):
    id: int
    type: Literal["user"] = "user"
    approved: bool = True
    email: Optional[str] = None
    name: Optional[str] = None
    user_type_id: Optional[int] = None
    dev_mode: bool = False


class InternalAdminRecordResponse(BaseModel):
    id: int
    type: Literal["admin"] = "admin"
    pubkey: str
    session_nonce: int = 0


class InternalUserTypeResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    display_order: int = 0
    created_at: Optional[str] = None


def _require_internal_token(x_internal_agent_token: Optional[str] = Header(None)) -> None:
    if not INTERNAL_AGENT_TOKEN:
        raise HTTPException(status_code=503, detail="Internal agent token not configured")
    if not x_internal_agent_token or x_internal_agent_token != INTERNAL_AGENT_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid internal agent token")


def _build_accessible_job_ids(user: InternalActorContext, requested_job_ids: Optional[list[str]]) -> list[str]:
    if user.type == "admin":
        available_job_ids = set(database.get_available_documents())
        if requested_job_ids:
            return [job_id for job_id in requested_job_ids if job_id in available_job_ids]
        return list(available_job_ids)

    available_job_ids = set(database.get_available_documents_for_user_type(user.user_type_id))
    if requested_job_ids:
        return [job_id for job_id in requested_job_ids if job_id in available_job_ids]
    return list(available_job_ids)


def _filter_results_to_accessible_jobs(search_results: list[dict], accessible_job_ids: list[str]) -> list[dict]:
    allowed = set(accessible_job_ids)
    return [
        result
        for result in search_results
        if result.get("payload", {}).get("job_id") in allowed
    ]


def _is_document_overview_query(query: str) -> bool:
    normalized = query.lower()
    material_terms = (
        "uploaded",
        "document",
        "doc",
        "pdf",
        "book",
        "file",
        "resource",
    )
    overview_terms = (
        "read",
        "learn about",
        "overview",
        "basic understanding",
        "understanding",
        "summarize",
        "summary",
        "my org",
        "our org",
        "organization",
    )
    return any(term in normalized for term in material_terms) and any(
        term in normalized for term in overview_terms
    )


def _opening_chunk_texts_for_documents(job_ids: list[str], seen_chunk_ids: set[str]) -> list[str]:
    opening_texts: list[str] = []
    for job_id in job_ids:
        rows = ingest_db.list_retrieval_chunks(
            job_id,
            limit=DOCUMENT_OVERVIEW_OPENING_CHUNKS_PER_DOCUMENT,
        )
        for row in rows:
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id or chunk_id in seen_chunk_ids:
                continue
            chunk = ingest_db.get_retrieval_chunk(chunk_id)
            text = (chunk or {}).get("text")
            if text:
                opening_texts.append(str(text))
                seen_chunk_ids.add(chunk_id)
    return opening_texts


def _execute_safe_select(sql: str) -> dict:
    normalized = sql.strip()
    if not re.match(r"^SELECT\b", normalized, re.IGNORECASE):
        return {
            "success": False,
            "columns": [],
            "rows": [],
            "executionTimeMs": 0,
            "error": "Only SELECT queries are allowed. Use the CRUD endpoints for modifications.",
        }
    if ";" in normalized:
        return {
            "success": False,
            "columns": [],
            "rows": [],
            "executionTimeMs": 0,
            "error": "Only a single SELECT statement is allowed.",
        }

    for keyword in READ_ONLY_SELECT_FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", normalized, re.IGNORECASE):
            return {
                "success": False,
                "columns": [],
                "rows": [],
                "executionTimeMs": 0,
                "error": "Query contains forbidden keyword",
            }

    is_allowed, error = validate_sql_allowed_tables(normalized)
    if not is_allowed:
        return {
            "success": False,
            "columns": [],
            "rows": [],
            "executionTimeMs": 0,
            "error": error,
        }

    start_time = time.time()
    try:
        conn = database.get_connection()
        cursor = conn.cursor()
        cursor.execute(normalized)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return {
            "success": True,
            "columns": columns,
            "rows": rows,
            "executionTimeMs": int((time.time() - start_time) * 1000),
            "error": None,
        }
    except Exception as exc:
        return {
            "success": False,
            "columns": [],
            "rows": [],
            "executionTimeMs": int((time.time() - start_time) * 1000),
            "error": str(exc),
        }


def _require_admin_actor(actor: InternalActorContext) -> None:
    if actor.type != "admin" or not actor.approved or not actor.pubkey:
        raise HTTPException(status_code=403, detail="Admin Config Tools require an approved admin actor")
    admin = database.get_admin_by_pubkey(actor.pubkey)
    if not admin or int(admin["id"]) != actor.id:
        raise HTTPException(status_code=403, detail="Admin Config Tools require an approved admin actor")


def _humanize_config_key(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _instance_settings_tool_data() -> dict[str, Any]:
    settings = {
        key: value
        for key, value in database.get_all_settings().items()
        if key not in INSTANCE_SETTINGS_INTERNAL_KEYS
    }
    explicitly_set_keys = sorted(
        key
        for key in database.get_onboarding_configured_keys()
        if key in settings
    )
    explicit_key_set = set(explicitly_set_keys)
    ordered_keys = [
        key for key in INSTANCE_SETTINGS_FIELD_ORDER if key in settings
    ] + sorted(
        key for key in settings if key not in INSTANCE_SETTINGS_FIELD_ORDER
    )
    fields = []
    for key in ordered_keys:
        field: dict[str, Any] = {
            "key": key,
            "label": INSTANCE_SETTINGS_LABELS.get(key, _humanize_config_key(key)),
            "value": settings.get(key),
            "source": "operator" if key in explicit_key_set else "default",
            "editable": True,
        }
        supported_values = INSTANCE_SETTINGS_SUPPORTED_VALUES.get(key)
        if supported_values:
            field["supported_values"] = supported_values
        fields.append(field)

    return {
        "settings": settings,
        "explicitly_set_keys": explicitly_set_keys,
        "fields": fields,
    }


def _deployment_settings_tool_data() -> dict[str, Any]:
    settings: dict[str, dict[str, Any]] = {}
    categories: dict[str, list[str]] = {}

    for row in database.get_all_deployment_config():
        key = str(row["key"])
        category = str(row.get("category") or "general")
        is_secret = bool(row.get("is_secret"))
        value = row.get("value")
        configured = bool(str(value or "").strip())
        if is_secret and configured:
            value = "********"
        settings[key] = {
            "value": value,
            "configured": configured,
            "secret": is_secret,
            "requires_restart": bool(row.get("requires_restart")),
            "source": "deployment_config",
            "category": category,
            "description": row.get("description"),
            "updated_at": row.get("updated_at"),
        }
        categories.setdefault(category, []).append(key)

    return {
        "settings": settings,
        "categories": {
            category: sorted(keys)
            for category, keys in sorted(categories.items())
        },
    }


def _ai_config_item_data(row: dict[str, Any]) -> dict[str, Any]:
    data = {
        "key": row.get("key"),
        "value": row.get("value"),
        "value_type": row.get("value_type"),
        "category": row.get("category"),
        "description": row.get("description"),
        "updated_at": row.get("updated_at"),
    }
    if "is_override" in row:
        data["is_override"] = bool(row.get("is_override"))
        data["override_user_type_id"] = row.get("override_user_type_id")
    return data


def _group_ai_config_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped = {
        "prompt_sections": {},
        "parameters": {},
        "defaults": {},
    }
    category_map = {
        "prompt_section": "prompt_sections",
        "parameter": "parameters",
        "default": "defaults",
    }
    for row in rows:
        group_key = category_map.get(str(row.get("category") or ""))
        key = row.get("key")
        if not group_key or not key:
            continue
        grouped[group_key][str(key)] = _ai_config_item_data(row)
    return grouped


def _agent_settings_tool_data() -> dict[str, Any]:
    user_types = database.list_user_types()
    global_rows = database.get_effective_ai_config(None)
    per_user_type = []
    for user_type in user_types:
        user_type_id = int(user_type["id"])
        effective_rows = database.get_effective_ai_config(user_type_id)
        overrides = {
            str(row["key"]): _ai_config_item_data(row)
            for row in effective_rows
            if row.get("is_override")
        }
        per_user_type.append({
            "user_type_id": user_type_id,
            "user_type_name": user_type.get("name"),
            "overrides": overrides,
            "effective_values": _group_ai_config_rows(effective_rows),
        })

    return {
        "global": _group_ai_config_rows(global_rows),
        "per_user_type": per_user_type,
        "limits": {
            "user_types_returned": len(per_user_type),
        },
    }


def _field_label(field_name: str) -> str:
    return field_name.replace("_", " ").capitalize()


def _onboarding_field_data(field: dict[str, Any]) -> dict[str, Any]:
    field_name = str(field.get("field_name") or "")
    return {
        "id": field.get("id"),
        "user_type_id": field.get("user_type_id"),
        "name": field_name,
        "label": _field_label(field_name),
        "field_type": field.get("field_type"),
        "required": bool(field.get("required")),
        "display_order": field.get("display_order"),
        "placeholder": field.get("placeholder"),
        "options": field.get("options"),
        "encryption_enabled": bool(field.get("encryption_enabled", True)),
        "include_in_chat": bool(field.get("include_in_chat", False)),
        "created_at": field.get("created_at"),
    }


def _user_types_tool_data() -> dict[str, Any]:
    user_types = database.list_user_types()
    user_type_items = []
    onboarding_questions = []
    for user_type in user_types:
        user_type_id = int(user_type["id"])
        fields = [
            _onboarding_field_data(field)
            for field in database.get_field_definitions(
                user_type_id=user_type_id,
                include_global=True,
            )
        ]
        onboarding_questions.extend(fields)
        user_type_items.append({
            "id": user_type_id,
            "name": user_type.get("name"),
            "description": user_type.get("description"),
            "icon": user_type.get("icon"),
            "display_order": int(user_type.get("display_order", 0) or 0),
            "created_at": user_type.get("created_at"),
            "onboarding_fields": fields,
        })

    return {
        "user_types": user_type_items,
        "onboarding_questions": onboarding_questions,
        "limits": {
            "user_types_returned": len(user_type_items),
            "onboarding_questions_returned": len(onboarding_questions),
        },
    }


def _document_access_item_data(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": document.get("job_id"),
        "filename": document.get("filename"),
        "status": document.get("status"),
        "total_chunks": int(document.get("total_chunks") or 0),
        "is_available": bool(document.get("is_available")),
        "is_default_active": bool(document.get("is_default_active")),
        "display_order": int(document.get("display_order") or 0),
        "updated_at": document.get("updated_at"),
        "is_override": bool(document.get("is_override", False)),
        "override_user_type_id": document.get("override_user_type_id"),
        "override_updated_at": document.get("override_updated_at"),
    }


def _document_access_tool_data() -> dict[str, Any]:
    global_documents = [
        _document_access_item_data(document)
        for document in database.get_effective_document_defaults(None)
    ]
    per_user_type = []
    for user_type in database.list_user_types():
        user_type_id = int(user_type["id"])
        effective_documents = [
            _document_access_item_data(document)
            for document in database.get_effective_document_defaults(user_type_id)
        ]
        per_user_type.append({
            "user_type_id": user_type_id,
            "user_type_name": user_type.get("name"),
            "available_document_ids": database.get_available_documents_for_user_type(user_type_id),
            "default_document_ids": database.get_active_documents_for_user_type(user_type_id),
            "documents": effective_documents,
        })

    return {
        "global": {
            "available_document_ids": database.get_available_documents_for_user_type(None),
            "default_document_ids": database.get_active_documents_for_user_type(None),
            "documents": global_documents,
        },
        "documents": global_documents,
        "per_user_type": per_user_type,
        "limits": {
            "documents_returned": len(global_documents),
            "user_types_returned": len(per_user_type),
        },
    }


def _onboarding_status_tool_data() -> dict[str, Any]:
    admins = database.list_admins()
    configured_key_set = {
        key
        for key in database.get_onboarding_configured_keys()
        if key in GUIDED_BOOTSTRAP_SETTING_KEYS
    }
    configured_keys = [
        key for key in GUIDED_BOOTSTRAP_SETTING_KEYS
        if key in configured_key_set
    ]
    missing_required_keys = [
        key for key in GUIDED_BOOTSTRAP_SETTING_KEYS
        if key not in configured_key_set
    ]
    user_type_data = _user_types_tool_data()
    user_types = user_type_data["user_types"]
    # The 9th guided question: the operator must define at least one non-admin
    # user type (e.g. Teacher, Student). Created live via the POST /admin/user-types
    # change-set path, so "done" simply means one or more user types now exist.
    user_types_complete = len(user_types) >= 1

    return {
        "instance": {
            "admin_exists": database.has_admin(),
            "admin_initialized": database.get_instance_state("admin_initialized") == "true",
            "setup_complete": database.get_instance_state("setup_complete") == "true",
            "ready_for_users": database.is_instance_setup_complete(),
            "admin_count": len(admins),
        },
        "guided_bootstrap": {
            "required_keys": list(GUIDED_BOOTSTRAP_SETTING_KEYS),
            "configured_keys": configured_keys,
            "missing_required_keys": missing_required_keys,
            "complete": not missing_required_keys,
            "required_count": len(GUIDED_BOOTSTRAP_SETTING_KEYS),
            "configured_required_count": len(configured_keys),
        },
        "user_types_setup": {
            "required_minimum": 1,
            "count": len(user_types),
            "names": [ut.get("name") for ut in user_types],
            "complete": user_types_complete,
        },
        "user_types": user_types,
        "onboarding_questions": user_type_data["onboarding_questions"],
        "limits": user_type_data["limits"],
    }


def _deployment_readiness_tool_data() -> tuple[dict[str, Any], list[str]]:
    from deployment_config import (
        _backup_restore_readiness_item,
        _deployment_validation_readiness_items,
        _inference_readiness_item,
        _lifecycle_readiness_item,
        _restart_readiness_item,
    )

    warnings: list[str] = []
    items = [
        *_deployment_validation_readiness_items(),
        _inference_readiness_item(),
    ]
    try:
        from lifecycle import get_lifecycle_status

        items.append(_lifecycle_readiness_item(get_lifecycle_status()))
    except Exception as exc:  # pragma: no cover - exact dependency failure varies by environment
        logger.warning("admin config deployment readiness skipped lifecycle read: %s", exc)
        warnings.append("lifecycle_readiness_unavailable")

    items.extend([
        _backup_restore_readiness_item(),
        _restart_readiness_item(),
    ])
    blockers = sum(1 for item in items if item["severity"] == "blocker")
    warnings_count = sum(1 for item in items if item["severity"] == "warning")
    ready = sum(1 for item in items if item["severity"] == "ready")
    return (
        {
            "status": "blocked" if blockers else ("warnings" if warnings_count else "ready"),
            "summary": {
                "blockers": blockers,
                "warnings": warnings_count,
                "ready": ready,
                "total": len(items),
            },
            "items": items,
        },
        warnings,
    )


@router.get("/users/{user_id}", response_model=InternalUserRecordResponse, dependencies=[Depends(_require_internal_token)])
async def get_user_record(user_id: int) -> InternalUserRecordResponse:
    user = database.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"User not found: {user_id}")

    return InternalUserRecordResponse(
        id=int(user["id"]),
        approved=bool(user.get("approved", True)),
        email=user.get("email"),
        name=user.get("name"),
        user_type_id=user.get("user_type_id"),
        dev_mode=bool(user.get("dev_mode", False)),
    )


@router.get("/admins/by-pubkey/{pubkey}", response_model=InternalAdminRecordResponse, dependencies=[Depends(_require_internal_token)])
async def get_admin_record(pubkey: str) -> InternalAdminRecordResponse:
    admin = database.get_admin_by_pubkey(pubkey)
    if not admin:
        raise HTTPException(status_code=404, detail=f"Admin not found: {pubkey}")

    return InternalAdminRecordResponse(
        id=int(admin["id"]),
        pubkey=admin["pubkey"],
        session_nonce=int(admin.get("session_nonce", 0) or 0),
    )


@router.get("/user-types/{user_type_id}", response_model=InternalUserTypeResponse, dependencies=[Depends(_require_internal_token)])
async def get_user_type_record(user_type_id: int) -> InternalUserTypeResponse:
    user_type = database.get_user_type(user_type_id)
    if not user_type:
        raise HTTPException(status_code=404, detail=f"User type not found: {user_type_id}")

    return InternalUserTypeResponse(
        id=int(user_type["id"]),
        name=user_type["name"],
        description=user_type.get("description"),
        icon=user_type.get("icon"),
        display_order=int(user_type.get("display_order", 0) or 0),
        created_at=user_type.get("created_at"),
    )


@router.post("/document-search", response_model=InternalDocumentSearchResponse, dependencies=[Depends(_require_internal_token)])
async def document_search(payload: InternalDocumentSearchRequest) -> InternalDocumentSearchResponse:
    session_stub = {
        "jurisdiction": payload.jurisdiction,
        "situation_details": payload.situation_details,
    }
    search_query = _build_search_query(payload.query, session_stub)

    search_filter = None
    accessible_job_ids = _build_accessible_job_ids(payload.user, payload.job_ids)

    if not accessible_job_ids:
        return InternalDocumentSearchResponse(
            sources=[],
            context="",
            search_query=search_query,
            top_k=payload.top_k,
        )

    query_embedding = embed_texts([f"query: {search_query}"])[0]

    search_filter = {
        "should": [{"key": "job_id", "match": {"value": job_id}} for job_id in accessible_job_ids]
    }

    qdrant_url = f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{COLLECTION_NAME}/points/search"
    search_payload = {
        "vector": query_embedding,
        "limit": payload.top_k,
        "with_payload": True,
    }
    if search_filter:
        search_payload["filter"] = search_filter

    response = httpx.post(qdrant_url, json=search_payload, timeout=30.0)
    response.raise_for_status()
    search_results = response.json().get("result", [])
    search_results = _filter_results_to_accessible_jobs(search_results, accessible_job_ids)
    sources, _, chunk_texts = _process_search_results(search_results)
    if _is_document_overview_query(payload.query):
        seen_chunk_ids = {str(source.get("chunk_id") or "") for source in sources}
        overview_job_ids = sorted(accessible_job_ids)[:MAX_OVERVIEW_DOCS]
        opening_chunk_texts = _opening_chunk_texts_for_documents(
            overview_job_ids,
            seen_chunk_ids,
        )
        chunk_texts = opening_chunk_texts + chunk_texts
    context = _build_context(chunk_texts, sources)
    return InternalDocumentSearchResponse(
        sources=sources,
        context=context,
        search_query=search_query,
        top_k=payload.top_k,
    )


@router.post(
    "/resources/search",
    response_model=InternalResourceSearchResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def resources_search(payload: InternalResourceSearchRequest) -> InternalResourceSearchResponse:
    """Generic faceted lookup of ready Admin-curated resources."""
    effective_limit = max(
        0,
        min(
            payload.limit if payload.limit is not None else DEFAULT_RESOURCE_SEARCH_LIMIT,
            MAX_RESOURCE_SEARCH_LIMIT,
        ),
    )
    effective_offset = max(0, int(payload.offset or 0))
    normalized_query = " ".join((payload.query or "").casefold().split()) or None
    resolved = database.normalize_jurisdiction(payload.region)
    if str(payload.region or "").strip() and not resolved:
        raise HTTPException(status_code=422, detail="Unknown resource region")
    resources = database.search_resources(
        region=payload.region,
        language=payload.language,
        limit=effective_limit,
        offset=effective_offset,
        query=payload.query,
        kind=payload.kind,
        tags=payload.tags,
        return_metadata=True,
    )
    page = resources
    return InternalResourceSearchResponse(
        resources=page["resources"],
        query=normalized_query,
        resolved_country_code=resolved,
        total_count=page["total_count"],
        returned_count=page["returned_count"],
        limit=effective_limit,
        offset=effective_offset,
        has_more=page["has_more"],
        next_offset=page["next_offset"],
    )


@router.post(
    "/session-logs",
    response_model=InternalSessionLogResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def log_user_session(payload: InternalSessionLogRequest) -> InternalSessionLogResponse:
    """Record a real (non-admin) User Conversation for admin review.

    Sage calls this after each exchange, and again to retry a failed update,
    with the Conversation's complete retained history. Repeated or out-of-order
    updates never shrink the record — see session_logs.record_user_conversation.
    The transcript is NIP-04 encrypted to the admin pubkey; fails closed (409) if
    no admin is configured, so nothing is stored in plaintext.
    """
    import session_logs
    import impersonation

    if payload.actor.type != "user":
        raise HTTPException(
            status_code=403,
            detail="User session logs require a user actor",
        )

    # Admin "Test as User" sessions are captured explicitly as a Test User
    # Session (source="test") elsewhere. Skip the ambient user-conversation log
    # only when the persisted user has the instance-derived test-user pubkey, so
    # an ordinary user with a reserved-looking email remains logged. See #494.
    with database.dedicated_connection():
        with database.get_cursor() as cursor:
            cursor.execute("SELECT user_type_id FROM users WHERE id = ?", (payload.actor.id,))
            user = cursor.fetchone()
        user_exists = user is not None
        is_test_user = user_exists and impersonation.is_provisioned_test_user(payload.actor.id)
    if not user_exists:
        # Sage may retry an update after the User was deleted; there is no one
        # left to record it for.
        logger.info("Skipping session log for missing user %s", payload.actor.id)
        return InternalSessionLogResponse(log_id="", status="skipped", turn_count=0)
    if is_test_user:
        logger.info(
            "Skipping ambient session log for provisioned test user %s",
            payload.actor.id,
        )
        return InternalSessionLogResponse(log_id="", status="skipped", turn_count=0)

    try:
        with database.dedicated_connection():
            saved = session_logs.record_user_conversation(
                sage_session_id=payload.sage_session_id,
                subject_user_id=payload.actor.id,
                messages=[message.model_dump() for message in payload.messages],
                through_sequence_id=payload.through_sequence_id,
                user_type_id=user["user_type_id"],
                title=payload.title,
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return InternalSessionLogResponse(
        log_id=saved["log_id"],
        status=saved["status"],
        turn_count=saved["turn_count"],
    )


@router.get("/document-access", dependencies=[Depends(_require_internal_token)])
async def document_access(user_type_id: Optional[int] = None):
    return {
        "user_type_id": user_type_id,
        "available_document_ids": database.get_available_documents_for_user_type(user_type_id),
        "default_document_ids": database.get_active_documents_for_user_type(user_type_id),
    }


@router.get("/user-profile-context/{user_id}", dependencies=[Depends(_require_internal_token)])
async def user_profile_context(user_id: int, user_type_id: Optional[int] = None):
    return {
        "user_id": user_id,
        "user_type_id": user_type_id,
        "profile": database.get_user_chat_context_values(user_id=user_id, user_type_id=user_type_id),
    }


@router.get("/admin-db-schema", dependencies=[Depends(_require_internal_token)])
async def admin_db_schema(table: Optional[str] = None):
    try:
        return describe_admin_database(table)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin-db-query", dependencies=[Depends(_require_internal_token)])
async def admin_db_query(request: InternalAdminDbQueryRequest):
    return _execute_safe_select(request.sql)


@router.post(
    "/admin-config/instance-settings",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_instance_settings(
    payload: InternalAdminConfigToolRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)

    return InternalAdminConfigToolResponse(
        tool="read_instance_settings",
        data=_instance_settings_tool_data(),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/update-instance-settings",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_update_instance_settings(
    payload: InternalUpdateInstanceSettingsRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)
    conversation_id = payload.conversation_id.strip()
    if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")

    settings = payload.settings.model_dump(exclude_unset=True)
    if not settings:
        raise HTTPException(status_code=422, detail="At least one Instance Setting is required")

    result = database.update_settings_with_audit(
        settings,
        changed_by=payload.actor.pubkey or str(payload.actor.id),
        action_source="sage_conversation",
        conversation_id=conversation_id,
    )
    return InternalAdminConfigToolResponse(
        tool="update_instance_settings",
        data={
            "outcome": "succeeded",
            "validation": {"status": "valid"},
            "saved_values": result["saved_values"],
            "changed_names": result["changed_names"],
            "affected_areas": ["instance_settings"],
        },
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/configure-instance",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_configure_instance(
    payload: InternalConfigureInstanceRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)
    conversation_id = payload.conversation_id.strip()
    if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")

    required_settings = {
        "instance_name",
        "assistant_name",
        "header_tagline",
        "description",
        "primary_color",
        "default_theme",
        "default_language",
        "auto_approve_users",
    }
    missing_settings = sorted(required_settings - payload.settings.model_fields_set)
    if missing_settings:
        raise HTTPException(
            status_code=422,
            detail=f"Guided setup requires Instance Settings: {', '.join(missing_settings)}",
        )
    settings = payload.settings.model_dump(exclude_unset=True)
    for key in ("instance_name", "assistant_name", "header_tagline", "description"):
        value = settings.get(key)
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(status_code=422, detail=f"{key} must not be blank")
        settings[key] = value.strip()
    primary_color = str(settings.get("primary_color") or "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", primary_color):
        raise HTTPException(status_code=422, detail="primary_color must be a #RRGGBB hex color")
    settings["primary_color"] = primary_color

    user_types = [item.model_dump() for item in payload.user_types]
    references = [item["reference"] for item in user_types]
    if len(references) != len(set(references)):
        raise HTTPException(status_code=422, detail="User Type references must be unique")
    names = [str(item["name"]).strip() for item in user_types]
    if any(not name for name in names):
        raise HTTPException(status_code=422, detail="User Type names must not be blank")
    if len({name.casefold() for name in names}) != len(names):
        raise HTTPException(status_code=422, detail="User Type names must be unique")
    for item, name in zip(user_types, names):
        item["name"] = name

    questions: list[dict[str, object]] = []
    known_references = set(references)
    for item in payload.onboarding_questions:
        raw = item.model_dump()
        reference = raw.pop("user_type_reference")
        if reference is not None and reference not in known_references:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown User Type reference: {reference}",
            )
        try:
            validated = FieldDefinitionCreate(**raw).model_dump()
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if validated["encryption_enabled"] and validated["include_in_chat"]:
            raise HTTPException(
                status_code=422,
                detail="Encrypted Onboarding Questions cannot be included in chat context",
            )
        validated["user_type_reference"] = reference
        questions.append(validated)

    def _normalize_rules(values: list[str], label: str) -> list[str]:
        normalized = [str(value).strip() for value in values]
        if any(not value for value in normalized):
            raise HTTPException(status_code=422, detail=f"{label} cannot contain blank values")
        return normalized

    behavior_rules = _normalize_rules(payload.behavior_rules, "behavior_rules")
    forbidden_topics = _normalize_rules(payload.forbidden_topics, "forbidden_topics")

    try:
        result = database.configure_instance_with_audit(
            settings=settings,
            user_types=user_types,
            onboarding_questions=questions,
            behavior_rules=behavior_rules,
            forbidden_topics=forbidden_topics,
            changed_by=payload.actor.pubkey or str(payload.actor.id),
            action_source="sage_conversation",
            conversation_id=conversation_id,
        )
    except sqlite3.IntegrityError as exc:
        detail = "Guided setup contains a duplicate User Type or Onboarding Question"
        raise HTTPException(status_code=422, detail=detail) from exc
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return InternalAdminConfigToolResponse(
        tool="configure_instance",
        data={
            "outcome": "succeeded",
            "validation": {"status": "valid"},
            "saved_setup": result["saved_setup"],
            "changed_names": result["changed_names"],
            "affected_areas": [
                "instance_settings",
                "agent_settings",
                "user_types",
                "onboarding_questions",
            ],
        },
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/deployment-settings",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_deployment_settings(
    payload: InternalAdminConfigToolRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)

    return InternalAdminConfigToolResponse(
        tool="read_deployment_settings",
        data=_deployment_settings_tool_data(),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/update-deployment-settings",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_update_deployment_settings(
    payload: InternalUpdateDeploymentSettingsRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)
    conversation_id = payload.conversation_id.strip()
    if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")

    from deployment_config import (
        ENV_CONFIG_MAP,
        validate_and_normalize_deployment_config_value,
    )

    validated: dict[str, dict[str, object]] = {}
    for key, value in payload.settings.items():
        try:
            normalized = validate_and_normalize_deployment_config_value(key, value)
        except HTTPException as exc:
            raise HTTPException(status_code=422, detail=exc.detail) from exc
        meta = ENV_CONFIG_MAP[key]
        validated[key] = {
            "value": normalized,
            "is_secret": bool(meta.get("is_secret")),
            "requires_restart": bool(meta.get("requires_restart")),
            "category": str(meta.get("category") or "general"),
            "description": str(meta.get("description") or ""),
        }

    smtp_keys = {"SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"}
    if smtp_keys.intersection(validated):
        meta = ENV_CONFIG_MAP["SMTP_LAST_TEST_SUCCESS"]
        validated["SMTP_LAST_TEST_SUCCESS"] = {
            "value": "false",
            "is_secret": False,
            "requires_restart": bool(meta.get("requires_restart")),
            "category": str(meta.get("category") or "general"),
            "description": str(meta.get("description") or ""),
        }

    result = database.update_deployment_configs_with_audit(
        validated,
        changed_by=payload.actor.pubkey or str(payload.actor.id),
        action_source="sage_conversation",
        conversation_id=conversation_id,
    )
    warnings: list[str] = []
    try:
        from config_loader import invalidate_cache

        invalidate_cache()
    except ImportError as exc:
        logger.debug("config_loader not available, skipping cache invalidation: %s", exc)
        warnings.append(
            "Deployment settings were saved, but runtime config cache invalidation is unavailable."
        )
    except Exception:
        logger.exception(
            "Deployment settings were saved, but config cache invalidation failed"
        )
        warnings.append(
            "Deployment settings were saved, but runtime config cache invalidation failed; "
            "runtime values may remain stale."
        )
    restart_required_keys = sorted(
        key
        for key in result["changed_names"]
        if bool(validated[key].get("requires_restart"))
    )
    return InternalAdminConfigToolResponse(
        tool="update_deployment_settings",
        data={
            "outcome": "succeeded",
            "validation": {"status": "valid"},
            "saved_values": result["saved_values"],
            "changed_names": result["changed_names"],
            "affected_areas": ["deployment_settings"],
            "restart_required": bool(restart_required_keys),
            "restart_required_keys": restart_required_keys,
        },
        warnings=warnings,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/read-deployment-secret",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_read_deployment_secret(
    payload: InternalReadDeploymentSecretRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)
    from deployment_config import ENV_CONFIG_MAP, FORBIDDEN_KEYS

    conversation_id = payload.conversation_id.strip()
    if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")
    key = payload.key.strip()
    meta = ENV_CONFIG_MAP.get(key)
    if key in FORBIDDEN_KEYS or not meta or not meta.get("is_secret"):
        raise HTTPException(status_code=422, detail="Requested Deployment Setting is not a readable secret")
    value = database.get_deployment_config_value(key)
    if value is None:
        raise HTTPException(status_code=404, detail="Deployment secret is not configured")
    database.log_config_audit_event(
        table_name="deployment_config_secret_reads",
        config_key=key,
        old_value=None,
        new_value='{"outcome":"succeeded","secret":"redacted"}',
        changed_by=payload.actor.pubkey or str(payload.actor.id),
        action_source="sage_conversation",
        conversation_id=conversation_id,
    )
    return InternalAdminConfigToolResponse(
        tool="read_deployment_secret",
        data={"key": key, "value": value},
        generated_at=datetime.now(timezone.utc).isoformat(),
        secret_policy=InternalAdminConfigSecretPolicy(mode="explicit_secret"),
    )


@router.post(
    "/admin-config/agent-settings",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_agent_settings(
    payload: InternalAdminConfigToolRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)

    return InternalAdminConfigToolResponse(
        tool="read_agent_settings",
        data=_agent_settings_tool_data(),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/update-agent-settings",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_update_agent_settings(
    payload: InternalUpdateAgentSettingsRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)
    conversation_id = payload.conversation_id.strip()
    if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")
    if not payload.updates and not payload.revert_keys:
        raise HTTPException(status_code=422, detail="At least one Agent Setting operation is required")
    if payload.user_type_id is None and payload.revert_keys:
        raise HTTPException(status_code=422, detail="Only User Type overrides can be reverted")
    if set(payload.updates).intersection(payload.revert_keys):
        raise HTTPException(status_code=422, detail="A setting cannot be updated and reverted together")
    if payload.user_type_id is not None and not database.get_user_type(payload.user_type_id):
        raise HTTPException(status_code=422, detail="User Type not found")

    from ai_config import validate_ai_config_value

    validated: dict[str, str] = {}
    for key, value in payload.updates.items():
        config = database.get_ai_config(key)
        if not config:
            raise HTTPException(status_code=422, detail=f"Unknown Agent Setting: {key}")
        try:
            validated[key] = validate_ai_config_value(
                key,
                value,
                str(config["value_type"]),
                f"direct Admin Config value for {key}",
            )
        except HTTPException as exc:
            raise HTTPException(status_code=422, detail=exc.detail) from exc

    for key in payload.revert_keys:
        if not database.get_ai_config(key):
            raise HTTPException(status_code=422, detail=f"Unknown Agent Setting: {key}")

    result = database.update_agent_settings_with_audit(
        validated,
        revert_keys=payload.revert_keys,
        user_type_id=payload.user_type_id,
        changed_by=payload.actor.pubkey or str(payload.actor.id),
        action_source="sage_conversation",
        conversation_id=conversation_id,
    )
    rows = (
        database.get_effective_ai_config(payload.user_type_id)
        if payload.user_type_id is not None
        else database.get_all_ai_config()
    )
    values = {str(row["key"]): str(row["value"]) for row in rows}
    requested_keys = sorted(set(validated).union(payload.revert_keys))
    return InternalAdminConfigToolResponse(
        tool="update_agent_settings",
        data={
            "outcome": "succeeded",
            "validation": {"status": "valid"},
            "saved_values": {key: values[key] for key in requested_keys},
            "changed_names": result["changed_names"],
            "reverted_keys": result["reverted_keys"],
            "user_type_id": payload.user_type_id,
            "affected_areas": ["agent_settings"],
        },
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/user-types",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_user_types(
    payload: InternalAdminConfigToolRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)

    return InternalAdminConfigToolResponse(
        tool="read_user_types",
        data=_user_types_tool_data(),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/manage-user-types",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_manage_user_types(
    payload: InternalManageUserTypesRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)
    conversation_id = payload.conversation_id.strip()
    if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")
    if payload.operation == "create" and payload.name is None:
        raise HTTPException(status_code=422, detail="name is required to create a User Type")
    if payload.operation in {"update", "delete"} and payload.user_type_id is None:
        raise HTTPException(status_code=422, detail="user_type_id is required")

    excluded = {"actor", "conversation_id", "operation", "user_type_id"}
    values = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if key not in excluded
    }
    if "name" in values:
        if values["name"] is None:
            raise HTTPException(status_code=422, detail="name must not be null")
        normalized_name = str(values["name"]).strip()
        if not normalized_name:
            raise HTTPException(status_code=422, detail="name must not be blank")
        values["name"] = normalized_name
    try:
        result = database.manage_user_type_with_audit(
            payload.operation,
            user_type_id=payload.user_type_id,
            values=values,
            changed_by=payload.actor.pubkey or str(payload.actor.id),
            action_source="sage_conversation",
            conversation_id=conversation_id,
        )
    except (sqlite3.IntegrityError, LookupError, ValueError) as exc:
        detail = "User Type name already exists" if "UNIQUE constraint" in str(exc) else str(exc)
        raise HTTPException(status_code=422, detail=detail) from exc

    return InternalAdminConfigToolResponse(
        tool="manage_user_types",
        data={
            "outcome": "succeeded",
            "validation": {"status": "valid"},
            "operation": payload.operation,
            "user_type": result["user_type"],
            "deleted_user_type_id": result["deleted_user_type_id"],
            "changed_names": result["changed_names"],
            "affected_areas": result["affected_areas"],
        },
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/manage-onboarding-questions",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_manage_onboarding_questions(
    payload: InternalManageOnboardingQuestionsRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)
    conversation_id = payload.conversation_id.strip()
    if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")
    if payload.operation in {"update", "delete"} and payload.question_id is None:
        raise HTTPException(status_code=422, detail="question_id is required")

    excluded = {"actor", "conversation_id", "operation", "question_id"}
    raw_values = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if key not in excluded
    }
    try:
        if payload.operation == "create":
            values = FieldDefinitionCreate(**raw_values).model_dump()
            existing = None
        elif payload.operation == "update":
            if not raw_values:
                raise ValueError("At least one Onboarding Question field is required")
            values = FieldDefinitionUpdate(**raw_values).model_dump(exclude_unset=True)
            existing = database.get_field_definition_by_id(payload.question_id or 0)
            if not existing:
                raise LookupError("Onboarding Question not found")
        else:
            values = {}
            existing = database.get_field_definition_by_id(payload.question_id or 0)
            if not existing:
                raise LookupError("Onboarding Question not found")
    except (ValidationError, ValueError, LookupError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    target_user_type = values.get(
        "user_type_id",
        existing.get("user_type_id") if existing else None,
    )
    if target_user_type is not None and not database.get_user_type(int(target_user_type)):
        raise HTTPException(status_code=422, detail="User Type not found")

    effective_encryption = values.get(
        "encryption_enabled",
        bool(existing.get("encryption_enabled")) if existing else True,
    )
    effective_include = values.get(
        "include_in_chat",
        bool(existing.get("include_in_chat")) if existing else False,
    )
    if effective_encryption and effective_include:
        raise HTTPException(
            status_code=422,
            detail="Encrypted Onboarding Questions cannot be included in chat context",
        )

    try:
        result = database.manage_onboarding_question_with_audit(
            payload.operation,
            question_id=payload.question_id,
            values=values,
            changed_by=payload.actor.pubkey or str(payload.actor.id),
            action_source="sage_conversation",
            conversation_id=conversation_id,
        )
    except (sqlite3.IntegrityError, LookupError, ValueError) as exc:
        detail = "Question name already exists for this User Type" if "UNIQUE constraint" in str(exc) else str(exc)
        raise HTTPException(status_code=422, detail=detail) from exc

    return InternalAdminConfigToolResponse(
        tool="manage_onboarding_questions",
        data={
            "outcome": "succeeded",
            "validation": {"status": "valid"},
            "operation": payload.operation,
            "onboarding_question": result["onboarding_question"],
            "deleted_question_id": result["deleted_question_id"],
            "changed_names": result["changed_names"],
            "affected_areas": result["affected_areas"],
        },
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/document-access",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_document_access(
    payload: InternalAdminConfigToolRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)

    return InternalAdminConfigToolResponse(
        tool="read_document_access",
        data=_document_access_tool_data(),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/update-document-access",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_update_document_access(
    payload: InternalUpdateDocumentAccessRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)
    conversation_id = payload.conversation_id.strip()
    if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")
    if not payload.updates and not payload.revert_job_ids:
        raise HTTPException(status_code=422, detail="At least one Document Access operation is required")
    if payload.user_type_id is not None and not database.get_user_type(payload.user_type_id):
        raise HTTPException(status_code=422, detail="User Type not found")

    updates: dict[str, dict[str, object]] = {}
    for item in payload.updates:
        job_id = item.job_id.strip()
        if job_id in updates:
            raise HTTPException(
                status_code=422,
                detail=f"Document appears more than once in updates: {job_id}",
            )
        if not ingest_db.get_job(job_id):
            raise HTTPException(status_code=422, detail=f"Document not found: {job_id}")
        values = item.model_dump(exclude={"job_id"}, exclude_none=True)
        if not values:
            raise HTTPException(status_code=422, detail=f"No access values supplied for {job_id}")
        if payload.user_type_id is not None and "display_order" in values:
            raise HTTPException(status_code=422, detail="display_order is global Document Access configuration")
        updates[job_id] = values

    revert_job_ids = [job_id.strip() for job_id in payload.revert_job_ids]
    if set(updates).intersection(revert_job_ids):
        raise HTTPException(status_code=422, detail="A Document cannot be updated and reverted together")
    for job_id in revert_job_ids:
        if not ingest_db.get_job(job_id):
            raise HTTPException(status_code=422, detail=f"Document not found: {job_id}")

    result = database.update_document_access_with_audit(
        updates,
        revert_job_ids=revert_job_ids,
        user_type_id=payload.user_type_id,
        changed_by=payload.actor.pubkey or str(payload.actor.id),
        action_source="sage_conversation",
        conversation_id=conversation_id,
    )
    effective = {
        str(item["job_id"]): item
        for item in database.get_effective_document_defaults(payload.user_type_id)
    }
    requested_job_ids = sorted(set(updates).union(revert_job_ids))
    return InternalAdminConfigToolResponse(
        tool="update_document_access",
        data={
            "outcome": "succeeded",
            "validation": {"status": "valid"},
            "saved_values": {
                job_id: effective.get(job_id)
                for job_id in requested_job_ids
            },
            "changed_names": result["changed_names"],
            "reverted_job_ids": result["reverted_job_ids"],
            "user_type_id": payload.user_type_id,
            "affected_areas": ["document_access"],
        },
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/onboarding-status",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_onboarding_status(
    payload: InternalAdminConfigToolRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)

    return InternalAdminConfigToolResponse(
        tool="read_onboarding_status",
        data=_onboarding_status_tool_data(),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/admin-config/deployment-readiness",
    response_model=InternalAdminConfigToolResponse,
    dependencies=[Depends(_require_internal_token)],
)
async def admin_config_deployment_readiness(
    payload: InternalAdminConfigToolRequest,
) -> InternalAdminConfigToolResponse:
    _require_admin_actor(payload.actor)
    data, warnings = _deployment_readiness_tool_data()

    return InternalAdminConfigToolResponse(
        tool="read_deployment_readiness",
        data=data,
        warnings=warnings,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/health", dependencies=[Depends(_require_internal_token)])
async def internal_agent_health() -> dict[str, str]:
    return {"status": "healthy"}
