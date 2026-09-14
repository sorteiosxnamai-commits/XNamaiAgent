"""Sync incremental do MercosAdaptor para o indice local.

A propriedade central deste modulo e a **atomicidade do cursor**:

    o cursor so avanca depois que a pagina INTEIRA foi gravada com sucesso.

Se ele avancasse item a item, uma falha no meio da pagina faria os registros
seguintes desaparecerem silenciosamente — a proxima execucao comecaria depois
deles e ninguem notaria a lacuna. Por isso a escrita da pagina e um passo unico
e o cursor e confirmado so no fim (`store.set_cursor`).

O motor e **agnostico de campos de negocio** de proposito: ele lida com
identidade (`id`) e incremental (`ultima_alteracao`), que sao contrato garantido
do adaptador, e delega a traducao ao `writer` injetado. Isso permite que a
logica de paginacao e cursor seja revisada e testada agora, sem depender do
schema de produto — que ainda nao pode ser mapeado sem inventar campos
(ver `docs/mercos_contract.md`, MERCOS_SCHEMA_DISCOVERY_BLOCKED).

Sync e LEITURA. Nenhum caminho aqui emite POST/PUT.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .client import MercosAdaptorClient, MercosAdaptorError
from .normalizer import PROVIDER_NAME, CommerceRecord, normalize_page

#: Teto de paginas por execucao. O adaptador pausa entre paginas e serializa as
#: chamadas; sem teto, um cursor que nao avanca viraria laco longo contra ele.
DEFAULT_MAX_PAGES = 50


class SyncStateStore(Protocol):
    """Onde a posicao confirmada do sync e guardada."""

    def get_cursor(self, provider: str, resource: str) -> str | None: ...

    def set_cursor(self, provider: str, resource: str, cursor: str | None) -> None: ...


class PageWriter(Protocol):
    """Persiste uma pagina normalizada. Deve ser idempotente (upsert por chave)."""

    async def write_page(self, resource: str, records: list[CommerceRecord]) -> int: ...


class InMemorySyncStateStore:
    """Store de teste/uso efemero. O store duravel usa `ai_commerce_sync_state`."""

    def __init__(self) -> None:
        self._cursors: dict[tuple[str, str], str | None] = {}

    def get_cursor(self, provider: str, resource: str) -> str | None:
        return self._cursors.get((provider, resource))

    def set_cursor(self, provider: str, resource: str, cursor: str | None) -> None:
        self._cursors[(provider, resource)] = cursor


@dataclass
class SyncOutcome:
    """Resultado de uma execucao. Vai para log e /health — sem dado de negocio."""

    ok: bool
    resource: str
    pages: int = 0
    records: int = 0
    skipped: int = 0
    error_code: str | None = None
    retry_after: float | None = None
    stopped_reason: str | None = None
    cursor_advanced: bool = False

    def as_log(self) -> dict[str, Any]:
        """Projecao segura: contadores e codigos, nunca payload."""
        return {
            "resource": self.resource,
            "ok": self.ok,
            "pages": self.pages,
            "records": self.records,
            "skipped": self.skipped,
            "error_code": self.error_code,
            "retry_after": self.retry_after,
            "stopped_reason": self.stopped_reason,
            "cursor_advanced": self.cursor_advanced,
        }


async def sync_resource(
    *,
    client: MercosAdaptorClient,
    resource: str,
    store: SyncStateStore,
    writer: PageWriter,
    max_pages: int = DEFAULT_MAX_PAGES,
    provider: str = PROVIDER_NAME,
) -> SyncOutcome:
    """Sincroniza um recurso a partir do cursor confirmado.

    Retorna sempre um `SyncOutcome` — falha nao levanta, porque quem chama
    (cron, health, tarefa manual) precisa registrar o motivo sem derrubar o
    processo. O que nunca acontece em falha e o cursor avancar.
    """
    outcome = SyncOutcome(ok=True, resource=resource)
    cursor = store.get_cursor(provider, resource)

    for _ in range(max(1, max_pages)):
        requested = cursor
        try:
            page = await client.list_resource(resource, changed_after=cursor)
        except MercosAdaptorError as exc:
            # Falha de leitura: posicao intacta, pagina sera refeita.
            outcome.ok = False
            outcome.error_code = exc.code
            outcome.retry_after = exc.retry_after
            return outcome

        records = normalize_page(resource, page.data)
        outcome.skipped += len(page.data) - len(records)

        if records:
            try:
                await writer.write_page(resource, records)
            except Exception:  # noqa: BLE001 - o motivo vai no codigo, nao no log
                # Escrita parcial: NAO confirmar cursor. A pagina inteira volta.
                outcome.ok = False
                outcome.error_code = "write_failed"
                return outcome
            outcome.records += len(records)

        outcome.pages += 1

        # Pagina inteira gravada: so agora a posicao e confirmada.
        confirmed = page.next_cursor or page.page_cursor
        if confirmed and confirmed != requested:
            store.set_cursor(provider, resource, confirmed)
            cursor = confirmed
            outcome.cursor_advanced = True

        if not page.has_more:
            return outcome

        if cursor == requested:
            # O adaptador diz que ha mais, mas a posicao nao andou. Continuar
            # reenviaria a mesma pagina para sempre — parar e o comportamento
            # seguro. Comparacao contra `requested` (o cursor DA REQUISICAO),
            # nunca contra o valor recem-atribuido a partir da resposta.
            outcome.stopped_reason = "cursor_stalled"
            return outcome

    outcome.stopped_reason = "page_budget"
    return outcome
