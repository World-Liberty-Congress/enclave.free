"""Bounded ownership/provenance checks for Admin retained-message usage reports."""
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

import database
import test_user_provenance
from internal_agent import InternalActorContext, _require_admin_actor, _require_internal_token

router = APIRouter(prefix="/internal/agent/admin", tags=["internal-agent"])
UserId = Annotated[StrictInt, Field(gt=0, le=2147483647)]


class UsageUsersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: InternalActorContext
    user_ids: Annotated[list[UserId], Field(max_length=200)]

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.user_ids)) != len(self.user_ids):
            raise ValueError("Duplicate User IDs")
        return self


@router.post("/usage-users", dependencies=[Depends(_require_internal_token)])
async def usage_users(payload: UsageUsersRequest) -> dict:
    _require_admin_actor(payload.actor)
    if not payload.user_ids:
        return {"users": []}
    with database.get_cursor() as cursor:
        placeholders = ",".join("?" for _ in payload.user_ids)
        cursor.execute(f"SELECT id FROM users WHERE id IN ({placeholders})", payload.user_ids)
        existing = {row["id"] for row in cursor.fetchall()}
    return {"users": [
        {"id": user_id, "is_test": test_user_provenance.is_test_identity(user_id)}
        for user_id in payload.user_ids if user_id in existing
    ]}
