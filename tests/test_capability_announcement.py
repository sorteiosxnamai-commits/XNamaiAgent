"""O agente nao pode oferecer o que nao consegue fazer naquele turno.

Em producao, com a Mercos ativa e SOMENTE tres tools expostas
(`search_products`, `get_product`, `check_inventory`), o agente respondeu:

    "Posso ajudar voce com informacoes sobre nossos produtos, tirar duvidas,
     acompanhar pedidos e oferecer suporte no que precisar."

Nao existe capability de pedido alguma exposta. O convite e falso, e o cliente
so descobre depois de pedir — que e o pior momento.

A listagem de APIs disponiveis, sozinha, nao resolvia: dizer o que existe nao
fecha a porta do que nao existe, e o modelo preenchia a lacuna. A restricao
precisa ser explicita, e precisa ser DERIVADA DO RUNTIME — nunca escrita na
persona, senao vira mentira no dia em que a capability for ligada.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.capability_catalog import (
    _CUSTOMER_CAPABILITIES,
    _ORDER_CAPABILITIES,
    format_capability_catalog_for_prompt,
)

PROIBIDO = ("acompanhar", "rastrear", "status de pedido")


def _provider_pronto():
    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.provider import MercosCommerceProvider
    from app.commerce.mercos.sync_state import SyncState

    class _Sincronizado:
        def read(self, provider, resource):
            return SyncState(provider, resource, last_success_at=datetime.now(timezone.utc))

    return MercosCommerceProvider(
        MercosAdaptorClient(base_url="https://a.example.com", api_key="k"),
        index=object(),
        tenant_id="xnamai",
        sync_state=_Sincronizado(),
    )


@pytest.fixture
def com_provider(monkeypatch):
    def aplicar(provider):
        import app.commerce.provider as modulo

        monkeypatch.setattr(modulo, "get_commerce_provider", lambda: provider)
        return format_capability_catalog_for_prompt()

    return aplicar


# === o cenario real de producao ============================================


def test_with_mercos_ready_the_prompt_forbids_offering_order_tracking(com_provider):
    texto = com_provider(_provider_pronto())
    assert "nao ofereca acompanhar" in texto.casefold()
    assert "nao ha consulta de pedido" in texto.casefold()


def test_with_mercos_ready_only_the_three_approved_apis_are_listed(com_provider):
    texto = com_provider(_provider_pronto())
    listadas = [
        linha.split(" (")[0].removeprefix("- ")
        for linha in texto.splitlines()
        if linha.startswith("- ") and " (retryable)" in linha or " (manual)" in linha
    ]
    assert set(listadas) == {"search_products", "get_product", "check_inventory"}


@pytest.mark.parametrize("capability", sorted(_ORDER_CAPABILITIES))
def test_no_order_capability_is_offered_as_available(com_provider, capability):
    texto = com_provider(_provider_pronto())
    assert f"- {capability} (" not in texto


@pytest.mark.parametrize("capability", sorted(_CUSTOMER_CAPABILITIES))
def test_no_customer_capability_is_offered_as_available(com_provider, capability):
    texto = com_provider(_provider_pronto())
    assert f"- {capability} (" not in texto


def test_create_order_is_never_announced(com_provider):
    """Mutacao de pedido nao entra no anuncio em hipotese alguma."""
    texto = com_provider(_provider_pronto())
    assert "create_order" not in texto


# === a restricao e derivada, nao escrita a mao ==============================


def test_the_restriction_disappears_when_the_capability_exists():
    """Ligar consulta de pedido apaga a proibicao sozinha — sem editar prompt."""
    from app.capability_catalog import _restrictions_for

    sem_pedido = "\n".join(_restrictions_for(["search_products"]))
    com_pedido = "\n".join(_restrictions_for(["search_products", "get_order"]))

    assert "nao ha consulta de pedido" in sem_pedido.casefold()
    assert "nao ha consulta de pedido" not in com_pedido.casefold()


def test_without_any_commerce_capability_nothing_is_promised():
    from app.capability_catalog import _restrictions_for

    texto = "\n".join(_restrictions_for([])).casefold()
    assert "nenhuma consulta comercial esta ativa" in texto
    assert "nao prometa catalogo" in texto


def test_the_restriction_never_names_a_vendor():
    """A proibicao vale para qualquer provider: nada de Mercos no prompt."""
    from app.capability_catalog import _restrictions_for

    for caso in ([], ["search_products"], ["search_products", "get_order"]):
        texto = "\n".join(_restrictions_for(caso)).casefold()
        for marca in ("mercos", "tray", "xnamai"):
            assert marca not in texto


# === nao regrediu o estado sem provider =====================================


def test_the_null_provider_prompt_still_describes_the_full_contract(com_provider):
    """Sem provider o catalogo segue descrevendo o contrato, como no baseline."""
    from app.commerce.provider import NullCommerceProvider

    texto = com_provider(NullCommerceProvider())
    assert "- get_order (" in texto
    assert "- search_products (" in texto
    # E por listar pedido como existente, nao emite a proibicao — o prompt
    # permanece identico ao baseline neste caminho.
    assert "nao ha consulta de pedido" not in texto.casefold()


# === a persona continua fora disso ==========================================


def test_the_system_instructions_do_not_hardcode_any_capability_list():
    from app.openai_agent import SYSTEM_INSTRUCTIONS

    texto = SYSTEM_INSTRUCTIONS.casefold()
    for termo in ("search_products", "get_order", "mercos"):
        assert termo not in texto
