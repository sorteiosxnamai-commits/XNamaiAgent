"""Entrypoint operacional do sync de produtos.

O motor (`sync.py`) e puro e testavel; faltava quem o chamasse. Este modulo faz
essa ligacao: monta client + store + writer a partir da configuracao, roda o
sync e registra sucesso/falha no estado duravel — e e o registro do SUCESSO que
torna o provider pronto para o modelo.

Nunca cria tabela, nunca executa migration. Com a 023 pendente, o sync devolve
`state_table_missing` em vez de derrubar a chamada.
"""

from __future__ import annotations

from typing import Any

from .sync import DEFAULT_MAX_PAGES, SyncOutcome, sync_resource
from .sync_state import PRODUCTS_RESOURCE, PROVIDER_NAME


async def run_product_sync(
    *,
    settings: Any | None = None,
    client: Any | None = None,
    store: Any | None = None,
    writer: Any | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """Executa um ciclo de sync de produtos e devolve um resumo seguro.

    Tudo injetavel para teste; em producao os componentes vem da configuracao.
    O retorno nunca carrega cursor, payload ou segredo — so contadores e codigos.
    """
    from ...config import get_settings

    resolved_settings = settings or get_settings()

    if not getattr(resolved_settings, "mercos_adaptor_configured", False):
        return {"ok": False, "error": "commerce_adaptor_not_configured"}
    if not getattr(resolved_settings, "database_url", ""):
        return {"ok": False, "error": "database_not_configured"}

    # Tenant COMERCIAL, nunca o da persona. Fonte unica: Settings.
    tenant_id = resolved_settings.commerce_tenant_id

    if client is None:
        from .client import MercosAdaptorClient

        client = MercosAdaptorClient(
            base_url=resolved_settings.mercos_adaptor_url,
            api_key=resolved_settings.mercos_adaptor_api_key,
            timeout_seconds=getattr(resolved_settings, "mercos_adaptor_timeout_seconds", 90.0),
        )
    if store is None:
        from .sync_state import DatabaseSyncStateStore

        store = DatabaseSyncStateStore(tenant_id=tenant_id)
    if writer is None:
        from .catalog import ProductPageWriter
        from .catalog_index_reader import CatalogIndexProductReader

        writer = ProductPageWriter(CatalogIndexProductReader(), tenant_id=tenant_id)

    # Migration pendente: recusar de forma explicita. Rodar o sync sem onde
    # gravar o cursor faria a proxima execucao recomecar do zero para sempre.
    state = store.read(PROVIDER_NAME, PRODUCTS_RESOURCE)
    if getattr(state, "missing_table", False):
        return {
            "ok": False,
            "error": "sync_state_table_missing",
            "hint": "aplicar sql/023_mercos_sync_state.sql",
        }

    outcome: SyncOutcome = await sync_resource(
        client=client,
        resource=PRODUCTS_RESOURCE,
        store=store,
        writer=writer,
        max_pages=max_pages,
        provider=PROVIDER_NAME,
    )

    if outcome.ok:
        store.record_success(
            PROVIDER_NAME, PRODUCTS_RESOURCE,
            records=outcome.records, pages=outcome.pages,
        )
    else:
        # Falha NAO apaga `last_success_at`: o catalogo anterior continua valido
        # e a politica de freshness decide, fato a fato, o que pode ser afirmado.
        store.record_failure(PROVIDER_NAME, PRODUCTS_RESOURCE, error_code=outcome.error_code)

    return {"ok": outcome.ok, **outcome.as_log()}
