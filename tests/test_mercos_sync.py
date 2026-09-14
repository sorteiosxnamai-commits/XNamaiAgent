"""Sync incremental: paginacao, atomicidade do cursor e idempotencia.

O que este arquivo protege e a propriedade mais cara de errar num sync
incremental: **o cursor so avanca depois que a pagina inteira foi gravada**. Se
ele avancar no meio, os registros restantes daquela pagina somem para sempre —
e ninguem percebe, porque a proxima execucao comeca depois deles.

Nenhum teste toca a Mercos nem o adaptador real: transporte simulado e escritor
injetado.
"""

from __future__ import annotations

import httpx
import pytest

from app.commerce.mercos.client import MercosAdaptorClient
from app.commerce.mercos.sync import (
    InMemorySyncStateStore,
    SyncOutcome,
    sync_resource,
)


class _Writer:
    """Escritor de teste: acumula e pode falhar num item especifico."""

    def __init__(self, fail_on: str | None = None):
        self.written: list[str] = []
        self.fail_on = fail_on
        self.calls = 0

    async def write_page(self, resource, records):
        self.calls += 1
        for record in records:
            if self.fail_on is not None and record.external_id == self.fail_on:
                raise RuntimeError("falha ao gravar item")
            self.written.append(record.external_id)
        return len(records)


def _pages(*pages):
    """Handler que devolve as paginas na ordem, respeitando o cursor."""
    state = {"i": 0}

    def handler(request: httpx.Request):
        index = state["i"]
        state["i"] = min(index + 1, len(pages) - 1)
        return httpx.Response(200, json=pages[index])

    return handler


def _page(rows, next_cursor=None, resource="products"):
    return {
        "resource": resource,
        "count": len(rows),
        "pageCursor": rows[-1]["ultima_alteracao"] if rows else None,
        "nextCursor": next_cursor,
        "data": rows,
    }


def _row(pid, changed):
    return {"id": pid, "ultima_alteracao": changed}


