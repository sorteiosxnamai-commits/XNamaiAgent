from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: Capacidades comerciais genéricas. Contratos — nem todas implementadas na Parte 1.
COMMERCE_CAPABILITIES: tuple[str, ...] = (
    "search_products",
    "get_product",
    "check_inventory",
    "search_customer",
    "get_customer",
    "create_customer",
    "update_customer",
    "list_orders",
    "get_order",
    "create_order",
    "update_order",
    "get_payment_conditions",
    "get_price_tables",
)


@runtime_checkable
class CommerceProvider(Protocol):
    """Fonte comercial oficial. Implementada por provider concreto."""

    name: str

    async def execute(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ...
