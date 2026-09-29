"""Fachada neutra de saude e operacao comercial.

`api/index.py` nao pode conhecer fornecedor: ele pergunta ao provider ativo, e
cada provider projeta a propria prontidao. Trocar de fornecedor nao deve exigir
editar a camada HTTP.
"""

from __future__ import annotations

from typing import Any


def commerce_sync_health() -> dict[str, Any]:
    """Prontidao do provider ativo, ou vazio quando ele nao reporta nada."""
    try:
        from .provider import get_commerce_provider

        provider = get_commerce_provider()
    except Exception:  # noqa: BLE001 - health nunca derruba o servico
        return {}
    projection = getattr(provider, "sync_health", None)
    if not callable(projection):
        return {}
    try:
        result = projection()
    except Exception:  # noqa: BLE001
        return {}
    return result if isinstance(result, dict) else {}


async def run_configured_product_sync() -> dict[str, Any]:
    """Dispara o sync de catalogo do provider ativo.

    Provider sem sync (o Null, por exemplo) responde de forma explicita em vez
    de fingir sucesso — "nada a sincronizar" e diferente de "sincronizado".
    """
    try:
        from .provider import get_commerce_provider

        provider = get_commerce_provider()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "commerce_provider_unavailable"}

    runner = getattr(provider, "run_product_sync", None)
    if not callable(runner):
        return {
            "ok": False,
            "error": "sync_not_supported_by_provider",
            "provider": getattr(provider, "name", "unknown"),
        }
    return await runner()


#: Session-scoped advisory lock key for the customer index sync. Any fixed
#: string works — hashtextextended folds it into the lock id; only its
#: uniqueness among the app's other lock keys matters.
_CUSTOMER_SYNC_LOCK_KEY = "commerce.customer_index.sync"


async def run_configured_customer_sync() -> dict[str, Any]:
    """Update the private customer index through the active provider.

    Serialised by a non-blocking Postgres advisory lock held for the whole
    call (not just one query): a cron tick that lands while a previous sync
    is still running skips instead of racing it. No database configured (or
    a lock-check failure) falls back to running unlocked — the provider's own
    per-document creation lock stays the real safety net either way; this is
    only about not doing duplicate paging work.
    """
    try:
        from .provider import get_commerce_provider

        provider = get_commerce_provider()
    except Exception:
        return {"ok": False, "error": "commerce_provider_unavailable"}
    runner = getattr(provider, "run_customer_sync", None)
    if not callable(runner):
        return {"ok": False, "error": "sync_not_supported_by_provider"}

    from ..config import get_settings

    settings = get_settings()
    if not settings.database_url:
        return await runner()

    import psycopg

    from ..db import get_conn

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (_CUSTOMER_SYNC_LOCK_KEY,),
                )
                acquired = bool(cur.fetchone()[0])
            if not acquired:
                return {"ok": False, "error": "sync_already_running"}
            try:
                return await runner()
            finally:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (_CUSTOMER_SYNC_LOCK_KEY,),
                    )
    except psycopg.Error:
        # Lock bookkeeping itself failed (not the sync): don't block a
        # legitimate sync attempt over that — run it unlocked this once.
        return await runner()


async def run_configured_product_full_refresh() -> dict[str, Any]:
    """Reconstroi o catalogo do provider ativo, sem mover o cursor incremental.

    Operacao administrativa de reparo: existe para quando o FORMATO do que foi
    gravado mudou, caso em que reexecutar o sync incremental nao alcanca as
    linhas que ninguem alterou na origem.
    """
    try:
        from .provider import get_commerce_provider

        provider = get_commerce_provider()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "commerce_provider_unavailable"}

    runner = getattr(provider, "run_product_full_refresh", None)
    if not callable(runner):
        return {
            "ok": False,
            "error": "full_refresh_not_supported_by_provider",
            "provider": getattr(provider, "name", "unknown"),
        }
    return await runner()