def _client(handler):
    return MercosAdaptorClient(
        base_url="https://adaptor.example.com",
        api_key="internal-key",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


# --- caminho feliz ----------------------------------------------------------


@pytest.mark.asyncio
async def test_single_page_writes_records_and_advances_the_cursor():
    handler = _pages(_page([_row(1, "2026-01-01T00:00:00")]))
    store = InMemorySyncStateStore()
    writer = _Writer()

    outcome = await sync_resource(
        client=_client(handler), resource="products", store=store, writer=writer
    )

    assert outcome.ok is True
    assert outcome.records == 1
    assert outcome.pages == 1
    assert writer.written == ["1"]
    assert store.get_cursor("mercos", "products") == "2026-01-01T00:00:00"


@pytest.mark.asyncio
async def test_follows_pagination_until_there_is_no_next_cursor():
    handler = _pages(
        _page([_row(1, "2026-01-01T00:00:00")], next_cursor="2026-01-01T00:00:00"),
        _page([_row(2, "2026-01-02T00:00:00")], next_cursor="2026-01-02T00:00:00"),
        _page([_row(3, "2026-01-03T00:00:00")]),
    )
    store = InMemorySyncStateStore()
    writer = _Writer()

    outcome = await sync_resource(
        client=_client(handler), resource="products", store=store, writer=writer
    )

    assert writer.written == ["1", "2", "3"]
    assert outcome.pages == 3
    assert store.get_cursor("mercos", "products") == "2026-01-03T00:00:00"


@pytest.mark.asyncio
async def test_resumes_from_the_stored_cursor():
    seen = {}

    def handler(request: httpx.Request):
        seen["cursor"] = request.url.params.get("alterado_apos")
        return httpx.Response(200, json=_page([_row(9, "2026-02-01T00:00:00")]))

    store = InMemorySyncStateStore()
    store.set_cursor("mercos", "products", "2026-01-31T00:00:00")

    await sync_resource(
        client=_client(handler), resource="products", store=store, writer=_Writer()
    )
    assert seen["cursor"] == "2026-01-31T00:00:00"


# --- atomicidade: o ponto que nao pode falhar -------------------------------


@pytest.mark.asyncio
async def test_cursor_does_not_advance_when_a_record_fails():
    """Falha no item N deixa o cursor onde estava — a pagina volta inteira."""
    handler = _pages(_page([_row(1, "2026-01-01T00:00:00"), _row(2, "2026-01-02T00:00:00")]))
    store = InMemorySyncStateStore()
    store.set_cursor("mercos", "products", "2025-12-31T00:00:00")

    outcome = await sync_resource(
        client=_client(handler),
        resource="products",
        store=store,
        writer=_Writer(fail_on="2"),
    )

    assert outcome.ok is False
    assert outcome.error_code == "write_failed"
    assert store.get_cursor("mercos", "products") == "2025-12-31T00:00:00"


@pytest.mark.asyncio
async def test_cursor_keeps_the_last_confirmed_page_when_a_later_page_fails():
    """Paginas ja gravadas nao sao perdidas; a que falhou sera refeita."""
    handler = _pages(
        _page([_row(1, "2026-01-01T00:00:00")], next_cursor="2026-01-01T00:00:00"),
        _page([_row(2, "2026-01-02T00:00:00")], next_cursor="2026-01-02T00:00:00"),
    )
    store = InMemorySyncStateStore()

    outcome = await sync_resource(
        client=_client(handler),
        resource="products",
        store=store,
        writer=_Writer(fail_on="2"),
    )

    assert outcome.ok is False
    assert store.get_cursor("mercos", "products") == "2026-01-01T00:00:00"


@pytest.mark.asyncio
async def test_adaptor_failure_never_advances_the_cursor():
    def handler(_):
        return httpx.Response(500, json={"error": "boom"})

    store = InMemorySyncStateStore()
    store.set_cursor("mercos", "products", "2026-01-01T00:00:00")

    outcome = await sync_resource(
        client=_client(handler), resource="products", store=store, writer=_Writer()
    )

    assert outcome.ok is False
    assert outcome.error_code == "adaptor_unavailable"
    assert store.get_cursor("mercos", "products") == "2026-01-01T00:00:00"


@pytest.mark.asyncio
async def test_rate_limit_is_reported_without_losing_position():
    def handler(_):
        return httpx.Response(429, json={"error": "Too Many Requests"},
                              headers={"Retry-After": "30"})

    store = InMemorySyncStateStore()
    outcome = await sync_resource(
        client=_client(handler), resource="products", store=store, writer=_Writer()
    )
    assert outcome.error_code == "rate_limited"
    assert outcome.retry_after == 30
    assert store.get_cursor("mercos", "products") is None


# --- idempotencia -----------------------------------------------------------


@pytest.mark.asyncio
async def test_reprocessing_the_same_page_is_idempotent():
    """Reexecutar a mesma pagina entrega o mesmo conjunto — sem duplicar."""
    page = _page([_row(1, "2026-01-01T00:00:00"), _row(2, "2026-01-02T00:00:00")])

    first = _Writer()
    await sync_resource(
        client=_client(_pages(page)), resource="products",
        store=InMemorySyncStateStore(), writer=first,
    )
    second = _Writer()
    await sync_resource(
        client=_client(_pages(page)), resource="products",
        store=InMemorySyncStateStore(), writer=second,
    )
    assert first.written == second.written == ["1", "2"]


@pytest.mark.asyncio
async def test_rows_without_identity_are_skipped_not_invented():
    """Linha sem `id` nao vira registro: inventar chave seria pior que descartar."""
    handler = _pages(_page([{"ultima_alteracao": "2026-01-01T00:00:00"}, _row(2, "2026-01-02T00:00:00")]))
    writer = _Writer()

    outcome = await sync_resource(
        client=_client(handler), resource="products",
        store=InMemorySyncStateStore(), writer=writer,
    )
    assert writer.written == ["2"]
    assert outcome.skipped == 1


@pytest.mark.asyncio
async def test_empty_page_is_a_successful_no_op():
    outcome = await sync_resource(
        client=_client(_pages(_page([]))), resource="products",
        store=InMemorySyncStateStore(), writer=_Writer(),
    )
    assert outcome.ok is True
    assert outcome.records == 0


# --- limites ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_budget_stops_a_catalog_that_never_ends():
    """Catalogo maior que o orcamento para no teto — e retoma na proxima execucao."""
    day = {"n": 0}

    def handler(request: httpx.Request):
        day["n"] += 1
        stamp = f"2026-01-{day['n']:02d}T00:00:00"
        return httpx.Response(200, json=_page([_row(day["n"], stamp)], next_cursor=stamp))

    store = InMemorySyncStateStore()
    outcome = await sync_resource(
        client=_client(handler), resource="products", store=store,
        writer=_Writer(), max_pages=3,
    )
    assert outcome.pages == 3
    assert outcome.stopped_reason == "page_budget"
    # Posicao confirmada: a proxima execucao continua daqui, sem repetir tudo.
    assert store.get_cursor("mercos", "products") == "2026-01-03T00:00:00"


@pytest.mark.asyncio
async def test_stalled_cursor_stops_instead_of_looping():
    """Adaptador diz "ha mais" mas o cursor nao anda: parar, nao girar."""
    handler = _pages(_page([_row(1, "2026-01-01T00:00:00")], next_cursor="2026-01-01T00:00:00"))
    store = InMemorySyncStateStore()
    store.set_cursor("mercos", "products", "2026-01-01T00:00:00")

    outcome = await sync_resource(
        client=_client(handler), resource="products", store=store,
        writer=_Writer(), max_pages=50,
    )
    assert outcome.stopped_reason == "cursor_stalled"
    assert outcome.pages == 1


@pytest.mark.asyncio
async def test_sync_never_calls_a_mutation():
    """Sync e leitura. Um POST aqui criaria dado no ERP do cliente."""
    methods = []

    def handler(request: httpx.Request):
        methods.append(request.method)
        return httpx.Response(200, json=_page([_row(1, "2026-01-01T00:00:00")]))

    await sync_resource(
        client=_client(handler), resource="products",
        store=InMemorySyncStateStore(), writer=_Writer(),
    )
    assert set(methods) == {"GET"}


def test_outcome_carries_no_payload():
    """O resultado vai para log/health: nao pode carregar dado de negocio."""
    outcome = SyncOutcome(ok=True, resource="products", pages=1, records=2)
    assert "payload" not in outcome.as_log()
    assert set(outcome.as_log()) <= {
        "resource", "ok", "pages", "records", "skipped",
        "error_code", "retry_after", "stopped_reason", "cursor_advanced",
    }
