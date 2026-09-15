"""A fronteira entre o pacote Mercos e o indice compartilhado.

Dois defeitos viviam exatamente aqui, e nenhum aparecia nos testes porque todos
substituiam o indice por um duplo em memoria — que nao tem coluna `payload` nem
tratamento de erro de banco.

1. `upsert_canonical_items` gravava a projecao das PROPRIAS colunas do item na
   coluna `payload`, descartando `item.raw`. O reader do Mercos le `nome`,
   `ativo`, `excluido` e `ultima_alteracao` dali: recebia `None` nos quatro. O
   nome do produto caia para `title_normalized` (minusculo, como o cliente veria)
   e a defesa de disponibilidade do read path ficava cega.

2. O mesmo writer engolia QUALQUER excecao e devolvia `0`. Para indexacao
   oportunista isso e aceitavel; para o sync incremental e mentira: a pagina
   parecia processada, o cursor avancava, e os registros daquela pagina sumiam
   do catalogo para sempre. A garantia central do sync — "cursor so avanca
   depois da pagina inteira persistida" — era falsa na pratica.

As duas correcoes sao OPT-IN. Os callers legados continuam com o comportamento
antigo, e ha teste para isso.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from app.catalog_index import CanonicalCatalogItem, upsert_canonical_items
from app.commerce.mercos.catalog import product_to_index_fields, sanitize_payload
from app.commerce.mercos.catalog_index_reader import (
    CatalogIndexProductReader,
    _to_product,
)
from app.commerce.mercos.normalizer import normalize_product
from tests.fixtures import mercos_payloads as fx

TENANT = "xnamai"
INSTANTE_DO_SYNC = datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)
DATA_NA_ORIGEM = "2025-11-20T08:56:18"
NOME_ORIGINAL = "Produto Exemplo"


# === duplo de banco: captura o que teria sido gravado ======================


class _CursorFalso:
    def __init__(self, gravados: list[dict[str, Any]], explodir: bool):
        self._gravados = gravados
        self._explodir = explodir

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        if self._explodir:
            raise RuntimeError("falha de INSERT no indice")
        if params is not None:
            self._gravados.append(params)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ConexaoFalsa:
    def __init__(self, gravados, explodir):
        self._gravados = gravados
        self._explodir = explodir

    def cursor(self):
        return _CursorFalso(self._gravados, self._explodir)

    def commit(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def banco(monkeypatch):
    """Substitui SOMENTE o driver; o SQL e os binds sao os reais."""

    def montar(*, explodir: bool = False) -> list[dict[str, Any]]:
        gravados: list[dict[str, Any]] = []
        import app.db as db

        monkeypatch.setattr(db, "ensure_tables", lambda: None)
        monkeypatch.setattr(db, "get_conn", lambda: _ConexaoFalsa(gravados, explodir))
        monkeypatch.setattr(db, "to_jsonb", lambda value: value)
        return gravados

    return montar


def _item_mercos(**overrides: Any) -> CanonicalCatalogItem:
    overrides.setdefault("nome", NOME_ORIGINAL)
    overrides.setdefault("ultima_alteracao", DATA_NA_ORIGEM)
    overrides.setdefault("observacoes", "NAO PERSISTIR")
    row = fx.product(**overrides)
    campos = product_to_index_fields(normalize_product(row), synced_at=INSTANTE_DO_SYNC)
    return CanonicalCatalogItem(
        tenant_id=TENANT,
        catalog_item_key=f"product:{campos['product_id']}",
        product_id=campos["product_id"],
        reference=campos["reference"],
        title_normalized=campos["title_normalized"],
        price=campos["price"],
        stock=campos["stock"],
        available=campos["available"],
        category=campos["category"],
        freshness_at=campos["freshness_at"],
        factual_source=campos["factual_source"],
        raw=campos["payload"],
    )


# === 1. payload do Mercos sobrevive a persistencia =========================


def test_the_mercos_payload_is_what_gets_stored(banco):
    gravados = banco()
    assert upsert_canonical_items([_item_mercos()], persist_raw_payload=True) == 1
    payload = gravados[0]["payload"]
    assert payload["nome"] == NOME_ORIGINAL
    assert payload["ativo"] is True
    assert payload["excluido"] is False
    assert payload["ultima_alteracao"] == DATA_NA_ORIGEM


def test_the_sanitized_fields_stay_out_of_the_stored_payload(banco):
    """`sanitize_payload` remove campo livre; persistir o raw nao o traz de volta."""
    gravados = banco()
    upsert_canonical_items([_item_mercos()], persist_raw_payload=True)
    payload = gravados[0]["payload"]
    assert "observacoes" not in payload
    assert "produtos_grade" not in payload
    assert "NAO PERSISTIR" not in str(payload)


def test_the_column_binds_still_come_from_the_item_projection(banco):
    """So o CONTEUDO da coluna payload muda — as colunas seguem as do item."""
    gravados = banco()
    upsert_canonical_items([_item_mercos()], persist_raw_payload=True)
    bind = gravados[0]
    assert bind["product_id"] == "1001"
    assert bind["title_normalized"] == "produto exemplo"
    # `model_dump(mode="json")` serializa o datetime; o instante e o mesmo.
    assert bind["freshness_at"].startswith("2026-09-15T13:30:00")


def test_freshness_at_written_is_the_sync_instant(banco):
    """O instante gravado e o do sync, nao a data de alteracao na origem."""
    gravados = banco()
    upsert_canonical_items([_item_mercos()], persist_raw_payload=True)
    assert gravados[0]["freshness_at"].startswith("2026-09-15T13:30:00")
    assert not gravados[0]["freshness_at"].startswith("2025-11-20")
    assert gravados[0]["payload"]["ultima_alteracao"] == DATA_NA_ORIGEM


# === 2. ida e volta completa: o reader recupera o que o writer gravou ======


def test_the_reader_recovers_name_and_state_from_the_stored_payload(banco):
    """Mercos raw -> indice -> leitura: a viagem inteira, sem duplo em memoria."""
    gravados = banco()
    upsert_canonical_items([_item_mercos()], persist_raw_payload=True)

    linha = dict(gravados[0])
    produto = _to_product(linha)

    assert produto is not None
    assert produto.name == NOME_ORIGINAL, "nome perdeu a caixa original"
    assert produto.active is True
    assert produto.excluded is False
    assert produto.raw["ultima_alteracao"] == DATA_NA_ORIGEM


def test_without_persisting_raw_the_reader_loses_name_and_state(banco):
    """Documenta o bug: e exatamente o que producao mostrou."""
    gravados = banco()
    upsert_canonical_items([_item_mercos()])  # legado, sem a flag

    produto = _to_product(dict(gravados[0]))
    assert produto is not None
    assert produto.name != NOME_ORIGINAL
    assert produto.active is None
    assert produto.excluded is None


def test_the_read_path_defence_can_see_an_inactive_snapshot_again(banco):
    """Com o payload certo, produto inativo volta a ser recusado na leitura."""
    from app.commerce.mercos.catalog_search import is_commercially_unavailable

    gravados = banco()
    upsert_canonical_items([_item_mercos(ativo=False)], persist_raw_payload=True)
    produto = _to_product(dict(gravados[0]))
    assert is_commercially_unavailable(produto) is True


# === 3. o comportamento legado nao mudou ===================================


def test_legacy_callers_keep_storing_the_column_projection(banco):
    gravados = banco()
    upsert_canonical_items([_item_mercos()])
    payload = gravados[0]["payload"]
    assert "product_id" in payload, "payload legado deixou de ser a projecao"
    assert "nome" not in payload
    assert "ultima_alteracao" not in payload


def test_legacy_callers_still_swallow_write_failures(banco):
    """Best-effort segue best-effort: indexacao oportunista nao derruba turno."""
    banco(explodir=True)
    assert upsert_canonical_items([_item_mercos()]) == 0


def test_index_products_best_effort_is_untouched(banco, monkeypatch):
    """O caller legado real nao passa flag alguma."""
    import app.catalog_index as ci

    capturado: dict[str, Any] = {}

    def espiao(items, **kwargs):
        capturado.update(kwargs)
        return len(items)

    monkeypatch.setattr(ci, "upsert_canonical_items", espiao)
    ci.index_products_best_effort([{"id": "1", "name": "x", "tenant_id": TENANT}])
    assert capturado == {}, f"caller legado passou flags: {capturado}"


def test_persist_raw_ignores_a_non_dict_raw(banco):
    """Defesa: raw invalido nao pode virar payload corrompido."""
    gravados = banco()
    item = _item_mercos()
    objeto = item.model_copy(update={"raw": None})
    upsert_canonical_items([objeto], persist_raw_payload=True)
    assert "product_id" in gravados[0]["payload"]


# === 4. atomicidade: falha de escrita nao pode avancar cursor ==============


def test_strict_propagates_the_write_failure(banco):
    banco(explodir=True)
    with pytest.raises(RuntimeError):
        upsert_canonical_items([_item_mercos()], strict=True)


def test_the_mercos_reader_opts_into_strict_and_raw(banco, monkeypatch):
    """O unico caminho que liga as duas flags e o do pacote Mercos."""
    import app.catalog_index as ci

    capturado: dict[str, Any] = {}

    def espiao(items, **kwargs):
        capturado.update(kwargs)
        return len(items)

    monkeypatch.setattr(ci, "upsert_canonical_items", espiao)
    banco()
    CatalogIndexProductReader().upsert_products(
        TENANT, [normalize_product(fx.product(nome=NOME_ORIGINAL))]
    )
    assert capturado == {"persist_raw_payload": True, "strict": True}


def _pagina(linhas, next_cursor=None):
    return {
        "resource": "products",
        "count": len(linhas),
        "pageCursor": linhas[-1]["ultima_alteracao"] if linhas else None,
        "nextCursor": next_cursor,
        "data": linhas,
    }


def _cliente(payload):
    from app.commerce.mercos.client import MercosAdaptorClient

    return MercosAdaptorClient(
        base_url="https://a.example.com",
        api_key="k",
        timeout_seconds=5,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )


@pytest.mark.asyncio
async def test_a_failed_upsert_does_not_advance_the_cursor(banco):
    """A garantia central, agora exercitada com o writer REAL do indice."""
    from app.commerce.mercos.catalog import ProductPageWriter
    from app.commerce.mercos.sync import InMemorySyncStateStore, sync_resource

    banco(explodir=True)
    store = InMemorySyncStateStore()
    store.set_cursor("mercos", "products", "POSICAO-ORIGINAL")

    outcome = await sync_resource(
        client=_cliente(_pagina([fx.product(id=1, ativo=True, excluido=False)])),
        resource="products",
        store=store,
        writer=ProductPageWriter(CatalogIndexProductReader(), tenant_id=TENANT),
    )

    assert outcome.ok is False
    assert outcome.error_code == "write_failed"
    assert store.get_cursor("mercos", "products") == "POSICAO-ORIGINAL"


@pytest.mark.asyncio
async def test_without_strict_the_same_failure_would_advance_the_cursor(banco):
    """Reproduz o bug: engolindo a excecao, a pagina "conclui" e o cursor anda."""
    import app.catalog_index as ci
    from app.commerce.mercos.catalog import ProductPageWriter
    from app.commerce.mercos.sync import InMemorySyncStateStore, sync_resource

    banco(explodir=True)
    original = ci.upsert_canonical_items
    try:
        ci.upsert_canonical_items = lambda items, **kw: original(items)  # sem strict
        store = InMemorySyncStateStore()
        store.set_cursor("mercos", "products", "POSICAO-ORIGINAL")
        outcome = await sync_resource(
            client=_cliente(_pagina([fx.product(id=1, ativo=True, excluido=False)])),
            resource="products",
            store=store,
            writer=ProductPageWriter(CatalogIndexProductReader(), tenant_id=TENANT),
        )
    finally:
        ci.upsert_canonical_items = original

    assert outcome.ok is True, "sem strict a falha de banco passa despercebida"
    assert store.get_cursor("mercos", "products") != "POSICAO-ORIGINAL"


def test_the_delete_path_already_raises_on_failure():
    """Assimetria original: delete ja levantava; so o upsert engolia."""
    import inspect

    from app.catalog_index_repository import CatalogIndexRepository

    fonte = inspect.getsource(CatalogIndexRepository.delete_items)
    assert "raise" in fonte
