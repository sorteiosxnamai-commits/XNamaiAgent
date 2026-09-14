"""Ciclo de vida do produto no catalogo local, apos a validacao contra a Mercos real.

O erro que estes testes impedem e o mais caro da integracao inteira: o agente
oferecer um produto que a Mercos ja marcou como inativo ou excluido. Pular o
registro no sync NAO resolve — o snapshot antigo ficaria no indice para sempre.
A transicao de estado precisa REMOVER.

E o contrapeso: ausencia de campo nao e negacao. Um payload sem `ativo` nao
autoriza nem criar snapshot (afirmaria disponibilidade sem evidencia) nem apagar
o anterior (destruiria dado bom por causa de campo faltando).
"""

from __future__ import annotations

import httpx
import pytest

from app.commerce.mercos.catalog import (
    CatalogDecision,
    ProductPageWriter,
    decide,
)
from app.commerce.mercos.catalog_search import (
    SUPPORTED_FILTERS,
    UNSUPPORTED_FILTERS,
    check_inventory,
    get_product,
    search_products,
)
from app.commerce.mercos.client import MercosAdaptorClient
from app.commerce.mercos.normalizer import normalize_product
from app.commerce.mercos.sync import InMemorySyncStateStore, sync_resource
from tests.fixtures import mercos_payloads as fx

TENANT = "tenant-de-teste"


class _Index:
    """Indice em memoria com upsert e delete pontual."""

    def __init__(self, seed=None):
        self.items: dict[str, object] = dict(seed or {})
        self.fail_upsert = False
        self.fail_delete = False

    def upsert_products(self, tenant_id, products):
        if self.fail_upsert:
            raise RuntimeError("falha de upsert")
        for p in products:
            self.items[p.external_id] = p
        return len(products)

    def delete_products(self, tenant_id, product_ids):
        if self.fail_delete:
            raise RuntimeError("falha de delete")
        removed = 0
        for pid in product_ids:
            if self.items.pop(str(pid), None) is not None:
                removed += 1
        return removed

    # leitura, para os testes de read path
    def search_products(self, *, tenant_id, text, reference, category_id, available, limit):
        return list(self.items.values())[:limit]

    def get_product(self, *, tenant_id, product_id):
        return self.items.get(str(product_id))


def _writer(index):
    return ProductPageWriter(index, tenant_id=TENANT)


def _record(row):
    class _R:
        raw = row
    return _R()


async def _write(index, rows):
    writer = _writer(index)
    await writer.write_page("products", [_record(r) for r in rows])
    return writer.last_outcome


# === 1. category_id rejeitado ==============================================


def test_category_id_is_rejected_explicitly():
    """Campo nao existe no payload real (0/500): filtro nao pode ser anunciado."""
    result = search_products(_Index(), tenant_id=TENANT, arguments={"category_id": "55"})
    assert result["ok"] is False
    assert result["error"] == "unsupported_filter"
    assert result["unsupported_filters"] == ["category_id"]


def test_category_id_is_not_advertised_as_supported():
    assert "category_id" not in SUPPORTED_FILTERS
    assert "category_id" in UNSUPPORTED_FILTERS


# === 2-6. decisao por estado ===============================================


@pytest.mark.parametrize(
    "ativo,excluido,esperado",
    [
        (True, False, CatalogDecision.INDEX),
        (False, False, CatalogDecision.REMOVE),
        (True, True, CatalogDecision.REMOVE),
        (False, True, CatalogDecision.REMOVE),
        (None, False, CatalogDecision.IGNORE_UNKNOWN),
        (True, None, CatalogDecision.IGNORE_UNKNOWN),
        (None, None, CatalogDecision.IGNORE_UNKNOWN),
    ],
)
def test_state_decides_the_catalog_action(ativo, excluido, esperado):
    product = normalize_product(fx.product(ativo=ativo, excluido=excluido))
    assert decide(product) is esperado


