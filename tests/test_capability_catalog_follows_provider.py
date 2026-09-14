"""O catalogo anunciado ao modelo acompanha o que o provider ativo sabe servir.

Propriedade em duas direcoes:

* sem provider em uso (Null, ou provider sem capacidade exposta) o catalogo
  descreve o CONTRATO e o prompt fica byte a byte igual ao baseline;
* com um provider em uso que declara capacidades, o catalogo encolhe para elas —
  anunciar carrinho/cupom/frete que o fornecedor nao tem convida o modelo a
  prometer o que nao existe.
"""

from __future__ import annotations

import pytest

from app.capability_catalog import (
    build_capability_catalog,
    format_capability_catalog_for_prompt,
)
from app.commerce.provider import (
    get_commerce_provider,
    reset_commerce_provider,
    set_commerce_provider,
)
from app.commerce.tools import TOOL_REGISTRY


@pytest.fixture(autouse=True)
def _restore_provider():
    reset_commerce_provider()
    yield
    reset_commerce_provider()


class _UnavailableProvider:
    name = "stub-indisponivel"
    available = False
    llm_capabilities = frozenset({"get_order"})

    async def execute(self, capability, arguments):  # pragma: no cover
        raise AssertionError("nao deve ser chamado")


class _LimitedProvider:
    name = "stub-limitado"
    available = True
    llm_capabilities = frozenset({"search_products", "get_product", "get_order"})

    async def execute(self, capability, arguments):  # pragma: no cover
        return {"ok": True}


def test_null_provider_keeps_the_declared_contract():
    assert get_commerce_provider().available is False
    catalog = build_capability_catalog()
    assert set(catalog["commerce_apis"]) == set(TOOL_REGISTRY["commerce"])


def test_unavailable_provider_does_not_shrink_the_catalog():
    """Provider presente mas sem capacidade exposta nao altera o prompt."""
    set_commerce_provider(_UnavailableProvider())
    catalog = build_capability_catalog()
    assert set(catalog["commerce_apis"]) == set(TOOL_REGISTRY["commerce"])


def test_available_provider_shrinks_the_catalog_to_what_it_serves():
    set_commerce_provider(_LimitedProvider())
    catalog = build_capability_catalog()
    assert set(catalog["commerce_apis"]) == {"search_products", "get_product", "get_order"}


@pytest.mark.parametrize(
    "capability", ["get_cart", "create_cart", "list_coupons", "quote_shipping", "get_order_payment"]
)
def test_capabilities_the_provider_cannot_serve_are_not_announced(capability):
    set_commerce_provider(_LimitedProvider())
    catalog = build_capability_catalog()
    assert capability not in catalog["commerce_apis"]
    assert capability not in format_capability_catalog_for_prompt()


def test_catalog_never_crashes_the_turn_when_the_provider_explodes(monkeypatch):
    """O catalogo alimenta o prompt: falhar aqui derrubaria o turno inteiro."""

    def boom():
        raise RuntimeError("provider quebrado")

    monkeypatch.setattr("app.commerce.provider.get_commerce_provider", boom)
    catalog = build_capability_catalog()
    assert set(catalog["commerce_apis"]) == set(TOOL_REGISTRY["commerce"])
