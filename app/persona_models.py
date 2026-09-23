from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


PersonaStatus = Literal["draft", "active", "archived"]
PersonaSource = Literal["user", "migration", "system"]


class PersonaVersion(BaseModel):
    id: int | None = None
    tenant_id: str
    persona_key: str
    version: int
    name: str
    source: PersonaSource = "user"
    instructions: str
    instructions_hash: str
    status: PersonaStatus = "draft"
    created_by: str | None = None
    activated_by: str | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonaVersionCreate(BaseModel):
    name: str = "XNamai Comercial"
    instructions: str
    created_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_business_policies(cls, value):
        from .business_policy import BusinessPolicy
        BusinessPolicy.model_validate(value.get("business_policies") or {})
        return value
