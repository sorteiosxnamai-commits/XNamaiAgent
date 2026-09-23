from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: Capacidades comerciais genericas reconhecidas pela fronteira.
#: Espelha ``commerce.tools.TOOL_REGISTRY["commerce"]`` (baseline 201bd16).
#: A Parte 1 NAO antecipa capacidades de nenhum fornecedor futuro: uma
#: capability so entra aqui quando existe provider concreto que a implemente.
COMMERCE_CAPABILITIES: tuple[str, ...] = (
    "search_products",
    "get_product",
    "get_product_link",
    "check_inventory",
    "list_categories",
    "get_category",
    "get_category_tree",
    "list_product_variants",
    "get_product_variant",
    "search_customer",
    "get_customer",
    "create_customer",
    "list_coupons",
    "get_coupon",
    "create_cart",
    "get_cart",
    "get_cart_complete",
    "set_cart_item_quantity",
    "delete_cart",
    "get_payment_options",
    "quote_shipping",
    "list_shipping_methods",
    "create_order",
    "list_orders",
    "get_order",
    "get_order_complete",
    "get_order_payment",
)


@runtime_checkable
class CommerceProvider(Protocol):
    """Fonte comercial oficial. Implementada por provider concreto."""

    name: str
    #: O provider consegue executar capacidades agora? Quando False, nenhuma
    #: tool comercial e exposta ao modelo.
    available: bool

    async def execute(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ...
