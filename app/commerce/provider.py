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

    def sync_health(self) -> dict[str, Any]:
        """Sem fonte configurada: nada pronto, e isso e dito explicitamente."""
        return {
            "commerce_adaptor_configured": False,
            "mercos_product_sync_ready": False,
            "mercos_product_sync_last_success_at": None,
        }
    #: Nao ha fonte comercial: o modelo NAO pode receber tools comerciais.
    #: Gating generico por provider, nunca por env de fornecedor.
    available = False

    async def list_payment_conditions(self) -> list[Any]:
        """Sem fonte comercial nao ha condicoes. Vazio, nunca excecao.

        A revisao entao bloqueia por "nenhuma condicao ativa", que e um estado
        tratado — em vez de o turno quebrar por falta de fornecedor.
        """
        return []

    async def execute(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise CommerceUnavailableError(capability)


_provider: CommerceProvider | None = None


def _build_configured_provider() -> CommerceProvider:
    """Provider a partir da configuracao. Sem fallback para fornecedor legado.

    So existem dois desfechos: adaptador comercial configurado -> provider real;
    nada configurado -> Null. Nunca Tray, nunca NewStore, nunca Mercos direto.
    """
    from ..config import get_settings

    settings = get_settings()
    if getattr(settings, "mercos_adaptor_configured", False):
        from .mercos.provider import build_mercos_provider

        return build_mercos_provider(settings)
    return NullCommerceProvider()


def get_commerce_provider() -> CommerceProvider:
    global _provider
    if _provider is None:
        _provider = _build_configured_provider()
    return _provider


def set_commerce_provider(provider: CommerceProvider) -> None:
    """Ponto de plug da Parte 2 (MercosCommerceProvider)."""
    global _provider
    _provider = provider


def reset_commerce_provider() -> None:
    global _provider
    _provider = None
