"""Busca local, leitura de produto e estoque — com validade explicita.

Nenhum teste toca banco nem Mercos: o leitor do indice e injetado.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.commerce.mercos.catalog import product_to_index_fields, sanitize_payload
from app.commerce.mercos.catalog_search import (
    SUPPORTED_FILTERS,
    UNSUPPORTED_FILTERS,
    check_inventory,
    get_product,
    resolve_limit,
    search_products,
)
from app.commerce.mercos.normalizer import normalize_product
from tests.fixtures import mercos_payloads as fx

NOW = datetime.now(timezone.utc)


def _product(**overrides):
    """Produto sincronizado AGORA, salvo quando o teste pede outro instante.

    A fixture tem um `ultima_alteracao` fixo; aqui o padrao e "recem-sincronizado"
    para que freshness so entre em jogo quando o teste quiser.
    """
    fields = {"ultima_alteracao": NOW.isoformat()}
    fields.update(overrides)
    return normalize_product(fx.product(**fields))


class _Reader:
    """Indice em memoria."""

    def __init__(self, products=None):
        self.products = products or []
        self.calls = []

    def search_products(self, *, tenant_id, text, reference, category_id, available, limit, offset=0):
        self.calls.append(
            {"text": text, "reference": reference, "category_id": category_id,
             "available": available, "limit": limit}
        )
        return self.products[:limit]

    def get_product(self, *, tenant_id, product_id):
        for product in self.products:
            if product.external_id == str(product_id):
                return product
        return None


# --- filtros ----------------------------------------------------------------


@pytest.mark.parametrize("unsupported", sorted(UNSUPPORTED_FILTERS))
def test_unsupported_filters_are_rejected_explicitly(unsupported):
    """Ignorar em silencio faria o agente crer que filtrou. Recusar e honesto."""
    result = search_products(_Reader(), tenant_id="t", arguments={unsupported: "x"})
    assert result["ok"] is False
    assert result["error"] == "unsupported_filter"
    assert unsupported in result["unsupported_filters"]
    assert sorted(SUPPORTED_FILTERS) == result["supported_filters"]


def test_empty_unsupported_filter_is_not_a_rejection():
    """Campo presente e vazio nao e pedido de filtro."""
    result = search_products(_Reader(), tenant_id="t", arguments={"brand": "", "query": "a"})
    assert result["ok"] is True


def test_supported_filters_reach_the_index():
    reader = _Reader()
    search_products(
        reader,
        tenant_id="t",
        arguments={"query": " relogio ", "reference": " REF-1 ", "available": True, "limit": 3},
    )
    assert reader.calls[0] == {
        "text": "relogio", "reference": "REF-1", "category_id": None,
        "available": True, "limit": 3,
    }


def test_name_is_accepted_as_text_when_query_is_absent():
    reader = _Reader()
    search_products(reader, tenant_id="t", arguments={"name": "Produto"})
    assert reader.calls[0]["text"] == "Produto"


@pytest.mark.parametrize("raw,expected", [(None, 10), ("x", 10), (0, 1), (5, 5), (999, 20), (-3, 1)])
def test_limit_is_clamped(raw, expected):
    assert resolve_limit(raw) == expected


# --- resultado --------------------------------------------------------------


def test_search_returns_products_with_freshness():
    result = search_products(_Reader([_product()]), tenant_id="t", arguments={"query": "a"})
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["source"] == "local_index"
    product = result["products"][0]
    assert product["external_id"] == "1001"
    assert product["freshness"]["stock_confirmed"] is True


def test_search_never_leaks_raw_payload():
    result = search_products(
        _Reader([_product(observacoes="anotacao interna")]),
        tenant_id="t",
        arguments={"query": "a"},
    )
    assert "anotacao interna" not in str(result)
    assert "raw" not in result["products"][0]


def test_empty_result_is_not_an_error():
    result = search_products(_Reader([]), tenant_id="t", arguments={"query": "nada"})
    assert result["ok"] is True
    assert result["count"] == 0
    assert result["products"] == []


# --- get_product ------------------------------------------------------------


def test_get_product_returns_the_snapshot():
    result = get_product(_Reader([_product()]), tenant_id="t", product_id="1001")
    assert result["ok"] is True
    assert result["product"]["name"] == "Produto Sintetico A"


def test_get_product_missing_is_explicit_not_empty():
    result = get_product(_Reader([]), tenant_id="t", product_id="9999")
    assert result["ok"] is False
    assert result["error"] == "product_not_found"


# --- check_inventory --------------------------------------------------------


def test_inventory_reports_a_confirmed_number():
    result = check_inventory(_Reader([_product(saldo_estoque=7)]), tenant_id="t", product_id="1001")
    assert result["ok"] is True
    assert result["stock"] == 7
    assert result["stock_confirmed"] is True
    assert result["available"] is True


def test_inventory_distinguishes_absent_stock_from_zero():
    """A distincao que evita negar venda de item que existe."""
    absent = check_inventory(
        _Reader([_product(saldo_estoque=...)]), tenant_id="t", product_id="1001"
    )
    zero = check_inventory(
        _Reader([_product(saldo_estoque=0)]), tenant_id="t", product_id="1001"
    )
    assert absent["stock"] is None
    assert absent["available"] is None
    assert zero["stock"] == 0
    assert zero["available"] is False


def test_expired_stock_keeps_the_value_but_is_not_confirmed():
    """Snapshot velho nao vira "sem estoque" — vira "nao confirmei"."""
    old = (NOW - timedelta(hours=6)).isoformat()
    result = check_inventory(
        _Reader([_product(saldo_estoque=4, ultima_alteracao=old)]),
        tenant_id="t",
        product_id="1001",
    )
    assert result["stock"] == 4
    assert result["stock_confirmed"] is False
    assert result["freshness"]["freshness"] == "unconfirmed"


def test_inventory_missing_product_is_explicit():
    result = check_inventory(_Reader([]), tenant_id="t", product_id="9999")
    assert result["ok"] is False
    assert result["error"] == "product_not_found"


# --- mapeamento para o indice -----------------------------------------------


def test_index_fields_follow_the_official_mapping():
    fields = product_to_index_fields(_product(), synced_at=NOW)
    assert fields["product_id"] == "1001"
    assert fields["reference"] == "REF-1001"
    assert fields["title_normalized"] == "produto sintetico a"
    assert fields["price"] == 149.90
    assert fields["stock"] == 7
    assert fields["factual_source"] == "commerce_search"


def test_index_keeps_none_instead_of_zero():
    fields = product_to_index_fields(
        _product(preco_tabela=..., saldo_estoque=...), synced_at=NOW
    )
    assert fields["price"] is None
    assert fields["stock"] is None


def test_freshness_uses_the_sync_instant_not_the_provider_timestamp():
    """Este teste ja afirmou o contrario — e era o bug.

    `freshness_at` alimenta o TTL de leitura do indice e o `synced_at` da
    politica de frescor: ambos perguntam "ha quanto tempo NOS confirmamos isto",
    nao "quando a origem mudou". Gravar `ultima_alteracao` aqui apagava o
    catalogo inteiro da leitura. A data da origem segue no payload.
    """
    stamp = "2026-02-01T08:00:00"
    fields = product_to_index_fields(_product(ultima_alteracao=stamp), synced_at=NOW)
    assert fields["freshness_at"] == NOW
    assert fields["payload"]["ultima_alteracao"] == stamp


def test_freshness_falls_back_to_sync_time_without_provider_timestamp():
    fields = product_to_index_fields(_product(ultima_alteracao=...), synced_at=NOW)
    assert fields["freshness_at"] == NOW


def test_payload_drops_free_text_that_may_carry_internal_notes():
    payload = sanitize_payload(fx.product(observacoes="negociacao interna"))
    assert "observacoes" not in payload
    assert payload["codigo"] == "REF-1001"
