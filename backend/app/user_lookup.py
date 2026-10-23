"""Bounded Admin ciphertext projection. No signer or decrypted data lives here."""

from __future__ import annotations

import hashlib
import math
from typing import Annotated, Any, Literal, Self
from typing_extensions import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

import auth
import database

router = APIRouter(prefix="/admin/user-lookup")
Id = Annotated[StrictInt, Field(gt=0, le=9007199254740991)]
Selector = Annotated[StrictStr, Field(pattern=r"^(name|email|field:[1-9][0-9]{0,15})$")]


class PlainCell(TypedDict):
    value: str


class CipherCell(TypedDict):
    ciphertext: str
    ephemeral_pubkey: str


class FieldMetadata(TypedDict):
    selector: str
    type: str


class ProjectedUser(TypedDict):
    id: int
    values: dict[str, PlainCell | CipherCell | None]


class ProjectionResponse(TypedDict):
    users: list[ProjectedUser]
    fields: list[FieldMetadata]
    through_id: int
    next_after_id: int | None
    eligible: int


class SessionResponse(TypedDict):
    pubkey: str
    session_fingerprint: str


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Predicate(StrictModel):
    field: Selector
    operator: Literal["equals", "contains", "gt", "gte", "lt", "lte"]
    value: StrictStr | StrictInt | float | StrictBool


class Metadata(StrictModel):
    approved: StrictBool | None = None
    user_type_id: Id | None = None
    pubkey_present: StrictBool | None = None


class Projection(StrictModel):
    user_ids: Annotated[list[Id], Field(min_length=1, max_length=20)] | None = None
    after_id: Annotated[StrictInt, Field(ge=0, le=9007199254740991)] = 0
    through_id: Annotated[StrictInt, Field(ge=0, le=9007199254740991)] | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 20
    select: Annotated[list[Selector], Field(min_length=1, max_length=6)]
    where: Annotated[list[Predicate], Field(max_length=3)] = []
    metadata: Metadata = Field(default_factory=Metadata)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if len(set(self.select)) != len(self.select):
            raise ValueError("duplicate selectors")
        if self.user_ids is not None:
            if len(set(self.user_ids)) != len(self.user_ids):
                raise ValueError("duplicate IDs")
            if (
                self.after_id
                or self.through_id is not None
                or self.metadata.model_fields_set
            ):
                raise ValueError("exact IDs and page filters are exclusive")
        if any(p.field not in self.select for p in self.where):
            raise ValueError("predicate field must be selected")
        return self


def _credential(request: Request) -> str:
    return (
        auth._resolve_auth_token(
            request.headers.get("authorization"),
            request.cookies.get(auth.ADMIN_SESSION_COOKIE_NAME),
        )
        or ""
    )


