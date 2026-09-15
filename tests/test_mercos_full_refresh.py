"""Full refresh: reconstruir o indice sem mover a posicao do sync incremental.

Por que a operacao existe. O sync normal comeca no cursor confirmado. Quando a
correcao muda o FORMATO do que se grava — foi o caso da semantica de
`freshness_at` e do payload persistido — reexecutar o incremental nao repara
nada: produto ativo que ninguem alterou na origem nunca e revisitado, e fica no
indice com o snapshot velho. Idempotencia do upsert so ajuda quem volta a ser
visitado.

E o que a operacao nao pode fazer. Ela nao pode resetar o `last_cursor` duravel:
perder essa posicao faria o proximo incremental reprocessar o catalogo inteiro e
— pior — perder o delta ocorrido durante a reconstrucao. Por isso o cursor da
varredura vive so em memoria, e o duravel nao e tocado nem no sucesso nem na
falha.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from app.commerce.mercos.catalog import ProductPageWriter
from app.commerce.mercos.client import MercosAdaptorClient
from app.commerce.mercos.sync_runner import run_product_full_refresh
from app.commerce.mercos.sync_state import PRODUCTS_RESOURCE, PROVIDER_NAME, SyncState
from tests.fixtures import mercos_payloads as fx

TENANT = "xnamai"
CURSOR_DE_PRODUCAO = "CURSOR-PRODUCAO"
DATA_NA_ORIGEM = "2025-11-20T08:56:18"


class _Settings:
    mercos_adaptor_configured = True
    mercos_adaptor_url = "https://a.example.com"
    mercos_adaptor_api_key = "k"
    mercos_adaptor_timeout_seconds = 5.0
    commerce_tenant_id = TENANT
    database_url = "postgresql://exemplo/db"


class _StoreDuravel:
    """Duplo do `DatabaseSyncStateStore`. Registra QUALQUER tentativa de escrita."""

    def __init__(self, cursor: str | None = CURSOR_DE_PRODUCAO, *, missing_table=False):
        self.cursor = cursor
        self.missing_table = missing_table
        self.escritas: list[str] = []
        self.sucesso_registrado = 0
        self.falha_registrada = 0

    def read(self, provider, resource):
        return SyncState(
            provider, resource,
            last_cursor=self.cursor,
            last_success_at=datetime(2026, 9, 15, 13, 17, tzinfo=timezone.utc),
            missing_table=self.missing_table,
        )

    def get_cursor(self, provider, resource):
        return self.cursor

    def set_cursor(self, provider, resource, cursor):
        self.escritas.append("set_cursor")
        self.cursor = cursor

    def record_success(self, provider, resource, **kwargs):
        self.escritas.append("record_success")
        self.sucesso_registrado += 1

    def record_failure(self, provider, resource, **kwargs):
        self.escritas.append("record_failure")
        self.falha_registrada += 1


class _Writer:
    """Captura o que teria sido gravado, com falha opcional."""

    def __init__(self, *, explodir=False):
        self.explodir = explodir
        self.paginas: list[list[Any]] = []

    async def write_page(self, resource, records):
        if self.explodir:
            raise RuntimeError("falha de upsert")
        self.paginas.append(list(records))


class _Cliente:
    """Serve paginas em sequencia e registra o `changed_after` de cada chamada."""

    def __init__(self, paginas: list[dict[str, Any]]):
        self._paginas = paginas
        self.cursores_pedidos: list[str | None] = []

    async def list_resource(self, resource, *, changed_after=None, **kwargs):
        self.cursores_pedidos.append(changed_after)
        indice = min(len(self.cursores_pedidos) - 1, len(self._paginas) - 1)
        bruto = self._paginas[indice]

        class _Pagina:
            data = bruto["data"]
            page_cursor = bruto.get("pageCursor")
            next_cursor = bruto.get("nextCursor")
            has_more = bool(bruto.get("nextCursor"))

        return _Pagina()


def _pagina(linhas, next_cursor=None):
    return {
        "data": linhas,
        "pageCursor": linhas[-1]["ultima_alteracao"] if linhas else None,
        "nextCursor": next_cursor,
    }


async def _rodar(*, paginas, store=None, writer=None, **kwargs):
    store = store if store is not None else _StoreDuravel()
    writer = writer if writer is not None else _Writer()
    cliente = _Cliente(paginas)
    resultado = await run_product_full_refresh(
        settings=_Settings(), client=cliente, writer=writer, state_store=store, **kwargs
    )
    return resultado, cliente, store, writer


# === 1. comeca do zero, mesmo com cursor de producao gravado ===============


@pytest.mark.asyncio
async def test_full_refresh_starts_from_a_null_cursor():
    """A diferenca inteira em relacao ao incremental esta na primeira chamada."""
    store = _StoreDuravel(cursor=CURSOR_DE_PRODUCAO)
    _, cliente, _, _ = await _rodar(paginas=[_pagina([fx.product(id=1)])], store=store)
    assert cliente.cursores_pedidos[0] is None, "full refresh partiu do cursor duravel"


@pytest.mark.asyncio
async def test_the_incremental_cursor_would_have_been_used_as_the_start():
    """Contraste explicito: o runner incremental parte do cursor gravado."""
    from app.commerce.mercos.sync import sync_resource

    store = _StoreDuravel(cursor=CURSOR_DE_PRODUCAO)
    cliente = _Cliente([_pagina([fx.product(id=1)])])
    await sync_resource(
        client=cliente, resource=PRODUCTS_RESOURCE, store=store, writer=_Writer()
    )
    assert cliente.cursores_pedidos[0] == CURSOR_DE_PRODUCAO


# === 2. o cursor duravel nao e tocado ======================================


@pytest.mark.asyncio
async def test_full_refresh_never_writes_to_the_durable_store():
    store = _StoreDuravel(cursor=CURSOR_DE_PRODUCAO)
    resultado, _, store, _ = await _rodar(
        paginas=[_pagina([fx.product(id=1)])], store=store
    )
    assert resultado["ok"] is True
    assert store.escritas == [], f"full refresh escreveu no store duravel: {store.escritas}"
    assert store.cursor == CURSOR_DE_PRODUCAO


@pytest.mark.asyncio
async def test_full_refresh_does_not_record_success():
    """Prontidao continua sendo assunto do incremental — nada e fabricado aqui."""
    _, _, store, _ = await _rodar(paginas=[_pagina([fx.product(id=1)])])
    assert store.sucesso_registrado == 0
    assert store.falha_registrada == 0


# === 3. o cursor interno avanca em memoria entre paginas ===================


@pytest.mark.asyncio
async def test_the_in_memory_cursor_advances_between_pages():
    paginas = [
        _pagina([fx.product(id=1, ultima_alteracao="2026-01-01T00:00:00")], next_cursor="P2"),
        _pagina([fx.product(id=2, ultima_alteracao="2026-02-01T00:00:00")]),
    ]
    resultado, cliente, store, _ = await _rodar(paginas=paginas)
    assert cliente.cursores_pedidos == [None, "P2"], "a varredura nao paginou"
    assert resultado["pages"] == 2
    assert resultado["records"] == 2
    assert store.cursor == CURSOR_DE_PRODUCAO, "paginar vazou para o store duravel"


@pytest.mark.asyncio
async def test_a_complete_sweep_is_reported_as_complete():
    resultado, _, _, _ = await _rodar(paginas=[_pagina([fx.product(id=1)])])
    assert resultado["complete"] is True
    assert resultado["mode"] == "full_refresh"


@pytest.mark.asyncio
async def test_a_truncated_sweep_is_not_reported_as_complete():
    """Bater no teto de paginas nao e reconstrucao: quem opera precisa saber."""
    paginas = [_pagina([fx.product(id=n)], next_cursor=f"P{n}") for n in range(1, 6)]
    resultado, _, _, _ = await _rodar(paginas=paginas, max_pages=2)
    assert resultado["ok"] is True
    assert resultado["complete"] is False
    assert resultado["stopped_reason"] == "page_budget"


# === 4. produto antigo na origem e reconfirmado agora ======================


@pytest.mark.asyncio
async def test_a_product_unchanged_since_the_first_sync_is_rewritten():
    """O caso que motivou a operacao: `ultima_alteracao` de 2025, snapshot de hoje."""
    from app.commerce.mercos.catalog import product_to_index_fields

    writer = _Writer()
    antes = datetime.now(timezone.utc)
    await _rodar(
        paginas=[_pagina([
            fx.product(
                id=1, nome="Produto Exemplo", ativo=True, excluido=False,
                ultima_alteracao=DATA_NA_ORIGEM, observacoes="NAO PERSISTIR",
            )
        ])],
        writer=writer,
    )

    registro = writer.paginas[0][0]
    from app.commerce.mercos.normalizer import normalize_product

    campos = product_to_index_fields(normalize_product(registro.raw))
    assert antes <= campos["freshness_at"] <= datetime.now(timezone.utc)
    assert campos["payload"]["ultima_alteracao"] == DATA_NA_ORIGEM
    assert campos["payload"]["nome"] == "Produto Exemplo"
    assert campos["payload"]["ativo"] is True
    assert campos["payload"]["excluido"] is False
    assert "observacoes" not in campos["payload"]


# === 5. falha de escrita ===================================================


@pytest.mark.asyncio
async def test_a_write_failure_stops_the_refresh():
    resultado, _, store, _ = await _rodar(
        paginas=[_pagina([fx.product(id=1)])], writer=_Writer(explodir=True)
    )
    assert resultado["ok"] is False
    assert resultado["error_code"] == "write_failed"
    assert resultado["complete"] is False


@pytest.mark.asyncio
async def test_a_write_failure_leaves_the_durable_cursor_intact():
    store = _StoreDuravel(cursor=CURSOR_DE_PRODUCAO)
    await _rodar(
        paginas=[_pagina([fx.product(id=1)])], store=store, writer=_Writer(explodir=True)
    )
    assert store.cursor == CURSOR_DE_PRODUCAO
    assert store.escritas == []


# === 6. lifecycle inalterado ===============================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ativo,excluido,esperado",
    [(True, False, "index"), (False, False, "remove"), (True, True, "remove"),
     (None, False, "ignore_unknown_state"), (True, None, "ignore_unknown_state")],
)
async def test_the_lifecycle_is_identical_in_a_full_refresh(ativo, excluido, esperado):
    from app.commerce.mercos.catalog import decide
    from app.commerce.mercos.normalizer import normalize_product

    writer = _Writer()
    await _rodar(
        paginas=[_pagina([fx.product(id=1, ativo=ativo, excluido=excluido)])],
        writer=writer,
    )
    registro = writer.paginas[0][0]
    assert decide(normalize_product(registro.raw)).value == esperado


@pytest.mark.asyncio
async def test_the_real_product_writer_is_reused():
    """Nenhum caminho de escrita paralelo: e o mesmo `ProductPageWriter`."""

    class _Indice:
        def __init__(self):
            self.itens = {}

        def upsert_products(self, tenant_id, products):
            for p in products:
                self.itens[p.external_id] = p
            return len(products)

        def delete_products(self, tenant_id, product_ids):
            return 0

    indice = _Indice()
    await _rodar(
        paginas=[_pagina([fx.product(id=7, ativo=True, excluido=False)])],
        writer=ProductPageWriter(indice, tenant_id=TENANT),
    )
    assert "7" in indice.itens


# === 7. guardas de configuracao ============================================


@pytest.mark.asyncio
async def test_without_the_state_table_the_refresh_refuses():
    """Sem a 023 o passo seguinte nao roda: reparar so metade seria enganoso."""
    store = _StoreDuravel(missing_table=True)
    resultado, _, _, _ = await _rodar(paginas=[_pagina([fx.product(id=1)])], store=store)
    assert resultado["ok"] is False
    assert resultado["error"] == "sync_state_table_missing"


@pytest.mark.asyncio
async def test_without_an_adaptor_the_refresh_refuses():
    class _SemAdaptador(_Settings):
        mercos_adaptor_configured = False

    resultado = await run_product_full_refresh(settings=_SemAdaptador())
    assert resultado == {"ok": False, "error": "commerce_adaptor_not_configured"}


@pytest.mark.asyncio
async def test_without_a_database_the_refresh_refuses():
    class _SemBanco(_Settings):
        database_url = ""

    resultado = await run_product_full_refresh(settings=_SemBanco())
    assert resultado == {"ok": False, "error": "database_not_configured"}


@pytest.mark.asyncio
async def test_the_result_never_leaks_a_cursor_or_payload():
    resultado, _, _, _ = await _rodar(paginas=[_pagina([fx.product(id=1)])])
    assert "last_cursor" not in resultado
    assert "payload" not in resultado
    assert "data" not in resultado


# === 8. o incremental nao mudou ============================================


def _metodos_chamados(funcao) -> set[str]:
    """Nomes de metodo efetivamente CHAMADOS — docstring nao conta."""
    import ast
    import inspect
    import textwrap

    arvore = ast.parse(textwrap.dedent(inspect.getsource(funcao)))
    return {
        no.func.attr
        for no in ast.walk(arvore)
        if isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
    }


def test_the_incremental_runner_still_records_success():
    """Garantia de que o refresh nao virou substituto do sync normal."""
    from app.commerce.mercos import sync_runner

    incremental = _metodos_chamados(sync_runner.run_product_sync)
    assert {"record_success", "record_failure"} <= incremental

    refresh = _metodos_chamados(sync_runner.run_product_full_refresh)
    assert "record_success" not in refresh
    assert "record_failure" not in refresh
    assert "set_cursor" not in refresh


def test_the_full_refresh_paginates_with_an_in_memory_store():
    import inspect

    from app.commerce.mercos import sync_runner

    corpo = "\n".join(
        linha for linha in inspect.getsource(sync_runner.run_product_full_refresh).splitlines()
        if not linha.strip().startswith("#")
    ).split('"""')[-1]
    assert "InMemorySyncStateStore()" in corpo
