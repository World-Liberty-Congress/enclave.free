"""Bounded, conservatively masked reads of the retained control-plane Audit Log."""
import base64
import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, ConfigDict, field_validator, model_validator

import database
from internal_agent import InternalAdminConfigToolRequest, _require_admin_actor, _require_internal_token

router = APIRouter(prefix="/internal/agent/admin-config", tags=["internal-agent"])
MAX_RESPONSE_BYTES = 1200
# Free text (including prompts), unknown/retired keys and secret-bearing tables
# stay masked even if today's configuration no longer marks them as secrets.
_PUBLIC_TEXT_KEYS = frozenset({"instance_name", "assistant_name", "header_tagline",
                               "default_theme", "default_language", "primary_color"})
_SCALAR_KEYS = frozenset({
    "temperature", "top_p", "top_k", "max_tokens", "max_iterations", "context_window",
    "memory_budget", "web_search_default", "rag_enabled", "rag_top_k",
})


class AuditReadRequest(InternalAdminConfigToolRequest):
    model_config = ConfigDict(extra="forbid")
    start_time: datetime | None = None
    end_time: datetime | None = None
    actor_id: str | None = Field(default=None, min_length=1, max_length=128)
    action_source: str | None = Field(default=None, min_length=1, max_length=128)
    config_key: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("start_time", "end_time")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("Audit time filters require a timezone")
            return value.astimezone(timezone.utc)
        return value

    @model_validator(mode="after")
    def ordered_interval(self) -> "AuditReadRequest":
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValueError("start_time must precede end_time")
        return self

    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=10, ge=1, le=20, strict=True)


def _encoded_size(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _excerpt(value: object, maximum: int = 64) -> tuple[str, bool]:
    text = str(value or "")
    def size(value: str) -> int:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size(text) <= maximum:
        return text, False
    # Bound encoded JSON, including escaped control characters and UTF-8.
    prefix = ""
    for character in text:
        if size(prefix + character + "…") > maximum:
            break
        prefix += character
    return prefix + "…", True


def _value(row: dict, value: str | None) -> str | dict | None:
    if value is None:
        return None
    if row["table_name"] == "instance_settings" and row["config_key"] in _PUBLIC_TEXT_KEYS:
        text, clipped = _excerpt(value, 80)
        return {"text": text, "excerpt": clipped}
    key = re.sub(r":type_\d+$", "", row["config_key"])
    if row["table_name"] in {"ai_config", "ai_config_user_type_overrides"} and key in _SCALAR_KEYS:
        if len(value) <= 32 and re.fullmatch(r"(?:true|false|-?\d+(?:\.\d+)?)", value):
            return value
    return {"redacted": True}


def _record(row: dict) -> dict:
    result = {"id": row["id"]}
    excerpted = []
    for target, source in (("at", "changed_at"), ("table", "table_name"), ("key", "config_key"),
                           ("actor", "changed_by"), ("source", "action_source")):
        result[target], clipped = _excerpt(row[source], 80 if target == "actor" else 64)
        if clipped:
            excerpted.append(target)
    result.update(before=_value(row, row["old_value"]), after=_value(row, row["new_value"]))
    if excerpted:
        result["excerpted_fields"] = excerpted
    return result


def _cursor(row: dict, snapshot: int, scope: str) -> str:
    payload = [row["sort_time"], row["id"], snapshot, scope]
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None, scope: str) -> tuple[tuple[int, int] | None, int | None]:
    if cursor is None:
        return None, None
    try:
        data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        at, row_id, snapshot, cursor_scope = data
        if cursor_scope != scope:
            raise ValueError()
        if (type(at) is not int or not -(2**63) <= at < 2**63
                or type(row_id) is not int or type(snapshot) is not int or not 0 < row_id <= snapshot <= 2**63 - 1):
            raise ValueError()
        return (at, row_id), snapshot
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        raise HTTPException(status_code=422, detail="Invalid audit cursor") from None


@router.post("/read-audit-log", dependencies=[Depends(_require_internal_token)])
async def read_audit_log(payload: AuditReadRequest) -> dict:
    _require_admin_actor(payload.actor)
    filters = payload.model_dump(mode="json", exclude={"actor", "cursor", "limit"})
    scope = hashlib.sha256(json.dumps([payload.actor.id, filters], sort_keys=True).encode()).hexdigest()[:16]
    after, snapshot = _decode_cursor(payload.cursor, scope)
    rows, matched, snapshot = database.query_audit_insights(
        limit=payload.limit + 1, after=after, snapshot=snapshot, **filters)
    result = {"records": [], "next_cursor": None, "coverage": {
        "scope": "retained_audit", "matched": matched, "returned": 0,
        "values": "Public branding excerpts and safe scalars only; other values redacted.",
    }}
    for index, row in enumerate(rows[:payload.limit]):
        record = _record(row)
        has_more = index + 1 < len(rows)
        candidate = {**result, "records": [*result["records"], record],
                     "next_cursor": _cursor(row, snapshot, scope) if has_more else None,
                     "coverage": {**result["coverage"], "returned": index + 1}}
        if _encoded_size(candidate) > MAX_RESPONSE_BYTES:
            break
        result = candidate
    if rows and not result["records"]:
        raise HTTPException(status_code=500, detail="An audit record could not fit the bounded page")
    return result
