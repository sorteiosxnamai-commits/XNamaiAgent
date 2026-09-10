import pytest

from app.commerce.contracts import COMMERCE_CAPABILITIES
from app.commerce.errors import COMMERCE_UNAVAILABLE_CODE
from app.commerce.tools import (
    MUTATION_TOOL_NAMES,
    RETRYABLE_TOOL_NAMES,
    TOOL_REGISTRY,
    TOOL_SCHEMAS,
    execute_tool,
)

NEUTRAL_NAMES = {
    "search_products",
    "get_product",
    "check_inventory",
    "search_customer",
    "get_customer",
    "list_orders",
    "get_order",
    "create_order",
}


def test_registry_has_no_raffle_domain():
    assert "raffle" not in TOOL_REGISTRY


def test_neutral_tool_names_are_preserved():
    assert NEUTRAL_NAMES.issubset(set(TOOL_REGISTRY["commerce"]))


def test_no_tray_or_newstore_specific_tools():
    forbidden = {"get_cart", "get_cart_complete", "create_cart", "list_coupons", "get_coupon"}
    assert forbidden.isdisjoint(set(TOOL_REGISTRY["commerce"]))


def test_schemas_are_openai_function_shaped():
    for item in TOOL_SCHEMAS:
        assert item["type"] == "function"
        assert item["function"]["name"] in TOOL_REGISTRY["commerce"]
        assert item["function"]["parameters"]["type"] == "object"


def test_read_only_and_mutation_are_disjoint_and_complete():
    """Verificado contra a fonte independente, não contra a própria construção.

    ``TOOL_REGISTRY["commerce"]`` é literalmente a união de RETRYABLE|MUTATION,
    então comparar com ele é tautológico. A fonte independente é
    ``COMMERCE_CAPABILITIES`` em ``app/commerce/contracts.py``: toda capacidade
    declarada no contrato precisa estar classificada como leitura OU mutação,
    e nenhuma pode estar nos dois.
    """
    assert RETRYABLE_TOOL_NAMES.isdisjoint(MUTATION_TOOL_NAMES)

    declared = set(COMMERCE_CAPABILITIES)
    classified = RETRYABLE_TOOL_NAMES | MUTATION_TOOL_NAMES

    assert not (declared - classified), (
        f"capacidades do contrato sem classificação: {sorted(declared - classified)}"
    )
    assert not (classified - declared), (
        f"tools sem capacidade declarada no contrato: {sorted(classified - declared)}"
    )


def test_mutations_are_never_auto_retryable():
    """Propriedade de segurança: nada que muta estado entra no caminho de retry."""
    assert MUTATION_TOOL_NAMES
    assert MUTATION_TOOL_NAMES.isdisjoint(RETRYABLE_TOOL_NAMES)
    for name in MUTATION_TOOL_NAMES:
        assert name.startswith(("create_", "update_", "delete_"))


def test_registry_is_exactly_the_declared_contract():
    assert set(TOOL_REGISTRY["commerce"]) == set(COMMERCE_CAPABILITIES)


@pytest.mark.asyncio
async def test_execute_tool_returns_unavailable_without_raising():
    result = await execute_tool("search_products", {"query": "relogio"})
    assert result["ok"] is False
    assert result["error"] == COMMERCE_UNAVAILABLE_CODE


@pytest.mark.asyncio
async def test_execute_tool_never_invents_data():
    result = await execute_tool("get_product", {"product_id": "123"})
    assert "products" not in result
    assert "price" not in result
