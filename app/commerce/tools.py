from __future__ import annotations

import time
from typing import Any

from .errors import COMMERCE_UNAVAILABLE_CODE, CommerceUnavailableError
from .provider import get_commerce_provider

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "search_products", "description": "Pesquisar produtos reais no catálogo oficial.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "name": {"type": "string"}, "reference": {"type": "string"}, "ean": {"type": "string"}, "brand": {"type": "string"}, "tokens": {"type": "array", "items": {"type": "string"}}, "available": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "page": {"type": "integer", "minimum": 1}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_product", "description": "Consultar detalhes atuais de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "check_inventory", "description": "Confirmar estoque e disponibilidade de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_customer", "description": "Pesquisar um cliente com filtro específico.", "parameters": {"type": "object", "properties": {"email": {"type": "string"}, "cpf": {"type": "string"}, "cnpj": {"type": "string"}, "name": {"type": "string"}, "limit": {"type": "integer", "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_customer", "description": "Consultar um cliente identificado.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_orders", "description": "Listar pedidos do cliente.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}, "limit": {"type": "integer", "maximum": 20}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_order", "description": "Consultar um pedido.", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_payment_conditions", "description": "Consultar condições de pagamento disponíveis.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}, "order_id": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_price_tables", "description": "Consultar tabelas de preço aplicáveis.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "create_customer", "description": "Cadastrar um cliente.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "email": {"type": "string"}, "cpf": {"type": "string"}, "cnpj": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "update_customer", "description": "Atualizar dados de um cliente.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}, "name": {"type": "string"}, "email": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "create_order", "description": "Criar um pedido.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}, "items": {"type": "array", "items": {"type": "object"}}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "update_order", "description": "Atualizar um pedido.", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}}},
]

RETRYABLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "search_products",
        "get_product",
        "check_inventory",
        "search_customer",
        "get_customer",
        "list_orders",
        "get_order",
        "get_payment_conditions",
        "get_price_tables",
    }
)

MUTATION_TOOL_NAMES: frozenset[str] = frozenset(
    {"create_customer", "update_customer", "create_order", "update_order"}
)

TOOL_REGISTRY: dict[str, tuple[str, ...]] = {
    "commerce": tuple(sorted(RETRYABLE_TOOL_NAMES | MUTATION_TOOL_NAMES)),
}

async def execute_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Executa uma capacidade comercial pelo provider configurado.

    Sem provider (Parte 1), devolve erro estruturado em vez de levantar,
    para que os chamadores existentes sigam tratando o resultado como dado.

    Toda chamada e instrumentada: a telemetria de tool-call e generica (vale
    para qualquer provider) e alimenta ``runtime.commerce_calls``, o evento
    ``commerce.call`` e as metricas de turno. Sucesso, indisponibilidade e erro
    inesperado sao todos observados.
    """
    # Import tardio: observability depende de runtime_context/turn_runtime e
    # importa-lo no topo criaria ciclo. Mesma estrategia do modulo anterior.
    from ..observability import record_commerce_observation
    from ..runtime_context import register_commerce_call

    args = dict(arguments or {})
    started = time.perf_counter()
    register_commerce_call()
    try:
        provider = get_commerce_provider()
        result = await provider.execute(name, args)
    except CommerceUnavailableError as exc:
        result = {
            "ok": False,
            "error": COMMERCE_UNAVAILABLE_CODE,
            "capability": exc.capability,
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "error": "commerce_tool_error",
            "error_type": type(exc).__name__,
            "status_code": getattr(exc, "status_code", None),
        }

    elapsed_ms = (time.perf_counter() - started) * 1000
    record_commerce_observation(
        tool=name,
        arguments=args,
        result=result,
        elapsed_ms=elapsed_ms,
    )
    ok = isinstance(result, dict) and "error" not in result
    print("[commerce.tool] executed", {
        "tool": name,
        "ok": ok,
        "elapsed_ms": round(elapsed_ms),
        "error": result.get("error") if isinstance(result, dict) else None,
    })
    print("[sales.tool]", {"tool": name, "success": ok})
    return result