@pytest.mark.asyncio
async def test_active_and_not_excluded_is_indexed():
    index = _Index()
    outcome = await _write(index, [fx.product(id=1, ativo=True, excluido=False)])
    assert outcome.upserted == 1
    assert "1" in index.items


@pytest.mark.asyncio
async def test_inactive_is_not_indexed_and_removes_the_previous_snapshot():
    index = _Index({"1": object()})
    outcome = await _write(index, [fx.product(id=1, ativo=False, excluido=False)])
    assert outcome.upserted == 0
    assert outcome.removed == 1
    assert "1" not in index.items


@pytest.mark.asyncio
async def test_excluded_is_not_indexed_and_removes_the_previous_snapshot():
    index = _Index({"1": object()})
    outcome = await _write(index, [fx.product(id=1, ativo=True, excluido=True)])
    assert outcome.upserted == 0
    assert outcome.removed == 1
    assert "1" not in index.items


@pytest.mark.asyncio
@pytest.mark.parametrize("ativo,excluido", [(None, False), (True, None), (None, None)])
async def test_unknown_state_neither_creates_nor_destroys(ativo, excluido):
    """Ausencia de campo nao e negacao: nao cria snapshot, nao apaga o anterior."""
    anterior = object()
    index = _Index({"1": anterior})
    outcome = await _write(index, [fx.product(id=1, ativo=ativo, excluido=excluido)])
    assert outcome.upserted == 0
    assert outcome.removed == 0
    assert outcome.ignored_unknown_state == 1
    assert index.items["1"] is anterior, "snapshot bom apagado por campo faltando"


# === 7-8. transicao entre syncs ============================================


@pytest.mark.asyncio
async def test_product_excluded_in_a_later_sync_disappears_from_search():
    index = _Index()
    await _write(index, [fx.product(id=1, ativo=True, excluido=False)])
    assert search_products(index, tenant_id=TENANT, arguments={"query": "a"})["count"] == 1

    await _write(index, [fx.product(id=1, ativo=True, excluido=True)])
    assert search_products(index, tenant_id=TENANT, arguments={"query": "a"})["count"] == 0


@pytest.mark.asyncio
async def test_product_deactivated_in_a_later_sync_disappears_from_search():
    index = _Index()
    await _write(index, [fx.product(id=1, ativo=True, excluido=False)])
    assert search_products(index, tenant_id=TENANT, arguments={"query": "a"})["count"] == 1

    await _write(index, [fx.product(id=1, ativo=False, excluido=False)])
    assert search_products(index, tenant_id=TENANT, arguments={"query": "a"})["count"] == 0


# === 9-11. atomicidade da pagina ===========================================


def _page(rows, next_cursor=None):
    return {
        "resource": "products",
        "count": len(rows),
        "pageCursor": rows[-1]["ultima_alteracao"] if rows else None,
        "nextCursor": next_cursor,
        "data": rows,
    }


