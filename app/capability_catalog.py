from __future__ import annotations

from typing import Any

from .tray_tools import TOOL_REGISTRY, TOOL_SCHEMAS


# Read-only / safe-to-auto-retry commerce APIs the critique loop may call.
RETRYABLE_API_NAMES: frozenset[str] = frozenset(
    {
        "search_products",
        "get_product",
        "get_product_link",
        "check_inventory",
        "get_cart",
        "get_cart_complete",
        "get_payment_options",
        "search_customer",
        "get_customer",
        "list_coupons",
        "get_coupon",
        "list_orders",
        "get_order",
        "get_order_complete",
        "get_order_payment",
        "quote_shipping",
        "list_shipping_methods",
    }
)

_API_HINTS: dict[str, str] = {
    "search_products": "Buscar produtos reais na loja",
    "get_product": "Detalhes de um produto por id",
    "get_product_link": "Link oficial do produto",
    "check_inventory": "Estoque/disponibilidade",
    "get_cart": "Consultar carrinho por session_id",
    "get_cart_complete": "Itens e totais do carrinho",
    "get_payment_options": "Opções de pagamento de carrinho/pedido",
    "search_customer": "Localizar cliente por CPF/e-mail",
    "get_customer": "Detalhes do cliente",
    "list_orders": "Listar pedidos do cliente/sessão",
    "get_order": "Consultar pedido",
    "get_order_complete": "Status completo do pedido",
    "get_order_payment": "Status/link de pagamento do pedido",
    "quote_shipping": "Cotação de frete",
    "list_shipping_methods": "Métodos de frete",
    "create_cart": "Criar carrinho (mutação)",
    "create_order": "Criar pedido (mutação)",
    "set_cart_item_quantity": "Alterar item do carrinho (mutação)",
}


def build_capability_catalog() -> dict[str, Any]:
    """Catalog of what the agent can do — for interpreter, responder and judge."""
    schema_names = {
        str((item.get("function") or {}).get("name") or "")
        for item in TOOL_SCHEMAS
        if isinstance(item, dict)
    }
    commerce = list(TOOL_REGISTRY.get("commerce") or ())
    raffle = list(TOOL_REGISTRY.get("raffle") or ())
    apis = []
    for name in commerce:
        apis.append(
            {
                "name": name,
                "domain": "commerce",
                "retryable": name in RETRYABLE_API_NAMES,
                "in_openai_tool_loop": name in schema_names,
                "hint": _API_HINTS.get(name, ""),
            }
        )
    for name in raffle:
        apis.append(
            {
                "name": name,
                "domain": "raffle",
                "retryable": False,
                "in_openai_tool_loop": False,
                "hint": f"Sorteio/local: {name}",
            }
        )
    return {
        "commerce_apis": [item["name"] for item in apis if item["domain"] == "commerce"],
        "raffle_capabilities": raffle,
        "retryable_apis": sorted(RETRYABLE_API_NAMES),
        "apis": apis,
        "policy": [
            "Nunca inventar produto, preço, estoque, pedido ou link de pagamento",
            "Usar APIs Tray para fatos comerciais; usar histórico/WORKING_MEMORY para continuidade",
            "Em pedido de link/pagamento, recuperar pedido e payment_url antes de responder",
            "Não afirmar ausência de pedido sem consultar histórico, estado e APIs relevantes",
        ],
    }


def format_capability_catalog_for_prompt(catalog: dict[str, Any] | None = None) -> str:
    payload = catalog or build_capability_catalog()
    lines = ["CAPABILITIES (o que você pode fazer):"]
    for policy in payload.get("policy") or []:
        lines.append(f"- {policy}")
    lines.append("APIs commerce disponíveis:")
    for name in payload.get("commerce_apis") or []:
        hint = _API_HINTS.get(str(name), "")
        retry = "retryable" if name in RETRYABLE_API_NAMES else "manual"
        lines.append(f"- {name} ({retry}){': ' + hint if hint else ''}")
    raffle = payload.get("raffle_capabilities") or []
    if raffle:
        lines.append("Capacidades de sorteio: " + ", ".join(str(item) for item in raffle))
    return "\n".join(lines)
