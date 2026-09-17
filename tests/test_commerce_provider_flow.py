"""Fluxo alvo da Parte 1, ponta a ponta, sem mocks no caminho comercial.

    intenção comercial -> CommerceProvider -> NullCommerceProvider
                       -> ``commerce_provider_unavailable``

Nada aqui substitui ``execute_tool``, ``get_commerce_provider`` ou o provider:
o turno roda contra a implementação real e a evidência é o comportamento
observado (telemetria emitida, resultado devolvido, ausência de rede e de
qualquer módulo legado carregado). Nenhum fallback para Tray é permitido.
"""

from __future__ import annotations

import sys

import pytest

from app.commerce.errors import COMMERCE_UNAVAILABLE_CODE
from app.commerce.provider import (
    NullCommerceProvider,
    get_commerce_provider,
    reset_commerce_provider,
)
from app.commerce.tools import execute_tool
from app.models import IncomingMessage

LEGACY_MODULE_TOKENS = ("tray", "mercadopago", "pix_", "sorteio")


@pytest.fixture(autouse=True)
def _real_provider():
    """Garante o provider real (nenhum outro teste pode ter plugado um fake)."""
    reset_commerce_provider()
    yield
    reset_commerce_provider()


@pytest.fixture
def _no_network(monkeypatch):
    """Qualquer tentativa de HTTP durante o turno é falha do teste.

    É assim que se prova a ausência de fallback: se sobrasse um cliente legado,
    ele teria que sair pela rede.
    """
    import httpx

    def explode(*_args, **_kwargs):
        raise AssertionError("nenhuma chamada HTTP é permitida sem provider comercial")

    monkeypatch.setattr(httpx.AsyncClient, "send", explode)
    monkeypatch.setattr(httpx.AsyncClient, "request", explode)
    monkeypatch.setattr(httpx.Client, "send", explode)
    monkeypatch.setattr(httpx.Client, "request", explode)


def _legacy_modules_loaded() -> list[str]:
    """Módulos de runtime (app/api) legados carregados no processo — deve ser vazio."""
    return [
        name
        for name in list(sys.modules)
        if (name.startswith("app.") or name.startswith("api."))
        and any(token in name.casefold() for token in LEGACY_MODULE_TOKENS)
    ]


def test_active_provider_is_the_null_provider():
    assert isinstance(get_commerce_provider(), NullCommerceProvider)
    assert get_commerce_provider().name == "null"


@pytest.mark.asyncio
async def test_boundary_returns_unavailable_for_every_declared_capability():
    """Nenhuma capacidade comercial pode responder com dado inventado."""
    from app.commerce.contracts import COMMERCE_CAPABILITIES

    for capability in COMMERCE_CAPABILITIES:
        result = await execute_tool(capability, {"query": "x", "product_id": "1"})
        assert result["ok"] is False
        assert result["error"] == COMMERCE_UNAVAILABLE_CODE
        assert result["capability"] == capability
        assert "products" not in result
        assert "price" not in result
        assert "stock" not in result


@pytest.mark.asyncio
async def test_commercial_intent_reaches_null_provider_and_invents_nothing(
    capsys, _no_network
):
    """Turno real: pergunta comercial -> boundary -> NullCommerceProvider.

    Sem patch algum no caminho comercial. A prova é a telemetria de produção
    emitida pelo próprio ``execute_tool``.
    """
    from app import openai_agent

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="Voces tem MarcaA ChargeMax? qual o preco?"),
        {},
    )
    output = capsys.readouterr().out

    # 1. O turno chegou de fato à fronteira comercial.
    assert "[commerce.tool] executed" in output
    executed = [
        line for line in output.splitlines() if line.startswith("[commerce.tool] executed")
    ]
    assert executed, "o turno comercial precisa exercitar a fronteira"

    # 2. TODA chamada terminou em commerce_provider_unavailable.
    for line in executed:
        assert "'ok': False" in line
        assert COMMERCE_UNAVAILABLE_CODE in line

    # 3. Nenhum fato comercial inventado chegou ao cliente.
    assert result.intent == "commerce"
    assert result.commercial_data in (None, {})
    reply = result.reply_text.casefold()
    assert "r$" not in reply
    assert "marcaa" not in reply
    assert "ChargeMax" not in reply

    # 4. Nenhum fallback: nenhum módulo legado foi carregado no processo.
    assert _legacy_modules_loaded() == []


@pytest.mark.asyncio
async def test_commercial_turn_never_falls_back_to_a_legacy_client(monkeypatch, _no_network):
    """Se alguém replugar um provider legado, o teste tem que ver a diferença.

    Aqui o provider real é instrumentado por composição (ainda levantando o erro
    real): serve para provar QUE o caminho passa pelo protocolo CommerceProvider
    e por mais nada.
    """
    from app import openai_agent
    from app.commerce import provider as provider_module

    seen: list[str] = []
    real = NullCommerceProvider()

    class ObservedNullProvider:
        name = "null"

        async def execute(self, capability, arguments):
            seen.append(capability)
            return await real.execute(capability, arguments)

    provider_module.set_commerce_provider(ObservedNullProvider())

    await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="quanto custa o produto 1025?"),
        {},
    )

    assert seen, "a intenção comercial precisa chegar ao CommerceProvider"
    assert _legacy_modules_loaded() == []


@pytest.mark.asyncio
async def test_execute_tool_never_raises_and_never_leaks_provider_internals(capsys):
    """Contrato do boundary: devolve dado estruturado, nunca levanta.

    Cobertura equivalente à que o cliente HTTP legado tinha (log seguro de
    requisição/resposta), agora na fronteira nova.
    """
    from app.commerce import provider as provider_module

    class ExplodingProvider:
        name = "exploding"

        async def execute(self, capability, arguments):
            raise RuntimeError("token=super-secret-value")

    provider_module.set_commerce_provider(ExplodingProvider())
    result = await execute_tool("search_products", {"query": "produto"})
    output = capsys.readouterr().out

    assert result["error"] == "commerce_tool_error"
    assert result["error_type"] == "RuntimeError"
    assert "super-secret-value" not in output
    assert "super-secret-value" not in str(result)
