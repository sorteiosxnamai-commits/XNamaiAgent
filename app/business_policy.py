"""Allowlisted business controls published as part of a persona version."""
from contextvars import ContextVar

from pydantic import BaseModel, ConfigDict, Field


class BusinessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    catalog_browse_limit: int = Field(default=10, ge=1, le=10)
    conversation_repair_handoff_after: int = Field(default=2, ge=1, le=5)
    knowledge_max_chunks: int = Field(default=4, ge=1, le=4)
    repair_ack: str = Field(default="Entendi, vou corrigir a consulta.", min_length=1, max_length=300)
    repair_clarification: str = Field(default="Entendi, interpretei errado. Qual produto ou referência você estava procurando?", min_length=1, max_length=600)
    repair_handoff: str = Field(default="Não consegui entender seu pedido corretamente. Vou encaminhar o atendimento para a equipe da XNamai.", min_length=1, max_length=600)


_POLICY: ContextVar[BusinessPolicy] = ContextVar("xnamai_business_policy", default=BusinessPolicy())


def current_policy() -> BusinessPolicy:
    return _POLICY.get()


def bind_policy(metadata: dict | None):
    policy = BusinessPolicy.model_validate((metadata or {}).get("business_policies") or {})
    return _POLICY.set(policy)


def reset_policy(token):
    _POLICY.reset(token)
