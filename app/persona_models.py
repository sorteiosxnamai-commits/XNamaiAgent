from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


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
    name: str = "NewStore Commercial"
    instructions: str
    created_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
