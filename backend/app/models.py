"""
Enclave Pydantic Models
Request and response models for user/admin management.
"""

from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Any, Optional, Union
from datetime import datetime


SUPPORTED_USER_FIELD_TYPES = {
    "text",
    "textarea",
    "number",
    "boolean",
    "email",
    "url",
    "select",
    "multi_select",
    "date",
}

SUPPORTED_DEFAULT_LANGUAGES = {
    "en",
    "es",
    "pt",
    "fr",
    "de",
    "it",
    "nl",
    "ru",
    "zh-Hans",
    "zh-Hant",
    "ja",
    "ko",
    "ar",
    "fa",
    "hi",
    "bn",
    "id",
    "th",
    "vi",
    "tr",
    "pl",
    "uk",
    "sv",
    "no",
    "da",
    "fi",
    "el",
    "he",
    "cs",
    "ro",
    "hu",
}

SUPPORTED_DEFAULT_THEMES = {"light", "dark", "system"}


class KnowledgeSourceScope(str, Enum):
    NONE = "none"
    SELECTED = "selected"
    ALL = "all"


def _validate_email_shape(value: str) -> str:
    normalized = value.strip()
    if len(normalized) > 254 or normalized.count("@") != 1:
        raise ValueError("email must be a valid email address")
    local, _, domain = normalized.partition("@")
    if not local or not domain or "." not in domain or any(char.isspace() for char in normalized):
        raise ValueError("email must be a valid email address")
    return normalized


def normalize_user_field_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_USER_FIELD_TYPES:
        raise ValueError("field_type is not supported")
    return normalized


def normalize_user_field_options(value: Optional[list[str]]) -> Optional[list[str]]:
    if value is None:
        return value
    normalized = []
    for option in value:
        clean = str(option).strip()
        if not clean or len(clean) > 200:
            raise ValueError("options must be non-empty strings no longer than 200 characters")
        normalized.append(clean)
    return normalized


