"""Estado duravel do sync (`ai_commerce_sync_state`) e a nocao de PRONTO.

A distincao que este modulo existe para fazer:

    configurado  = URL + chave do adaptador validas
    PRONTO       = existe sync de products concluido com sucesso

Configurado nao implica pronto. Sem essa separacao, ligar as envs exporia
`search_products` sobre um indice nunca sincronizado: a busca voltaria vazia e o
agente leria isso como "nao temos esse produto" — uma negativa comercial falsa,
indistinguivel de um fato para quem esta do outro lado.

Catalogo vazio DEPOIS de um sync bem-sucedido e estado valido e continua pronto.
Nunca sincronizado e um estado diferente, e nao fica pronto.

A migration 023 ainda nao foi executada. Toda leitura aqui tolera a tabela
ausente: devolve "nao pronto" e segue. Tabela faltando nao pode derrubar webhook,
e tambem nao pode ser silenciada como "nao ha produtos".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PROVIDER_NAME = "mercos"
PRODUCTS_RESOURCE = "products"
TABLE = "public.ai_commerce_sync_state"


@dataclass(frozen=True)
class SyncState:
    """Leitura do estado de sync. `missing_table` distingue ausencia de vazio."""

    provider: str
    resource: str
    last_cursor: str | None = None
    last_success_at: datetime | None = None
    last_error_code: str | None = None
    consecutive_failures: int = 0
    total_records: int = 0
    missing_table: bool = False

    @property
    def ready(self) -> bool:
        """Houve ao menos um sync concluido com sucesso?

        Nao exige produtos: um catalogo vazio apos sync bem-sucedido e valido.
        """
        return not self.missing_table and self.last_success_at is not None

    def as_health(self) -> dict[str, Any]:
        """Projecao segura para /health: sem cursor, sem payload, sem segredo."""
        return {
            "ready": self.ready,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_error_code": self.last_error_code,
            "consecutive_failures": self.consecutive_failures,
            "state_table_missing": self.missing_table,
        }


def _is_missing_table(exc: Exception) -> bool:
    """A falha foi "tabela nao existe" (migration 023 pendente)?"""
    text = f"{type(exc).__name__}: {exc}".casefold()
    return "undefinedtable" in text or "does not exist" in text or "no such table" in text


class DatabaseSyncStateStore:
    """Estado de sync em `ai_commerce_sync_state`.

    Implementa o protocolo `SyncStateStore` do motor de sync e adiciona a
    leitura de readiness. Nao cria tabela: a migration e passo operacional
    explicito, nunca efeito colateral de um webhook.
    """

    def __init__(self, *, tenant_id: str = "newstore") -> None:
        self._tenant_id = tenant_id

    # --- protocolo do motor de sync ---------------------------------------

    def get_cursor(self, provider: str, resource: str) -> str | None:
        state = self.read(provider, resource)
        return state.last_cursor

    def set_cursor(self, provider: str, resource: str, cursor: str | None) -> None:
        """Confirma a posicao. Chamado so depois da pagina inteira gravada."""
        self._execute(
            f"""
            INSERT INTO {TABLE}
                (tenant_id, provider, resource, last_cursor, last_attempt_at, updated_at)
            VALUES (%(tenant)s, %(provider)s, %(resource)s, %(cursor)s, now(), now())
            ON CONFLICT (tenant_id, provider, resource) DO UPDATE
               SET last_cursor = EXCLUDED.last_cursor,
                   last_attempt_at = now(),
                   updated_at = now()
            """,
            {
                "tenant": self._tenant_id,
                "provider": provider,
                "resource": resource,
                "cursor": cursor,
            },
        )

    # --- readiness e registro de execucao ----------------------------------

    def read(self, provider: str = PROVIDER_NAME, resource: str = PRODUCTS_RESOURCE) -> SyncState:
        try:
            from ...db import get_conn

            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT last_cursor, last_success_at, last_error_code,
                               consecutive_failures, total_records
                          FROM {TABLE}
                         WHERE tenant_id = %(tenant)s
                           AND provider = %(provider)s
                           AND resource = %(resource)s
                        """,
                        {
                            "tenant": self._tenant_id,
                            "provider": provider,
                            "resource": resource,
                        },
                    )
                    row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001 - readiness nunca derruba o turno
            return SyncState(
                provider=provider,
                resource=resource,
                missing_table=_is_missing_table(exc),
            )

        if not row:
            return SyncState(provider=provider, resource=resource)
        return SyncState(
            provider=provider,
            resource=resource,
            last_cursor=row.get("last_cursor"),
            last_success_at=row.get("last_success_at"),
            last_error_code=row.get("last_error_code"),
            consecutive_failures=int(row.get("consecutive_failures") or 0),
            total_records=int(row.get("total_records") or 0),
        )

    def record_success(self, provider: str, resource: str, *, records: int, pages: int) -> None:
        """Marca o sync como concluido. E ISTO que torna o provider pronto."""
        self._execute(
            f"""
            INSERT INTO {TABLE}
                (tenant_id, provider, resource, last_success_at, last_attempt_at,
                 last_error_code, consecutive_failures, total_pages, total_records, updated_at)
            VALUES (%(tenant)s, %(provider)s, %(resource)s, now(), now(),
                    NULL, 0, %(pages)s, %(records)s, now())
            ON CONFLICT (tenant_id, provider, resource) DO UPDATE
               SET last_success_at = now(),
                   last_attempt_at = now(),
                   last_error_code = NULL,
                   consecutive_failures = 0,
                   total_pages = {TABLE}.total_pages + EXCLUDED.total_pages,
                   total_records = {TABLE}.total_records + EXCLUDED.total_records,
                   updated_at = now()
            """,
            {
                "tenant": self._tenant_id,
                "provider": provider,
                "resource": resource,
                "pages": pages,
                "records": records,
            },
        )

    def record_failure(self, provider: str, resource: str, *, error_code: str | None) -> None:
        """Registra a falha SEM apagar `last_success_at`.

        Um sync que falha depois de um sync bom nao invalida o catalogo: os
        dados continuam la e a politica de freshness decide, fato a fato, o que
        ainda pode ser afirmado. Apagar aqui seria jogar fora informacao boa por
        causa de uma falha de rede.
        """
        self._execute(
            f"""
            INSERT INTO {TABLE}
                (tenant_id, provider, resource, last_attempt_at, last_error_code,
                 consecutive_failures, updated_at)
            VALUES (%(tenant)s, %(provider)s, %(resource)s, now(), %(error)s, 1, now())
            ON CONFLICT (tenant_id, provider, resource) DO UPDATE
               SET last_attempt_at = now(),
                   last_error_code = EXCLUDED.last_error_code,
                   consecutive_failures = {TABLE}.consecutive_failures + 1,
                   updated_at = now()
            """,
            {
                "tenant": self._tenant_id,
                "provider": provider,
                "resource": resource,
                "error": error_code,
            },
        )

    def _execute(self, sql: str, params: dict[str, Any]) -> bool:
        try:
            from ...db import get_conn

            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
            return True
        except Exception:  # noqa: BLE001 - migration pendente nao derruba o turno
            return False


def product_sync_ready(store: Any | None = None, *, tenant_id: str = "newstore") -> bool:
    """O catalogo Mercos esta pronto para responder ao modelo?"""
    resolved = store or DatabaseSyncStateStore(tenant_id=tenant_id)
    try:
        return resolved.read(PROVIDER_NAME, PRODUCTS_RESOURCE).ready
    except Exception:  # noqa: BLE001
        return False