@router.get("/session")
async def lookup_session(
    request: Request,
    response: Response,
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> SessionResponse:
    response.headers["Cache-Control"] = "no-store"
    return {
        "pubkey": admin["pubkey"],
        "session_fingerprint": hashlib.sha256(
            _credential(request).encode()
        ).hexdigest(),
    }


def _cell(
    value: str | int | float | bool | None,
    ciphertext: str | None,
    pubkey: str | None,
) -> PlainCell | CipherCell | None:
    if ciphertext is not None:
        # Preserve malformed ciphertext as unreadable, never silently substitute plaintext.
        return {"ciphertext": ciphertext, "ephemeral_pubkey": pubkey or ""}
    return {"value": str(value)} if value is not None else None


def project(request: Projection) -> ProjectionResponse:
    field_ids = [int(s[6:]) for s in request.select if s.startswith("field:")]
    fields: list[FieldMetadata] = [
        {"selector": s, "type": "text"}
        for s in request.select
        if s in ("name", "email")
    ]
    with database.get_cursor() as cursor:
        if field_ids:
            marks = ",".join("?" for _ in field_ids)
            cursor.execute(
                f"SELECT id, field_type FROM user_field_definitions WHERE id IN ({marks})",
                field_ids,
            )
            fields.extend(
                {"selector": f"field:{row['id']}", "type": row["field_type"]}
                for row in cursor.fetchall()
            )
        types = {f["selector"]: f["type"] for f in fields}
        if set(types) != set(request.select):
            raise HTTPException(400, "Unknown field selector")
        for predicate in request.where:
            field_type = types[predicate.field]
            value = predicate.value
            if (
                predicate.operator in ("gt", "gte", "lt", "lte")
                or field_type == "number"
            ):
                valid = (
                    field_type == "number"
                    and type(value) in (int, float)
                    and math.isfinite(value)
                    and predicate.operator != "contains"
                )
            elif field_type == "checkbox":
                valid = type(value) is bool and predicate.operator == "equals"
            else:
                valid = (
                    isinstance(value, str)
                    and len(value) <= 1000
                    and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
                    and predicate.operator in ("equals", "contains")
                )
            if not valid:
                raise HTTPException(400, "Predicate does not match field type")

        clauses, params = [], []
        if request.user_ids is not None:
            clauses.append("id IN (" + ",".join("?" for _ in request.user_ids) + ")")
            params.extend(request.user_ids)
            through = max(request.user_ids)
            eligible = len(request.user_ids)
        else:
            through = request.through_id
            if through is None:
                cursor.execute("SELECT COALESCE(MAX(id),0) FROM users")
                through = cursor.fetchone()[0]
            clauses.append("id <= ?")
            params.append(through)
            for key in ("approved", "user_type_id"):
                value = getattr(request.metadata, key)
                if value is not None:
                    clauses.append(f"{key} = ?")
                    params.append(value)
            if request.metadata.pubkey_present is not None:
                clauses.append("(pubkey IS NOT NULL AND pubkey != '') = ?")
                params.append(request.metadata.pubkey_present)
            cursor.execute(
                "SELECT COUNT(*) FROM users WHERE " + " AND ".join(clauses), params
            )
            eligible = cursor.fetchone()[0]
            clauses.append("id > ?")
            params.append(request.after_id)
        columns = ["id"]
        for name in ("name", "email"):
            if name in request.select:
                columns.extend([name, f"encrypted_{name}", f"ephemeral_pubkey_{name}"])
        # All interpolated identifiers are fixed above; all values are parameters.
        cursor.execute(
            "SELECT "
            + ",".join(columns)
            + " FROM users WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id LIMIT ?",
            [*params, request.limit + 1 if request.user_ids is None else 20],
        )
        rows = cursor.fetchall()
        has_more = request.user_ids is None and len(rows) > request.limit
        rows = rows[: request.limit] if request.user_ids is None else rows
        users: dict[int, ProjectedUser] = {
            r["id"]: {"id": r["id"], "values": {s: None for s in request.select}}
            for r in rows
        }
        for row in rows:
            for name in ("name", "email"):
                if name in request.select:
                    users[row["id"]]["values"][name] = _cell(
                        row[name],
                        row[f"encrypted_{name}"],
                        row[f"ephemeral_pubkey_{name}"],
                    )
        if users and field_ids:
            cursor.execute(
                "SELECT user_id,field_id,value,encrypted_value,ephemeral_pubkey FROM user_field_values WHERE user_id IN ("
                + ",".join("?" for _ in users)
                + ") AND field_id IN ("
                + ",".join("?" for _ in field_ids)
                + ")",
                [*users, *field_ids],
            )
            for row in cursor.fetchall():
                users[row["user_id"]]["values"][f"field:{row['field_id']}"] = _cell(
                    row["value"], row["encrypted_value"], row["ephemeral_pubkey"]
                )
    return {
        "users": list(users.values()),
        "fields": fields,
        "through_id": through,
        "next_after_id": rows[-1]["id"] if has_more else None,
        "eligible": eligible,
    }


@router.post("/projection")
async def lookup_projection(
    request: Projection,
    response: Response,
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> ProjectionResponse:
    response.headers["Cache-Control"] = "no-store"
    return project(request)
