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


async def run_configured_customer_sync() -> dict[str, Any]:
    """Update the private customer index through the active provider."""
    try:
        from .provider import get_commerce_provider

        provider = get_commerce_provider()
    except Exception:
        return {"ok": False, "error": "commerce_provider_unavailable"}
    runner = getattr(provider, "run_customer_sync", None)
    if not callable(runner):
        return {"ok": False, "error": "sync_not_supported_by_provider"}
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
