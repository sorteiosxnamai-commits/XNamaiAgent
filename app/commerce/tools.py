from __future__ import annotations

import time
from typing import Any

from .errors import COMMERCE_UNAVAILABLE_CODE, CommerceUnavailableError
from .provider import get_commerce_provider

#: Definicoes de tool expostas ao modelo. Conteudo identico ao baseline
#: 201bd16: a Parte 1 troca o EXECUTOR, nunca a superficie que o modelo ve.
#: Nenhuma capability nova pode ser adicionada aqui antes de existir um provider
#: concreto que a implemente (ver docs da Parte 2).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "search_products", "description": "Pesquisar produtos reais na loja.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "name": {"type": "string"}, "reference": {"type": "string"}, "ean": {"type": "string"}, "brand": {"type": "string"}, "tokens": {"type": "array", "items": {"type": "string"}, "description": "AND search tokens (ILIKE %token% each)"}, "category_id": {"type": "string"}, "available": {"type": "boolean"}, "available_in_store": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "page": {"type": "integer", "minimum": 1}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_product", "description": "Consultar detalhes atuais de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_product_link", "description": "Obter o link oficial de um produto real já identificado.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "check_inventory", "description": "Confirmar estoque e regras de disponibilidade de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_cart", "description": "Consultar um carrinho já identificado por sua sessão.", "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_cart_complete", "description": "Consultar itens e totais atuais de um carrinho já identificado.", "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_payment_options", "description": "Consultar opções reais de pagamento de um carrinho ou pedido existente. Informe exatamente um escopo.", "parameters": {"type": "object", "properties": {"cart_session_id": {"type": "string"}, "order_id": {"type": "string"}}, "oneOf": [{"required": ["cart_session_id"]}, {"required": ["order_id"]}], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_customer", "description": "Pesquisar um cliente com filtro específico, quando necessário.", "parameters": {"type": "object", "properties": {"email": {"type": "string"}, "cpf": {"type": "string"}, "cnpj": {"type": "string"}, "name": {"type": "string"}, "limit": {"type": "integer", "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_customer", "description": "Consultar um cliente identificado.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_coupons", "description": "Consultar cupons quando a conversa precisar disso.", "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "limit": {"type": "integer", "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_coupon", "description": "Consultar detalhes de um cupom.", "parameters": {"type": "object", "properties": {"coupon_id": {"type": "string"}}, "required": ["coupon_id"], "additionalProperties": False}}},
]
#: Capacidades comerciais reconhecidas pelo executor. Identico ao baseline,
#: menos a chave "raffle": o dominio de sorteio saiu do runtime na Parte 1.
TOOL_REGISTRY: dict[str, tuple[str, ...]] = {
    "commerce": ("search_products", "get_product", "get_product_link", "check_inventory", "list_categories", "get_category", "get_category_tree", "list_product_variants", "get_product_variant", "search_customer", "get_customer", "create_customer", "list_coupons", "get_coupon", "create_cart", "get_cart", "get_cart_complete", "set_cart_item_quantity", "delete_cart", "get_payment_options", "quote_shipping", "list_shipping_methods", "create_order", "list_orders", "get_order", "get_order_complete", "get_order_payment"),
}

#: Mutacoes: nunca auto-retentaveis pelo loop de critique.
MUTATION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "create_cart",
        "set_cart_item_quantity",
        "delete_cart",
        "create_order",
        "create_customer",
    }
)

#: Leitura sem auto-retry: consultas de catalogo/variacao que o loop de
#: critique nao reexecuta sozinho. Read-only, mas deliberadamente fora de
#: RETRYABLE — classificacao do baseline 201bd16.
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "list_categories",
        "get_category",
        "get_category_tree",
        "list_product_variants",
        "get_product_variant",
    }
)

#: Leitura segura -> o loop de critique pode reexecutar. Espelha
#: ``capability_catalog.RETRYABLE_API_NAMES`` (baseline).
RETRYABLE_TOOL_NAMES: frozenset[str] = frozenset(
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


def commerce_tools_available() -> bool:
    """Ha um provider comercial capaz de executar as tools?

    Gating generico por provider — nunca por env de fornecedor. Com o
    ``NullCommerceProvider`` isto e False e nenhuma tool comercial chega ao
    modelo, exatamente como no baseline sem Tray configurado.
    """
    return bool(getattr(get_commerce_provider(), "available", False))


def _schema_name(item: dict[str, Any]) -> str:
    return str((item.get("function") or {}).get("name") or "")


def tool_schemas_for_model() -> list[dict[str, Any]] | None:
    """Schemas a enviar ao modelo, ou ``None`` quando nao ha provider.

    Capability discovery: um provider disponivel NAO significa que ele implementa
    as capacidades herdadas. Quando o provider declara ``llm_capabilities``, so
    os schemas correspondentes sao expostos — oferecer ao modelo uma tool que o
    provider nao sabe executar produz tool-call inutil e, pior, passa a impressao
    de que o fato esta disponivel.
    """
    if not commerce_tools_available():
        return None
    provider = get_commerce_provider()
    allowed = getattr(provider, "llm_capabilities", None)
    if allowed is None:
        return TOOL_SCHEMAS
    schemas = [item for item in TOOL_SCHEMAS if _schema_name(item) in allowed]
    return schemas or None


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
    provider_name = "unknown"
    try:
        provider = get_commerce_provider()
        provider_name = getattr(provider, "name", "unknown")
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
    # Metadata segura: nome do provider e da capacidade, nunca argumento nem
    # payload — argumentos comerciais carregam documento, telefone e e-mail.
    print("[commerce.tool] executed", {
        "provider": provider_name,
        "tool": name,
        "ok": ok,
        "elapsed_ms": round(elapsed_ms),
        "error": result.get("error") if isinstance(result, dict) else None,
        "code": result.get("code") if isinstance(result, dict) else None,
    })
    print("[sales.tool]", {"tool": name, "success": ok})
    return result
