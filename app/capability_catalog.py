from __future__ import annotations

from typing import Any

from .commerce.tools import RETRYABLE_TOOL_NAMES, TOOL_REGISTRY, TOOL_SCHEMAS


# Read-only / safe-to-auto-retry commerce APIs the critique loop may call.
RETRYABLE_API_NAMES: frozenset[str] = frozenset(RETRYABLE_TOOL_NAMES)

_API_HINTS: dict[str, str] = {
    "search_products": "Buscar produtos reais no catálogo oficial",
    "get_product": "Detalhes de um produto por id",
    "check_inventory": "Estoque/disponibilidade",
    "search_customer": "Localizar cliente por CPF/CNPJ/e-mail",
    "get_customer": "Detalhes do cliente",
    "list_orders": "Listar pedidos do cliente",
    "get_order": "Consultar pedido",
    "get_payment_conditions": "Condições de pagamento disponíveis",
    "get_price_tables": "Tabelas de preço aplicáveis",
    "create_customer": "Cadastrar cliente (mutação)",
    "update_customer": "Atualizar cliente (mutação)",
    "create_order": "Criar pedido (mutação)",
    "update_order": "Atualizar pedido (mutação)",
}


def build_capability_catalog() -> dict[str, Any]:
    """Catalog of what the agent can do — for interpreter, responder and judge."""
    schema_names = {
        str((item.get("function") or {}).get("name") or "")
        for item in TOOL_SCHEMAS
        if isinstance(item, dict)
    }
    commerce = list(TOOL_REGISTRY.get("commerce") or ())
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
    return {
        "commerce_apis": [item["name"] for item in apis if item["domain"] == "commerce"],
        "retryable_apis": sorted(RETRYABLE_API_NAMES),
        "apis": apis,
        "policy": [
            "Nunca inventar produto, preço, estoque, pedido ou link de pagamento",
            "Usar a fonte comercial oficial para fatos comerciais; usar histórico/WORKING_MEMORY para continuidade",
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
    return "\n".join(lines)
