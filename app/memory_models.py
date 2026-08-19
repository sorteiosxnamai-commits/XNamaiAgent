"""Structured memory / instruction-extension proposal models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class MemoryScope(str, Enum):
    conversation = "conversation"
    contact = "contact"
    tenant_instruction = "tenant_instruction"


class MemoryAction(str, Enum):
    upsert = "upsert"
    forget = "forget"
    none = "none"


class MemoryKind(str, Enum):
    preferred_name = "preferred_name"
    communication_style = "communication_style"
    product_preference = "product_preference"
    brand_preference = "brand_preference"
    price_preference = "price_preference"
    color_preference = "color_preference"
    material_preference = "material_preference"
    size_preference = "size_preference"
    occasion = "occasion"
    recipient = "recipient"
    explicit_no_preference = "explicit_no_preference"
    correction = "correction"
    do_not_repeat = "do_not_repeat"
    stable_customer_fact = "stable_customer_fact"
    temporary_commitment = "temporary_commitment"
    conversation_goal = "conversation_goal"
    instruction_improvement = "instruction_improvement"


MemoryReasonCode = Literal[
    "explicit_user_preference",
    "explicit_user_correction",
    "explicit_user_identity",
    "explicit_user_forget_request",
    "repeated_pattern",
    "future_relevance",
    "conversation_commitment",
    "persona_gap_detected",
    "do_not_ask_again",
    "temporary_context",
]


class MemoryProposal(BaseModel):
    action: MemoryAction = MemoryAction.none
    scope: MemoryScope = MemoryScope.contact
    kind: MemoryKind = MemoryKind.stable_customer_fact

    key: str = ""
    value: str | int | float | bool | dict[str, Any] | list[Any] | None = None
    safe_summary: str | None = None

    importance: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=0.0, ge=0, le=1)

    reason_code: MemoryReasonCode = "future_relevance"

    ttl_days: int | None = None
    use_in_instructions: bool = False

    @field_validator("key")
    @classmethod
    def _trim_key(cls, value: str) -> str:
        return (value or "").strip()[:120]


class InstructionExtensionProposal(BaseModel):
    extension_key: str = ""
    proposed_instruction: str = ""

    scope: Literal["tenant", "channel", "contact"] = "tenant"
    scope_key: str | None = None

    category: Literal[
        "tone",
        "continuity",
        "clarification",
        "response_length",
        "customer_preference",
        "error_recovery",
        "domain_routing",
    ] = "tone"

    importance: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=0.0, ge=0, le=1)

    evidence_summary: str = ""
    requires_human_approval: bool = True

    @field_validator("extension_key")
    @classmethod
    def _trim_extension_key(cls, value: str) -> str:
        return (value or "").strip()[:120]

    @field_validator("proposed_instruction")
    @classmethod
    def _trim_instruction(cls, value: str) -> str:
        return (value or "").strip()[:2000]


class ConversationSummaryDelta(BaseModel):
    current_goal: str | None = None
    resolved_points: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    user_corrections: list[str] = Field(default_factory=list)
    commitments: list[str] = Field(default_factory=list)
    last_failure: str | None = None


class ResponseClaim(BaseModel):
    type: str = "generic"
    value: str | None = None
    fact_key: str | None = None


class AgentTurnEnvelope(BaseModel):
    """Side-channel envelope for natural reply + memory proposals."""

    reply: str
    claims: list[ResponseClaim] = Field(default_factory=list)

    memory_proposals: list[MemoryProposal] = Field(default_factory=list, max_length=5)
    instruction_extension_proposals: list[InstructionExtensionProposal] = Field(
        default_factory=list,
        max_length=2,
    )

    conversation_summary_delta: ConversationSummaryDelta | None = None


class MemoryPolicyDecision(BaseModel):
    accepted: bool
    auto_apply: bool = False
    requires_review: bool = True
    normalized_key: str | None = None
    normalized_value: Any | None = None
    expires_at: datetime | None = None
    rejection_codes: list[str] = Field(default_factory=list)
    sensitive_detected: bool = False
    proposal_type: str = "contact_memory"


class ContactMemory(BaseModel):
    id: int | None = None
    tenant_id: str
    sender_key: str
    memory_key: str
    memory_kind: str
    value: dict[str, Any] | Any = Field(default_factory=dict)
    safe_summary: str | None = None
    source: str = "model_proposal"
    status: str = "active"
    importance: float = 0.0
    confidence: float = 0.0
    use_in_instructions: bool = False
    sensitive: bool = False
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryProcessingResult(BaseModel):
    proposals_seen: int = 0
    proposals_persisted: int = 0
    proposals_rejected: int = 0
    proposals_duplicate: int = 0
    proposals_applied: int = 0
    proposals_pending_review: int = 0
    proposal_ids: list[int] = Field(default_factory=list)
    rejection_codes: list[str] = Field(default_factory=list)
