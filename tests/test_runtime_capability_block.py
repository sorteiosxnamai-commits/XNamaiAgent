"""O caminho GERAL do agente tambem precisa saber o que nao consegue fazer.

Com a Mercos ativa e SOMENTE tres tools expostas, perguntado "o que voce
consegue fazer?", o agente respondeu em producao:

    "Posso ajudar com informacoes sobre nossos produtos, tirar duvidas,
     acompanhar pedidos e oferecer suporte geral."

A proibicao ja existia — no catalogo de capabilities — mas `openai_agent`, que
atende o intent `general`, nunca leu esse catalogo: usava so `SYSTEM_INSTRUCTIONS`
mais a persona compilada. A pergunta cai justamente ali.

O bloco abaixo fecha a lacuna DERIVANDO do runtime. Nao entra na persona, nao
nomeia fornecedor, e a proibicao some sozinha no dia em que a capacidade for
ligada de verdade — que e o unico jeito de uma restricao dessas nao virar
mentira com o tempo.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.capability_catalog import runtime_capability_block, runtime_commerce_capabilities

APROVADAS = {"search_products", "get_product", "check_inventory"}
PEDIDO = ("list_orders", "get_order", "get_order_complete", "get_order_payment", "create_order")


def _provider_pronto():
    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.provider import MercosCommerceProvider
    from app.commerce.mercos.sync_state import SyncState

    class _Sincronizado:
        def read(self, provider, resource):
            return SyncState(provider, resource, last_success_at=datetime.now(timezone.utc))

    return MercosCommerceProvider(
        MercosAdaptorClient(base_url="https://a.example.com", api_key="k"),
        index=object(), tenant_id="xnamai", sync_state=_Sincronizado(),
    )


def _provider_sem_sync():
    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.provider import MercosCommerceProvider
    from app.commerce.mercos.sync_state import SyncState

    class _SemSync:
        def read(self, provider, resource):
            return SyncState(provider, resource)

    return MercosCommerceProvider(
        MercosAdaptorClient(base_url="https://a.example.com", api_key="k"),
        index=object(), tenant_id="xnamai", sync_state=_SemSync(),
    )


@pytest.fixture
def com_provider(monkeypatch):
    def aplicar(provider):
        import app.commerce.provider as modulo

        monkeypatch.setattr(modulo, "get_commerce_provider", lambda: provider)

    return aplicar


# === o que o runtime realmente serve ======================================


def test_a_ready_provider_reports_only_the_approved_capabilities(com_provider):
    com_provider(_provider_pronto())
    assert set(runtime_commerce_capabilities()) == APROVADAS


def test_a_provider_without_sync_reports_nothing(com_provider):
    """Configurado nao e pronto: sem sync nenhuma capacidade e anunciada."""
    com_provider(_provider_sem_sync())
    assert runtime_commerce_capabilities() == frozenset()


def test_the_null_provider_reports_nothing(com_provider):
    from app.commerce.provider import NullCommerceProvider

    com_provider(NullCommerceProvider())
    assert runtime_commerce_capabilities() == frozenset()


def test_a_broken_provider_degrades_to_nothing(monkeypatch):
    """Falha ao resolver provider nao pode derrubar o turno nem liberar tudo."""
    import app.commerce.provider as modulo

    def explode():
        raise RuntimeError("provider indisponivel")

    monkeypatch.setattr(modulo, "get_commerce_provider", explode)
    assert runtime_commerce_capabilities() == frozenset()
    assert "nao ofereca acompanhar" in runtime_capability_block().casefold()


# === o bloco de sistema ===================================================


def test_with_commerce_ready_the_block_lists_the_three_and_forbids_orders(com_provider):
    com_provider(_provider_pronto())
    texto = runtime_capability_block()
    minusculo = texto.casefold()
    for nome in APROVADAS:
        assert nome in texto
    assert "nao ofereca acompanhar" in minusculo
    assert "nao ha consulta de pedido" in minusculo


@pytest.mark.parametrize("capability", PEDIDO)
def test_no_order_capability_is_ever_announced(com_provider, capability):
    com_provider(_provider_pronto())
    texto = runtime_capability_block()
    assert f"AGORA: {capability}" not in texto
    linha_disponiveis = next(
        (l for l in texto.splitlines() if l.startswith("Consultas comerciais")), ""
    )
    assert capability not in linha_disponiveis


def test_without_commerce_the_block_promises_nothing(com_provider):
    from app.commerce.provider import NullCommerceProvider

    com_provider(NullCommerceProvider())
    minusculo = runtime_capability_block().casefold()
    assert "nenhuma consulta comercial esta ativa" in minusculo
    assert "nao prometa catalogo" in minusculo
    assert "consultas comerciais disponiveis agora" not in minusculo


def test_the_block_never_names_a_vendor(com_provider):
    for provider in (_provider_pronto(), _provider_sem_sync()):
        com_provider(provider)
        minusculo = runtime_capability_block().casefold()
        for marca in ("mercos", "tray", "xnamai", "supabase"):
            assert marca not in minusculo


def test_the_block_is_delimited(com_provider):
    com_provider(_provider_pronto())
    texto = runtime_capability_block()
    assert texto.startswith("<runtime_capabilities>")
    assert texto.endswith("</runtime_capabilities>")


# === ligacao com o caminho geral do agente ================================


def test_the_general_path_receives_the_block(com_provider):
    from app.openai_agent import _runtime_capability_blocks

    com_provider(_provider_pronto())
    blocos = _runtime_capability_blocks()
    assert len(blocos) == 1
    assert "search_products" in blocos[0]


def test_the_block_never_breaks_the_turn(monkeypatch):
    """Se a capacidade falhar, o prompt segue sem ela — nunca com excecao."""
    import app.capability_catalog as cc
    from app.openai_agent import _runtime_capability_blocks

    def explode():
        raise RuntimeError("falha ao montar bloco")

    monkeypatch.setattr(cc, "runtime_capability_block", explode)
    assert _runtime_capability_blocks() == []


def test_both_prompt_sites_include_the_block():
    """Sync e async: a mesma proibicao nos dois caminhos."""
    import inspect

    from app import openai_agent

    fonte = inspect.getsource(openai_agent)
    assert fonte.count("+ _runtime_capability_blocks(),") == 2


def test_the_persona_was_not_touched():
    from app.openai_agent import SYSTEM_INSTRUCTIONS

    minusculo = SYSTEM_INSTRUCTIONS.casefold()
    for termo in ("search_products", "get_order", "mercos", "runtime_capabilities"):
        assert termo not in minusculo


def test_order_tracking_was_not_enabled_anywhere(com_provider):
    """A correcao e de anuncio, nao de funcionalidade."""
    from app.commerce.mercos.provider import LLM_EXPOSED_CAPABILITIES

    assert LLM_EXPOSED_CAPABILITIES == frozenset(APROVADAS)
    com_provider(_provider_pronto())
    assert not (set(runtime_commerce_capabilities()) & set(PEDIDO))
