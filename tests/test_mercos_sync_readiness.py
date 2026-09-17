"""Configurado nao e pronto: o gate que separa os dois.

O risco que este arquivo existe para impedir: ligar as envs do adaptador exporia
`search_products` sobre um indice nunca sincronizado. A busca voltaria vazia e o
agente leria isso como "nao temos esse produto" — uma negativa comercial falsa
que, para o cliente do outro lado, e indistinguivel de um fato.

Nenhum teste toca banco, migration ou Mercos.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.commerce.mercos.client import MercosAdaptorClient
from app.commerce.mercos.provider import MercosCommerceProvider
from app.commerce.mercos.sync_state import SyncState

NOW = datetime.now(timezone.utc)


class _StateStore:
    """Store em memoria que imita os estados possiveis da tabela 023."""

    def __init__(self, state: SyncState | None = None, *, explode: bool = False):
        self._state = state
        self._explode = explode
        self.successes: list[dict] = []
        self.failures: list[dict] = []

    def read(self, provider, resource):
        if self._explode:
            raise RuntimeError("banco indisponivel")
        return self._state or SyncState(provider=provider, resource=resource)

    def get_cursor(self, provider, resource):
        return self.read(provider, resource).last_cursor

    def set_cursor(self, provider, resource, cursor):
        self._state = SyncState(provider=provider, resource=resource, last_cursor=cursor)

    def record_success(self, provider, resource, *, records, pages):
        self.successes.append({"records": records, "pages": pages})
        self._state = SyncState(provider=provider, resource=resource, last_success_at=NOW)

    def record_failure(self, provider, resource, *, error_code):
        self.failures.append({"error_code": error_code})


#: Tenant de teste explicito: o provider exige um e nao inventa.
TENANT = "tenant-de-teste"


def _provider(*, index=None, sync_state=None):
    return MercosCommerceProvider(
        MercosAdaptorClient(base_url="https://a.example.com", api_key="k", timeout_seconds=5),
        index=index,
        tenant_id=TENANT,
        sync_state=sync_state,
    )


SYNCED = SyncState(provider="mercos", resource="products", last_success_at=NOW)


# --- a matriz do gate -------------------------------------------------------


def test_no_index_means_no_tools():
    """Sem indice local nao ha de onde ler: nenhuma tool."""
    provider = _provider(index=None, sync_state=_StateStore(SYNCED))
    assert provider.available is False
    assert provider.llm_capabilities == frozenset()


def test_no_sync_state_means_no_tools():
    provider = _provider(index=object(), sync_state=None)
    assert provider.available is False
    assert provider.llm_capabilities == frozenset()


def test_missing_migration_means_no_tools_not_an_empty_catalog():
    """Tabela 023 ausente nao pode ser lida como "nao ha produtos"."""
    state = SyncState(provider="mercos", resource="products", missing_table=True)
    provider = _provider(index=object(), sync_state=_StateStore(state))
    assert provider.sync_ready is False
    assert provider.available is False
    assert state.ready is False


def test_never_synced_means_no_tools():
    """Linha inexistente na tabela: configurado, porem nunca sincronizado."""
    provider = _provider(index=object(), sync_state=_StateStore(SyncState("mercos", "products")))
    assert provider.sync_ready is False
    assert provider.available is False


def test_database_failure_degrades_to_not_ready_without_raising():
    """Readiness nunca derruba o turno — na duvida, nao expoe."""
    provider = _provider(index=object(), sync_state=_StateStore(explode=True))
    assert provider.sync_ready is False
    assert provider.available is False


def test_successful_sync_opens_the_three_tools():
    provider = _provider(index=object(), sync_state=_StateStore(SYNCED))
    assert provider.sync_ready is True
    assert provider.available is True
    assert provider.llm_capabilities == frozenset(
        {"search_products", "get_product", "check_inventory"}
    )


def test_empty_catalog_after_a_successful_sync_is_still_ready():
    """Catalogo vazio e resposta valida; nunca sincronizado e outra coisa."""
    state = SyncState(provider="mercos", resource="products", last_success_at=NOW, total_records=0)
    assert state.ready is True
    assert _provider(index=object(), sync_state=_StateStore(state)).available is True


def test_a_later_failure_does_not_close_the_tools():
    """Falha posterior nao apaga o catalogo — freshness governa cada fato."""
    state = SyncState(
        provider="mercos",
        resource="products",
        last_success_at=NOW - timedelta(hours=3),
        last_error_code="rate_limited",
        consecutive_failures=2,
    )
    provider = _provider(index=object(), sync_state=_StateStore(state))
    assert provider.available is True


# --- projecao para o /health ------------------------------------------------


def test_health_projection_carries_no_cursor_or_secret():
    state = SyncState(
        provider="mercos",
        resource="products",
        last_cursor="2026-03-01T10:00:00",
        last_success_at=NOW,
    )
    health = state.as_health()
    assert set(health) == {
        "ready", "last_success_at", "last_error_code",
        "consecutive_failures", "state_table_missing",
    }
    assert "2026-03-01T10:00:00" not in str(health)


def test_health_distinguishes_missing_table_from_never_synced():
    missing = SyncState("mercos", "products", missing_table=True).as_health()
    never = SyncState("mercos", "products").as_health()
    assert missing["state_table_missing"] is True
    assert never["state_table_missing"] is False
    assert missing["ready"] is never["ready"] is False


# --- o runner operacional ---------------------------------------------------


@pytest.mark.asyncio
async def test_runner_refuses_when_the_state_table_is_missing():
    """Sem onde gravar o cursor, cada execucao recomecaria do zero para sempre."""
    from app.commerce.mercos.sync_runner import run_product_sync

    class _Settings:
        mercos_adaptor_configured = True
        mercos_adaptor_url = "https://a.example.com"
        mercos_adaptor_api_key = "k"
        mercos_adaptor_timeout_seconds = 5
        database_url = "postgresql://x/y"
        # O tenant da persona fica aqui de proposito: o runner tem de IGNORA-LO
        # e usar o comercial. Se um dia voltar a le-lo, estes testes quebram.
        agent_persona_tenant_id = "xnamai"
        commerce_tenant_id = TENANT

    store = _StateStore(SyncState("mercos", "products", missing_table=True))
    result = await run_product_sync(settings=_Settings(), store=store, client=object(), writer=object())
    assert result["ok"] is False
    assert result["error"] == "sync_state_table_missing"
    assert store.successes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured,database,expected",
    [
        (False, "postgresql://x/y", "commerce_adaptor_not_configured"),
        (True, "", "database_not_configured"),
    ],
)
async def test_runner_refuses_without_configuration(configured, database, expected):
    from app.commerce.mercos.sync_runner import run_product_sync

    class _Settings:
        mercos_adaptor_configured = configured
        mercos_adaptor_url = "https://a.example.com"
        mercos_adaptor_api_key = "k"
        database_url = database
        # O tenant da persona fica aqui de proposito: o runner tem de IGNORA-LO
        # e usar o comercial. Se um dia voltar a le-lo, estes testes quebram.
        agent_persona_tenant_id = "xnamai"
        commerce_tenant_id = TENANT

    result = await run_product_sync(settings=_Settings())
    assert result["ok"] is False
    assert result["error"] == expected


@pytest.mark.asyncio
async def test_successful_run_records_the_success_that_opens_the_tools():
    from app.commerce.mercos.sync_runner import run_product_sync

    class _Settings:
        mercos_adaptor_configured = True
        mercos_adaptor_url = "https://a.example.com"
        mercos_adaptor_api_key = "k"
        mercos_adaptor_timeout_seconds = 5
        database_url = "postgresql://x/y"
        # O tenant da persona fica aqui de proposito: o runner tem de IGNORA-LO
        # e usar o comercial. Se um dia voltar a le-lo, estes testes quebram.
        agent_persona_tenant_id = "xnamai"
        commerce_tenant_id = TENANT

    def handler(_):
        return httpx.Response(200, json={
            "resource": "products", "count": 1,
            "pageCursor": "2026-03-01T10:00:00", "nextCursor": None,
            "data": [{"id": 1, "ultima_alteracao": "2026-03-01T10:00:00"}],
        })

    class _Writer:
        async def write_page(self, resource, records):
            return len(records)

    store = _StateStore(SyncState("mercos", "products"))
    client = MercosAdaptorClient(
        base_url="https://a.example.com", api_key="k", timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    result = await run_product_sync(
        settings=_Settings(), client=client, store=store, writer=_Writer()
    )
    assert result["ok"] is True
    assert store.successes == [{"records": 1, "pages": 1}]
    assert store.failures == []


@pytest.mark.asyncio
async def test_failed_run_records_failure_without_erasing_the_previous_success():
    from app.commerce.mercos.sync_runner import run_product_sync

    class _Settings:
        mercos_adaptor_configured = True
        mercos_adaptor_url = "https://a.example.com"
        mercos_adaptor_api_key = "k"
        mercos_adaptor_timeout_seconds = 5
        database_url = "postgresql://x/y"
        # O tenant da persona fica aqui de proposito: o runner tem de IGNORA-LO
        # e usar o comercial. Se um dia voltar a le-lo, estes testes quebram.
        agent_persona_tenant_id = "xnamai"
        commerce_tenant_id = TENANT

    def handler(_):
        return httpx.Response(500, json={"error": "boom"})

    class _Writer:
        async def write_page(self, resource, records):  # pragma: no cover
            raise AssertionError("nao deve gravar")

    previous = SyncState("mercos", "products", last_success_at=NOW - timedelta(hours=2))
    store = _StateStore(previous)
    client = MercosAdaptorClient(
        base_url="https://a.example.com", api_key="k", timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    result = await run_product_sync(
        settings=_Settings(), client=client, store=store, writer=_Writer()
    )
    assert result["ok"] is False
    assert store.failures == [{"error_code": "adaptor_unavailable"}]
    assert store.successes == []


def test_sync_result_never_leaks_payload_or_cursor():
    from app.commerce.mercos.sync import SyncOutcome

    log = SyncOutcome(ok=True, resource="products", pages=2, records=9).as_log()
    assert "cursor" not in str(log).replace("cursor_advanced", "")
    assert "payload" not in log
