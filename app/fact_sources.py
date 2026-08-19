"""Authority hierarchy for agent facts (Phase 3 + Etapa 5).

Security rules (immutable code) always win over persona, memory, Tray,
customer text, and history. Commercial numbers/links come only from Tray
(live preferred) or the official owning service. Persona may shape tone
only — never volatile price/stock/payment data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class FactSource(str, Enum):
    LOCAL_DATABASE = "local_database"
    TRAY_ADAPTER = "tray_adapter"
    TRAY_LIVE = "tray_live"
    CATALOG_SNAPSHOT = "catalog_snapshot"
    COMMERCE_STATE = "commerce_state"
    APPROVED_PERSONA = "approved_persona"
    CUSTOMER_MEMORY = "customer_memory"
    DETERMINISTIC_RULE = "deterministic_rule"
    SECURITY_RULE = "security_rule"


# Higher = stronger when sources conflict (Etapa 5 commerce ordering).
FACT_SOURCE_RANK: dict[FactSource, int] = {
    FactSource.CUSTOMER_MEMORY: 10,
    FactSource.APPROVED_PERSONA: 20,
    FactSource.COMMERCE_STATE: 40,
    FactSource.CATALOG_SNAPSHOT: 55,
    FactSource.LOCAL_DATABASE: 65,
    FactSource.TRAY_ADAPTER: 75,
    FactSource.TRAY_LIVE: 85,
    FactSource.DETERMINISTIC_RULE: 90,
    FactSource.SECURITY_RULE: 100,
}


EntityType = Literal[
    "product",
    "variant",
    "price",
    "inventory",
    "url",
    "order",
    "payment",
    "shipping",
    "cart",
    "customer",
    "raffle",
    "policy",
    "other",
]

_COMMERCIAL_TYPES = frozenset(
    {"product", "variant", "price", "inventory", "url", "payment", "shipping", "cart"}
)
_FORBIDDEN_COMMERCE = frozenset(
    {FactSource.APPROVED_PERSONA, FactSource.CUSTOMER_MEMORY}
)


class StructuredFact(BaseModel):
    """Internal evidence used for validation/observability — not shown to customers."""

    source: FactSource
    entity_type: EntityType = "other"
    entity_id: str | None = None
    key: str
    value: Any = None
    retrieved_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    tool_call_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    tenant_id: str | None = None
    revalidation_status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def authority_rank(self) -> int:
        return FACT_SOURCE_RANK.get(self.source, 0)


def preferred_fact(
    facts: list[StructuredFact],
    *,
    key: str | None = None,
    entity_type: EntityType | None = None,
) -> StructuredFact | None:
    candidates = [
        fact
        for fact in facts
        if (key is None or fact.key == key)
        and (entity_type is None or fact.entity_type == entity_type)
        and not (
            fact.entity_type in _COMMERCIAL_TYPES
            and fact.source in _FORBIDDEN_COMMERCE
        )
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.authority_rank(),
            1 if (item.metadata or {}).get("revalidated") else 0,
            item.confidence or 0.0,
        ),
    )


def infer_source_for_payload_key(
    key: str,
    *,
    used_tray: bool = False,
    from_commerce_state: bool = False,
    from_local_db: bool = False,
    factual_source: str | None = None,
    revalidated: bool = False,
) -> FactSource:
    lowered = (key or "").lower()
    factual = (factual_source or "").strip().lower()
    if revalidated or factual == "tray_live":
        return FactSource.TRAY_LIVE
    if factual in {"catalog_cache", "catalog_index"}:
        return FactSource.CATALOG_SNAPSHOT
    if from_local_db or any(
        token in lowered
        for token in ("balance", "coupon", "raffle", "sorteio", "particip")
    ):
        return FactSource.LOCAL_DATABASE
    if from_commerce_state or factual == "conversation_ref" or any(
        token in lowered
        for token in (
            "pending_action",
            "purchase_stage",
            "checkout_draft",
            "presented_product",
            "active_product",
        )
    ):
        return FactSource.COMMERCE_STATE
    if used_tray or any(
        token in lowered
        for token in (
            "price",
            "stock",
            "inventory",
            "product",
            "variant",
            "payment_url",
            "order_id",
            "shipping",
            "cart",
        )
    ):
        return FactSource.TRAY_ADAPTER
    return FactSource.DETERMINISTIC_RULE