def _client(payload):
    def handler(_):
        return httpx.Response(200, json=payload)

    return MercosAdaptorClient(
        base_url="https://a.example.com", api_key="k", timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_delete_failure_does_not_advance_the_cursor():
    index = _Index({"1": object()})
    index.fail_delete = True
    store = InMemorySyncStateStore()
    store.set_cursor("mercos", "products", "ANTES")

    outcome = await sync_resource(
        client=_client(_page([fx.product(id=1, ativo=False, excluido=False)])),
        resource="products", store=store, writer=_writer(index),
    )
    assert outcome.ok is False
    assert outcome.error_code == "write_failed"
    assert store.get_cursor("mercos", "products") == "ANTES"


@pytest.mark.asyncio
async def test_upsert_failure_does_not_advance_the_cursor():
    index = _Index()
    index.fail_upsert = True
    store = InMemorySyncStateStore()
    store.set_cursor("mercos", "products", "ANTES")

    outcome = await sync_resource(
        client=_client(_page([fx.product(id=1, ativo=True, excluido=False)])),
        resource="products", store=store, writer=_writer(index),
    )
    assert outcome.ok is False
    assert store.get_cursor("mercos", "products") == "ANTES"


@pytest.mark.asyncio
async def test_replaying_the_same_page_is_idempotent():
    rows = [
        fx.product(id=1, ativo=True, excluido=False),
        fx.product(id=2, ativo=False, excluido=False),
        fx.product(id=3, ativo=True, excluido=True),
    ]
    index = _Index()
    first = await _write(index, rows)
    estado = set(index.items)
    second = await _write(index, rows)

    assert set(index.items) == estado == {"1"}
    assert (first.upserted, second.upserted) == (1, 1)
    # A segunda passada nao tem o que remover: nada foi duplicado nem recriado.
    assert second.removed == 0


# === 12. read path: defesa em profundidade =================================


@pytest.mark.asyncio
async def test_search_never_returns_an_unavailable_snapshot():
    """Snapshot legado (gravado antes desta correcao) nao pode virar oferta."""
    index = _Index({
        "9": normalize_product(fx.product(id=9, ativo=False, excluido=False)),
        "8": normalize_product(fx.product(id=8, ativo=True, excluido=True)),
    })
    result = search_products(index, tenant_id=TENANT, arguments={"query": "a"})
    assert result["count"] == 0


@pytest.mark.parametrize("row", [fx.product_inactive(id=9), fx.product_excluded(id=9)])
def test_get_product_refuses_an_unavailable_snapshot(row):
    index = _Index({"9": normalize_product(row)})
    result = get_product(index, tenant_id=TENANT, product_id="9")
    assert result["ok"] is False
    assert result["error"] == "commerce_product_unavailable"


@pytest.mark.parametrize("row", [fx.product_inactive(id=9), fx.product_excluded(id=9)])
def test_inventory_says_unavailable_not_out_of_stock(row):
    """"Sem estoque" sugere que volta a ter. Aqui o produto e que saiu."""
    index = _Index({"9": normalize_product(row)})
    result = check_inventory(index, tenant_id=TENANT, product_id="9")
    assert result["ok"] is False
    assert result["error"] == "commerce_product_unavailable"
    assert result.get("stock") is None
    assert "stock_confirmed" not in result


# === 13-14. campos que o payload real mostrou ==============================


def test_missing_stock_stays_none_and_never_becomes_zero():
    product = normalize_product(fx.product_without_stock())
    assert product.stock is None
    assert product.available is None


def test_empty_reference_becomes_none_and_is_not_invented():
    """Nada de SKU sintetico, id como referencia ou nome copiado."""
    row = fx.product_without_reference(id=77, nome="Produto Sem Codigo")
    product = normalize_product(row)
    assert product.reference is None
    assert product.external_id == "77"
    assert product.name == "Produto Sem Codigo"


@pytest.mark.asyncio
async def test_product_without_reference_is_still_findable_by_name():
    index = _Index()
    await _write(index, [fx.product_without_reference(id=77, ativo=True, excluido=False)])
    result = search_products(index, tenant_id=TENANT, arguments={"query": "produto"})
    assert result["count"] == 1
    assert result["products"][0]["reference"] is None


def test_order_status_arrives_as_a_numeric_string():
    """A Mercos real devolve "0"/"2", nao 0/2."""
    from app.commerce.mercos.normalizer import normalize_order

    order = normalize_order(fx.order(status="2", status_faturamento="0"))
    assert order.status == "pedido"
    assert order.status_raw == 2
    assert order.billing_status == "nao_faturado"


# === contagem =============================================================


@pytest.mark.asyncio
async def test_page_outcome_counts_every_decision_without_payload():
    index = _Index({"2": object()})
    outcome = await _write(index, [
        fx.product(id=1, ativo=True, excluido=False),
        fx.product(id=2, ativo=False, excluido=False),
        fx.product(id=3, ativo=None, excluido=False),
    ])
    assert outcome.as_log() == {
        "received": 3, "upserted": 1, "removed": 1, "ignored_unknown_state": 1,
    }
    assert set(outcome.as_log()) == {
        "received", "upserted", "removed", "ignored_unknown_state"
    }