def normalize_default_language(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    normalized = value.strip()
    if normalized not in SUPPORTED_DEFAULT_LANGUAGES:
        allowed = ", ".join(sorted(SUPPORTED_DEFAULT_LANGUAGES))
        raise ValueError(f"default_language must be one of: {allowed}")
    return normalized


def normalize_default_theme(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_DEFAULT_THEMES:
        allowed = ", ".join(sorted(SUPPORTED_DEFAULT_THEMES))
        raise ValueError(f"default_theme must be one of: {allowed}")
    return normalized


# --- Admin Models ---

class AdminAuth(BaseModel):
    """Legacy request model for admin authentication (deprecated)"""
    pubkey: str


class AdminResponse(BaseModel):
    """Response model for admin data"""
    id: int
    pubkey: str
    created_at: Optional[str] = None


class AdminListResponse(BaseModel):
    """Response model for list of admins"""
    admins: list[AdminResponse]


# --- Nostr Auth Models ---

class NostrEvent(BaseModel):
    """A signed Nostr event (NIP-01)"""
    id: str
    pubkey: str
    created_at: int
    kind: int
    tags: list[list[str]]
    content: str
    sig: str


class AdminAuthRequest(BaseModel):
    """Request model for admin authentication with signed Nostr event"""
    event: NostrEvent


class AdminAuthResponse(BaseModel):
    """Response model for successful admin authentication"""
    admin: AdminResponse
    is_new: bool
    instance_initialized: bool
    session_token: str


# --- Instance Settings Models ---

class InstanceSettings(BaseModel):
    """Instance settings model"""
    instance_name: Optional[str] = None
    primary_color: Optional[str] = None
    description: Optional[str] = None
    public_email_display_name: Optional[str] = None
    logo_url: Optional[str] = None
    favicon_url: Optional[str] = None
    apple_touch_icon_url: Optional[str] = None
    icon: Optional[str] = None
    assistant_icon: Optional[str] = None
    user_icon: Optional[str] = None
    assistant_name: Optional[str] = None
    user_label: Optional[str] = None
    header_layout: Optional[str] = None
    header_tagline: Optional[str] = None
    chat_bubble_style: Optional[str] = None
    # Stored in SQLite as text ("true"/"false"), but accept boolean payloads from the UI/assistant.
    chat_bubble_shadow: Optional[bool] = None
    surface_style: Optional[str] = None
    status_icon_set: Optional[str] = None
    typography_preset: Optional[str] = None
    default_language: Optional[str] = None
    default_theme: Optional[str] = None
    auto_approve_users: Optional[Union[str, bool]] = None
    reachout_enabled: Optional[Union[str, bool]] = None
    reachout_mode: Optional[str] = None
    reachout_title: Optional[str] = None
    reachout_description: Optional[str] = None
    reachout_button_label: Optional[str] = None
    reachout_success_message: Optional[str] = None
    reachout_to_email: Optional[str] = None
    reachout_subject_prefix: Optional[str] = None
    reachout_rate_limit_per_hour: Optional[str] = None
    reachout_rate_limit_per_day: Optional[str] = None
    reachout_include_ip: Optional[Union[str, bool]] = None

    @field_validator("default_language")
    @classmethod
    def validate_default_language(cls, value: Optional[str]) -> Optional[str]:
        return normalize_default_language(value)

    @field_validator("default_theme")
    @classmethod
    def validate_default_theme(cls, value: Optional[str]) -> Optional[str]:
        return normalize_default_theme(value)

    model_config = ConfigDict(extra="forbid")


class InstanceSettingsResponse(BaseModel):
    """Response model for instance settings"""
    settings: dict


class InstanceStatusResponse(BaseModel):
    """Response model for instance status and setup state"""
    initialized: bool  # Whether an admin has been configured
    setup_complete: bool = False  # Whether admin has completed setup/auth
    ready_for_users: bool = False  # Whether users can register/login
    settings: dict = Field(default_factory=dict)  # Public instance settings
    protected_inference: dict = Field(default_factory=dict)


# --- User Type Models ---

class UserTypeCreate(BaseModel):
    """Request model for creating a user type"""
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)
    icon: Optional[str] = Field(default=None, max_length=80)
    display_order: int = 0


class UserTypeUpdate(BaseModel):
    """Request model for updating a user type"""
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    display_order: Optional[int] = None


class UserTypeResponse(BaseModel):
    """Response model for user type data"""
    id: int
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    display_order: int
    created_at: Optional[str] = None


class UserTypeListResponse(BaseModel):
    """Response model for list of user types"""
    types: list[UserTypeResponse]


# --- User Field Definition Models ---

class FieldDefinitionCreate(BaseModel):
    """Request model for creating a user field definition"""
    field_name: str = Field(..., min_length=1, max_length=120)
    field_type: str = Field(..., min_length=1, max_length=40)  # 'text', 'number', 'boolean', 'email', 'url', 'select', etc.
    required: bool = False
    display_order: int = 0
    user_type_id: Optional[int] = None  # None = global field (shown for all types)
    placeholder: Optional[str] = Field(default=None, max_length=240)  # Placeholder text for input
    options: Optional[list[str]] = Field(default=None, max_length=100)  # Options for select fields
    encryption_enabled: bool = True  # Secure default: encrypt field values
    include_in_chat: bool = False  # Include field value in AI chat context (only for unencrypted fields)

    @field_validator("field_type")
    @classmethod
    def validate_field_type(cls, value: str) -> str:
        return normalize_user_field_type(value)

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return normalize_user_field_options(value)


class FieldDefinitionUpdate(BaseModel):
    """Request model for updating a field definition"""
    field_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    field_type: Optional[str] = Field(default=None, min_length=1, max_length=40)
    required: Optional[bool] = None
    display_order: Optional[int] = None
    user_type_id: Optional[int] = None
    placeholder: Optional[str] = Field(default=None, max_length=240)
    options: Optional[list[str]] = Field(default=None, max_length=100)
    encryption_enabled: Optional[bool] = None  # Toggle encryption for field
    include_in_chat: Optional[bool] = None  # Toggle AI chat context inclusion

    @field_validator("field_type")
    @classmethod
    def validate_field_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return normalize_user_field_type(value)

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return normalize_user_field_options(value)


class FieldDefinitionResponse(BaseModel):
    """Response model for field definition"""
    id: int
    field_name: str
    field_type: str
    required: bool
    display_order: int
    user_type_id: Optional[int] = None  # None = global field
    placeholder: Optional[str] = None
    options: Optional[list[str]] = None
    encryption_enabled: bool = True  # Whether field values are encrypted
    include_in_chat: bool = False  # Whether field value is included in AI chat context
    created_at: Optional[str] = None


class FieldDefinitionListResponse(BaseModel):
    """Response model for list of field definitions"""
    fields: list[FieldDefinitionResponse]


class FieldEncryptionRequest(BaseModel):
    """Request model for updating field encryption setting"""
    encryption_enabled: bool
    force: bool = False  # Override warnings about existing data


class FieldEncryptionResponse(BaseModel):
    """Response model for field encryption update"""
    field_id: int
    encryption_enabled: bool
    warning: Optional[str] = None
    migrated_values: Optional[int] = None  # Number of values migrated


# --- Encrypted Data Models ---

class EncryptedField(BaseModel):
    """Encrypted field data for NIP-04 decryption"""
    ciphertext: str  # NIP-04 format: base64(encrypted)?iv=base64(iv)
    ephemeral_pubkey: str  # x-only pubkey (hex) for ECDH


# --- User Models ---

class UserCreate(BaseModel):
    """Request model for creating a user"""
    pubkey: Optional[str] = None
    email: Optional[str] = None      # Auth email (encrypted, enables email lookups)
    name: Optional[str] = None       # User's name (encrypted)
    user_type_id: Optional[int] = None  # Which user type they selected
    fields: dict = {}  # Dynamic fields defined by admin


class UserUpdate(BaseModel):
    """Request model for updating a user"""
    pubkey: Optional[str] = None
    approved: Optional[bool] = None
    fields: dict = {}


class UserResponse(BaseModel):
    """Response model for user data.

    Encrypted fields are returned in *_encrypted properties.
    Plaintext fields (email, name) are only populated for legacy unencrypted data.
    """
    id: int
    pubkey: Optional[str] = None
    email: Optional[str] = None  # Plaintext (legacy only)
    name: Optional[str] = None   # Plaintext (legacy only)
    email_encrypted: Optional[EncryptedField] = None  # NIP-04 encrypted
    name_encrypted: Optional[EncryptedField] = None   # NIP-04 encrypted
    user_type_id: Optional[int] = None
    user_type: Optional[UserTypeResponse] = None  # Nested type info
    approved: bool = True
    created_at: Optional[str] = None
    fields: dict = {}  # Plaintext (legacy only)
    fields_encrypted: dict = {}  # NIP-04 encrypted field values


class UserListResponse(BaseModel):
    """Response model for list of users"""
    users: list[UserResponse]


class UserRosterExportAuditRequest(BaseModel):
    """Audit metadata for a browser-generated User Roster Export."""
    filename: str = Field(..., min_length=1, max_length=240)
    user_count: int = Field(..., ge=0)
    pending_count: int = Field(..., ge=0)
    includes_decrypted_browser_values: bool = False


# --- Magic Link Auth Models ---

class MagicLinkRequest(BaseModel):
    """Request model for sending a magic link"""
    email: str = Field(..., min_length=3, max_length=254)
    name: str = Field(default="", max_length=200)
    # Locale is intentionally permissive at the request boundary. An absent,
    # unknown, or malformed value falls back to English rather than turning a
    # sign-in request into a validation error.
    locale: Any = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return _validate_email_shape(value)


class MagicLinkResponse(BaseModel):
    """Response model for magic link request"""
    success: bool
    message: str


class VerifyTokenRequest(BaseModel):
    """Request model for verifying a magic link token"""
    token: str


class AuthUserResponse(BaseModel):
    """Response model for authenticated user"""
    id: int
    email: str
    name: Optional[str] = None
    user_type_id: Optional[int] = None
    approved: bool = True
    created_at: Optional[str] = None
    needs_onboarding: bool = False
    needs_user_type: bool = False


class VerifyTokenResponse(BaseModel):
    """Response model for successful verification"""
    success: bool
    user: AuthUserResponse
    session_token: str


class SessionUserResponse(BaseModel):
    """Response model for /auth/me endpoint"""
    user: Optional[AuthUserResponse] = None
    authenticated: bool


# --- Onboarding Status Models ---

class OnboardingStatusResponse(BaseModel):
    """Canonical onboarding completeness state for the authenticated user."""
    user_id: int
    user_type_id: Optional[int] = None
    effective_user_type_id: Optional[int] = None
    needs_user_type: bool = False
    needs_onboarding: bool = False
    total_fields: int = 0
    required_fields: int = 0
    completed_required_fields: int = 0
    missing_required_fields: list[FieldDefinitionResponse] = []
    missing_optional_fields: list[FieldDefinitionResponse] = []


# --- User Type Migration Models ---

class UserTypeMigrationRequest(BaseModel):
    """Request model for migrating a user to a new user type."""
    target_user_type_id: int
    allow_incomplete: bool = True
    reason: Optional[str] = None


class UserTypeMigrationResponse(BaseModel):
    """Response model for single-user type migration."""
    success: bool
    user_id: int
    previous_user_type_id: Optional[int] = None
    target_user_type_id: int
    missing_required_count: int = 0
    missing_required_fields: list[str] = Field(default_factory=list)


class UserTypeMigrationBatchRequest(BaseModel):
    """Request model for bulk user-type migration."""
    user_ids: list[int] = Field(default_factory=list)
    target_user_type_id: int
    allow_incomplete: bool = True
    reason: Optional[str] = None


class UserTypeMigrationBatchResult(BaseModel):
    """Per-user result for bulk migration operations."""
    user_id: int
    success: bool
    previous_user_type_id: Optional[int] = None
    target_user_type_id: Optional[int] = None
    missing_required_count: int = 0
    missing_required_fields: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class UserTypeMigrationBatchResponse(BaseModel):
    """Response model for bulk user-type migration."""
    success: bool
    migrated: int = 0
    failed: int = 0
    results: list[UserTypeMigrationBatchResult] = Field(default_factory=list)


# --- Generic Response Models ---

class SuccessResponse(BaseModel):
    """Generic success response"""
    success: bool
    message: str


class ErrorResponse(BaseModel):
    """Generic error response"""
    error: str
    detail: Optional[str] = None


# --- Database Explorer Models ---

class ColumnInfo(BaseModel):
    """Column definition for a table"""
    name: str
    type: str  # SQLite types: TEXT, INTEGER, REAL, BLOB, NULL
    nullable: bool
    primaryKey: bool
    defaultValue: Optional[str] = None


class TableInfo(BaseModel):
    """Table metadata"""
    name: str
    columns: list[ColumnInfo]
    rowCount: int


class TablesListResponse(BaseModel):
    """Response model for list of tables"""
    tables: list[TableInfo]


class TableDataResponse(BaseModel):
    """Response model for table data (paginated)"""
    table: str
    columns: list[ColumnInfo]
    rows: list[dict]
    totalRows: int
    page: int
    pageSize: int
    totalPages: int


class DBQueryRequest(BaseModel):
    """Request model for SQL query execution"""
    sql: str = Field(..., min_length=1, max_length=10000)


class DBQueryResponse(BaseModel):
    """Response model for SQL query execution"""
    success: bool
    columns: list[str] = []
    rows: list[dict] = []
    rowsAffected: Optional[int] = None
    lastInsertId: Optional[int] = None
    error: Optional[str] = None
    executionTimeMs: Optional[int] = None


class RowMutationRequest(BaseModel):
    """Request model for inserting/updating a row"""
    data: dict


class RowMutationResponse(BaseModel):
    """Response model for row mutations"""
    success: bool
    id: Optional[int] = None
    error: Optional[str] = None


# --- Agent Settings Models ---

class AIConfigItem(BaseModel):
    """Single Agent Settings item"""
    key: str
    value: str
    value_type: str  # 'string', 'number', 'boolean', 'json'
    category: str  # 'prompt_section', 'parameter', 'default'
    description: Optional[str] = None
    updated_at: Optional[str] = None


class AIConfigResponse(BaseModel):
    """Response model for Agent Settings grouped by category"""
    prompt_sections: list[AIConfigItem] = []
    parameters: list[AIConfigItem] = []
    defaults: list[AIConfigItem] = []


class AIConfigUpdate(BaseModel):
    """Request model for updating an Agent Settings value"""
    value: str


class PromptPreviewRequest(BaseModel):
    """Request model for previewing assembled prompt"""
    sample_question: str = "What should I know about this topic?"
    sample_facts: dict = {}


class PromptPreviewResponse(BaseModel):
    """Response model for prompt preview"""
    assembled_prompt: str
    sections_used: list[str]


class SessionDefaultsResponse(BaseModel):
    """Response model for public session defaults"""
    model_config = ConfigDict(use_enum_values=True, validate_default=True)

    web_search_enabled: bool = False
    default_document_ids: list[str] = []
    default_tool_ids: list[str] = []
    knowledge_source_scope: KnowledgeSourceScope = KnowledgeSourceScope.NONE


# --- Agent Settings User-Type Override Models ---

class AIConfigWithInheritance(BaseModel):
    """Agent Settings item with inheritance information"""
    key: str
    value: str
    value_type: str  # 'string', 'number', 'boolean', 'json'
    category: str  # 'prompt_section', 'parameter', 'default'
    description: Optional[str] = None
    updated_at: Optional[str] = None
    is_override: bool = False
    override_user_type_id: Optional[int] = None


class AIConfigOverrideItem(BaseModel):
    """Single Agent Settings override for a user type"""
    key: str
    value: str
    user_type_id: int
    updated_at: Optional[str] = None


class AIConfigUserTypeResponse(BaseModel):
    """Response model for Agent Settings with user-type inheritance"""
    user_type_id: int
    user_type_name: Optional[str] = None
    prompt_sections: list[AIConfigWithInheritance] = []
    parameters: list[AIConfigWithInheritance] = []
    defaults: list[AIConfigWithInheritance] = []


class AIConfigOverrideUpdate(BaseModel):
    """Request model for updating an Agent Settings override"""
    value: str


# --- Document Defaults Models ---

class DocumentDefaultItem(BaseModel):
    """Single document default item"""
    job_id: str
    filename: Optional[str] = None
    status: Optional[str] = None
    total_chunks: Optional[int] = None
    is_available: bool = True
    is_default_active: bool = True
    display_order: int = 0
    updated_at: Optional[str] = None


class DocumentDefaultsResponse(BaseModel):
    """Response model for list of document defaults"""
    documents: list[DocumentDefaultItem]


class DocumentDefaultUpdate(BaseModel):
    """Request model for updating document defaults"""
    is_available: Optional[bool] = None
    is_default_active: Optional[bool] = None
    display_order: Optional[int] = None


class DocumentDefaultBatchItem(BaseModel):
    """Single item in a batch update"""
    job_id: str
    is_available: Optional[bool] = None
    is_default_active: Optional[bool] = None
    display_order: Optional[int] = None


class DocumentDefaultsBatchUpdate(BaseModel):
    """Request model for batch updating document defaults"""
    updates: list[DocumentDefaultBatchItem]


# --- Document Defaults User-Type Override Models ---

class DocumentDefaultWithInheritance(BaseModel):
    """Document default item with inheritance information"""
    job_id: str
    filename: Optional[str] = None
    status: Optional[str] = None
    total_chunks: Optional[int] = None
    is_available: bool = True
    is_default_active: bool = True
    display_order: int = 0
    updated_at: Optional[str] = None
    is_override: bool = False
    override_user_type_id: Optional[int] = None
    override_updated_at: Optional[str] = None


class DocumentDefaultsUserTypeResponse(BaseModel):
    """Response model for document defaults with user-type inheritance"""
    user_type_id: int
    user_type_name: Optional[str] = None
    documents: list[DocumentDefaultWithInheritance]


class DocumentDefaultOverrideUpdate(BaseModel):
    """Request model for updating document defaults override"""
    is_available: Optional[bool] = None
    is_default_active: Optional[bool] = None


# --- Deployment Configuration Models ---

class DeploymentConfigItem(BaseModel):
    """Single deployment config item"""
    key: str
    value: Optional[str] = None
    is_secret: bool = False
    requires_restart: bool = False
    category: str
    description: Optional[str] = None
    updated_at: Optional[str] = None


class DeploymentConfigResponse(BaseModel):
    """Response model for deployment config grouped by category"""
    llm: list[DeploymentConfigItem] = []
    embedding: list[DeploymentConfigItem] = []
    email: list[DeploymentConfigItem] = []
    storage: list[DeploymentConfigItem] = []
    security: list[DeploymentConfigItem] = []
    search: list[DeploymentConfigItem] = []
    domains: list[DeploymentConfigItem] = []
    ssl: list[DeploymentConfigItem] = []
    general: list[DeploymentConfigItem] = []


class DeploymentConfigUpdate(BaseModel):
    """Request model for updating a deployment config value"""
    value: str


class ServiceHealthItem(BaseModel):
    """Health status for a single service"""
    name: str
    status: str  # 'healthy', 'unhealthy', 'unknown'
    response_time_ms: Optional[int] = None
    last_checked: Optional[str] = None
    error: Optional[str] = None


class ServiceHealthResponse(BaseModel):
    """Response model for service health"""
    services: list[ServiceHealthItem]
    restart_required: bool = False
    changed_keys_requiring_restart: list[str] = []
    runtime_env: dict = Field(default_factory=dict)


class DeploymentValidationResponse(BaseModel):
    """Response model for config validation"""
    valid: bool
    errors: list[str] = []
    warnings: list[str] = []


# --- Config Audit Log Models ---

class ConfigAuditLogEntry(BaseModel):
    """Single audit log entry"""
    id: int
    table_name: str
    config_key: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    changed_by: str
    action_source: str
    conversation_id: Optional[str] = None
    changed_at: str


class ConfigAuditLogResponse(BaseModel):
    """Response model for audit log"""
    entries: list[ConfigAuditLogEntry]


# --- Test Email Models ---

class TestEmailRequest(BaseModel):
    """Request model for sending a test email"""
    email: str


class TestEmailResponse(BaseModel):
    """Response model for test email result"""
    success: bool
    message: str
    error: Optional[str] = None


# --- Reachout Models ---

class ReachoutRequest(BaseModel):
    """Authenticated user reachout request"""
    message: str = Field(..., min_length=1, max_length=5000)


class ReachoutResponse(BaseModel):
    """Response model for reachout submissions"""
    success: bool
    message: str


# --- Public Configuration Models ---

class PublicConfigResponse(BaseModel):
    """Response model for public unauthenticated configuration settings."""
    pass


# --- Resource Referral Directory Models ---

RESOURCE_SCOPE_LEVELS = {"country", "subregion", "region", "global"}
RESOURCE_KINDS = {"person", "organization", "product", "service", "method", "reference", "other"}
RESOURCE_POINTER_TYPES = {"email", "phone", "url", "address", "secure_channel", "identifier", "other"}


class ResourcePointer(BaseModel):
    type: str
    value: str = Field(..., min_length=1, max_length=2000)
    label: Optional[str] = Field(default=None, max_length=240)

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in RESOURCE_POINTER_TYPES:
            raise ValueError(f"pointer type must be one of: {', '.join(sorted(RESOURCE_POINTER_TYPES))}")
        return normalized

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("pointer value must not be blank")
        return normalized


class ResourceRegion(BaseModel):
    level: str
    code: Optional[str] = Field(default=None, max_length=12)

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in RESOURCE_SCOPE_LEVELS:
            raise ValueError(f"region level must be one of: {', '.join(sorted(RESOURCE_SCOPE_LEVELS))}")
        return normalized


class ResourceProvenance(BaseModel):
    verified_at: Optional[str] = None
    vetted_by: Optional[str] = Field(default=None, max_length=240)
    source_note: Optional[str] = Field(default=None, max_length=1000)


def normalize_resource_languages(value: Optional[list[str]]) -> Optional[list[str]]:
    if value is None:
        return value
    normalized = []
    for lang in value:
        clean = str(lang).strip()
        if clean and clean not in SUPPORTED_DEFAULT_LANGUAGES:
            allowed = ", ".join(sorted(SUPPORTED_DEFAULT_LANGUAGES))
            raise ValueError(f"language '{clean}' is not supported; must be one of: {allowed}")
        if clean:
            normalized.append(clean)
    return normalized


def normalize_resource_tags(value: Optional[list[str]]) -> Optional[list[str]]:
    if value is None:
        return None
    normalized: list[str] = []
    for tag in value:
        clean = str(tag).strip().casefold()
        if not clean:
            continue
        if len(clean) > 120:
            raise ValueError("resource tags must be 120 characters or fewer")
        if clean not in normalized:
            normalized.append(clean)
    return normalized


class ResourceCreate(BaseModel):
    """Request model for creating a directory resource.

    A draft becomes searchable only when it has a generic kind, useful
    description, and at least one nonblank pointer.
    """
    model_config = ConfigDict(extra="forbid")

    resource_id: Optional[str] = Field(default=None, max_length=120)
    name: str = Field(..., min_length=1, max_length=240)
    kind: Optional[str] = None
    tags: Optional[list[str]] = Field(default=None, max_length=80)
    pointers: Optional[list[ResourcePointer]] = Field(default=None, max_length=80)
    regions: Optional[list[ResourceRegion]] = Field(default=None, max_length=80)
    provenance: Optional[ResourceProvenance] = None
    description: Optional[str] = Field(default=None, max_length=4000)
    languages: Optional[list[str]] = Field(default=None, max_length=40)
    verified: Optional[bool] = None
    display_order: int = 0
    archived: bool = False

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if normalized not in RESOURCE_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(RESOURCE_KINDS))}")
        return normalized

    @model_validator(mode="after")
    def _require_resource_id_or_name(self):
        resource_id = str(self.resource_id or "").strip()
        self.resource_id = resource_id or None
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("name must not be blank")
        return self

    @field_validator("languages")
    @classmethod
    def _validate_languages(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return normalize_resource_languages(v)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return normalize_resource_tags(value)

class ResourceUpdate(BaseModel):
    """Request model for updating a directory resource (all fields optional)."""
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=240)
    kind: Optional[str] = None
    tags: Optional[list[str]] = Field(default=None, max_length=80)
    pointers: Optional[list[ResourcePointer]] = Field(default=None, max_length=80)
    regions: Optional[list[ResourceRegion]] = Field(default=None, max_length=80)
    provenance: Optional[ResourceProvenance] = None
    description: Optional[str] = Field(default=None, max_length=4000)
    languages: Optional[list[str]] = Field(default=None, max_length=40)
    verified: Optional[bool] = None
    display_order: Optional[int] = None
    archived: Optional[bool] = None

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if normalized not in RESOURCE_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(RESOURCE_KINDS))}")
        return normalized

    @field_validator("languages")
    @classmethod
    def _validate_languages(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return normalize_resource_languages(v)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return normalize_resource_tags(value)

class ResourceResponse(BaseModel):
    """Response model for a directory resource, including computed lifecycle state."""
    resource_id: str
    name: Optional[str] = None
    kind: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    pointers: list[ResourcePointer] = Field(default_factory=list)
    regions: list[ResourceRegion] = Field(default_factory=list)
    provenance: ResourceProvenance = Field(default_factory=ResourceProvenance)
    description: Optional[str] = None
    languages: list[str] = Field(default_factory=list)
    status: str
    missing_fields: list[str] = Field(default_factory=list)
    display_order: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ResourceListResponse(BaseModel):
    """Response model for a list of directory resources."""
    resources: list[ResourceResponse]


# --- Session Logs (Test & Feedback) ---

class SessionLogTraceReasoning(BaseModel):
    """Reasoning summary attached to a saved assistant turn."""
    model_config = ConfigDict(extra="allow")

    summary: Optional[str] = None


class SessionLogTraceTool(BaseModel):
    """Tool trace metadata attached to a saved assistant turn."""
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    execution: Optional[str] = None
    input_summary: Optional[str] = None
    output_summary: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionLogTraceRetrieval(BaseModel):
    """Retrieval trace metadata attached to a saved assistant turn."""
    model_config = ConfigDict(extra="allow")

    source_type: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    score: Optional[float] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionLogTraceDelta(BaseModel):
    """Streamed trace delta retained in a saved assistant turn."""
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    kind: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    tool_name: Optional[str] = None
    status: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class SessionLogTraceActivityStep(BaseModel):
    """Activity step retained in a saved assistant turn."""
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    kind: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    summary: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class SessionLogConversationTrace(BaseModel):
    """Conversation Trace metadata retained in a saved assistant turn."""
    model_config = ConfigDict(extra="allow")

    visibility: Optional[str] = Field(default=None, max_length=32)
    reasoning: Optional[SessionLogTraceReasoning] = None
    tools: list[SessionLogTraceTool] = Field(default_factory=list)
    retrieval: list[SessionLogTraceRetrieval] = Field(default_factory=list)
    trace_deltas: list[SessionLogTraceDelta] = Field(default_factory=list)
    activity_steps: list[SessionLogTraceActivityStep] = Field(default_factory=list)
    suppressed: bool = False


class SessionLogToolCall(BaseModel):
    """Tool call metadata retained in a saved assistant turn."""
    model_config = ConfigDict(extra="allow")

    tool_id: str = Field(..., min_length=1, max_length=128)
    tool_name: str = Field(..., min_length=1, max_length=256)
    query: Optional[str] = None
    output_summary: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    guarded: bool = False


# Transcript message IDs: Sage message UUIDs, browser turn IDs, or legacy-<index>.
SESSION_LOG_MESSAGE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


class SessionLogTurn(BaseModel):
    """One turn of a captured transcript (admin-supplied on save)."""
    message_id: Optional[str] = Field(default=None, pattern=SESSION_LOG_MESSAGE_ID_PATTERN)
    role: str = Field(..., max_length=32)        # 'user' | 'assistant' | 'system'
    content: str
    ts: Optional[str] = None
    trace: Optional[SessionLogConversationTrace] = None
    tools_used: list[SessionLogToolCall] = Field(default_factory=list)


class SessionLogCreate(BaseModel):
    """Open a new session log (transcript saved later)."""
    source: str = Field(default="admin_test")    # 'admin_test' | 'user'
    title: Optional[str] = Field(default=None, max_length=200)
    subject_user_id: Optional[int] = None
    user_type_id: Optional[int] = None
    sage_session_id: Optional[str] = Field(default=None, max_length=200)


class SessionLogSaveTranscript(BaseModel):
    """Save (and encrypt) the transcript for a session log, marking it complete."""
    turns: list[SessionLogTurn]
    title: Optional[str] = Field(default=None, max_length=200)


class SessionLogMetadata(BaseModel):
    """Public metadata for a session log — never includes transcript content."""
    log_id: str
    source: str
    title: Optional[str] = None
    subject_user_id: Optional[int] = None
    user_type_id: Optional[int] = None
    sage_session_id: Optional[str] = None
    turn_count: int = 0
    status: str = "active"
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None
    has_transcript: bool = False


class SessionLogListResponse(BaseModel):
    session_logs: list[SessionLogMetadata]


class SessionLogMessageFeedback(BaseModel):
    """Feedback on one assistant message. The comment is returned as NIP-04
    ciphertext. turn_index is the message's current position, or None when the
    transcript no longer contains it."""
    message_id: str
    turn_index: Optional[int] = None
    rating: str                                  # 'up' | 'down'
    comment_ciphertext: Optional[str] = None
    comment_ephemeral_pubkey: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SessionLogTranscriptMessage(BaseModel):
    message_id: str
    role: str


class SessionLogDetail(SessionLogMetadata):
    """Full log for admin review: metadata + encrypted transcript + feedback.

    The transcript is NIP-04 ciphertext; the admin decrypts it client-side via
    NIP-07 using transcript_ephemeral_pubkey. The backend never decrypts it.
    """
    transcript_ciphertext: Optional[str] = None
    transcript_ephemeral_pubkey: Optional[str] = None
    encrypted_to_pubkey: Optional[str] = None
    # Plaintext message order of the transcript; feedback names these IDs.
    transcript_messages: list[SessionLogTranscriptMessage] = []
    feedback: list[SessionLogMessageFeedback] = []


class SessionMessageFeedbackRequest(BaseModel):
    """Set a message's thumbs up/down with an optional qualitative comment."""
    rating: str = Field(..., pattern="^(up|down)$")
    comment: Optional[str] = Field(default=None, max_length=5000)


class TestUserProvisionResponse(BaseModel):
    """A provisioned test user for a given persona (user type)."""
    user_id: int
    user_type_id: Optional[int] = None
    created: bool
