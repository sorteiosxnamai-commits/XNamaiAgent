from __future__ import annotations

from typing import Any

from .contracts import CommerceProvider
from .errors import CommerceUnavailableError


class NullCommerceProvider:
    """Provider ativo da Parte 1: nenhuma fonte comercial configurada.

    Falha de modo explícito. Nunca consulta um provider legado e nunca
    inventa dados comerciais.
    """

    name = "null"
    #: Nao ha fonte comercial: o modelo NAO pode receber tools comerciais.
    #: Gating generico por provider, nunca por env de fornecedor.
    available = False

    async def execute(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise CommerceUnavailableError(capability)


_provider: CommerceProvider | None = None


def get_commerce_provider() -> CommerceProvider:
    global _provider
    if _provider is None:
        _provider = NullCommerceProvider()
    return _provider


def set_commerce_provider(provider: CommerceProvider) -> None:
    """Ponto de plug da Parte 2 (MercosCommerceProvider)."""
    global _provider
    _provider = provider


def reset_commerce_provider() -> None:
    global _provider
    _provider = None
