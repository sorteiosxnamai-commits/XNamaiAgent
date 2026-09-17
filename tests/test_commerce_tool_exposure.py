"""Tools comerciais so chegam ao modelo quando ha provider capaz de executa-las.

Regressao que este arquivo fixa: a primeira tentativa da Parte 1 trocou o gate
``intent == "commerce" and settings.tray_adapter_url and settings.tray_adapter_token``
por ``intent == "commerce"`` puro. Efeito: onde antes o modelo recebia ZERO
tools sem fonte configurada, passou a receber 13 tools que sempre falham —
mais tokens por turno e um convite a tool-call inutil.

O gate correto e por PROVIDER, nao por env de fornecedor.
"""

from __future__ import annotations

import inspect

import pytest

from app.commerce.provider import (
    NullCommerceProvider,
    get_commerce_provider,
    reset_commerce_provider,
    set_commerce_provider,
)
from app.commerce.tools import (
    TOOL_SCHEMAS,
    commerce_tools_available,
    tool_schemas_for_model,
)


@pytest.fixture(autouse=True)
def _restore_provider():
    reset_commerce_provider()
    yield
    reset_commerce_provider()


class _AvailableProvider:
    """Stand-in do provider concreto da Parte 2. Nao faz I/O."""

    name = "stub"
    available = True

    async def execute(self, capability, arguments):
        return {"ok": True, "capability": capability}


def test_null_provider_is_the_default_and_is_unavailable():
    provider = get_commerce_provider()
    assert isinstance(provider, NullCommerceProvider)
    assert provider.available is False
    assert commerce_tools_available() is False


def test_null_provider_sends_no_commerce_tool_to_the_model():
    assert tool_schemas_for_model() is None


def test_available_provider_may_expose_the_schemas():
    set_commerce_provider(_AvailableProvider())
    assert commerce_tools_available() is True
    schemas = tool_schemas_for_model()
    assert schemas == TOOL_SCHEMAS
    assert schemas, "um provider disponivel precisa expor pelo menos uma tool"


def test_openai_gate_depends_on_the_provider_not_on_a_vendor_env():
    """O gate no agente nao pode voltar a depender de env de fornecedor."""
    from app import openai_agent

    source = inspect.getsource(openai_agent.generate_openai_reply_async)
    assert "commerce_tools_available()" in source
    for vendor_env in ("tray_adapter_url", "tray_adapter_token", "settings.tray_", "mercos"):
        assert vendor_env not in source, f"gate voltou a depender de {vendor_env}"


@pytest.mark.asyncio
async def test_commerce_intent_without_provider_never_enters_the_tool_loop(monkeypatch):
    """Caminho real: intencao comercial + provider indisponivel => sem tool loop.

    Exercita ``generate_openai_reply_async`` de verdade e espiona os dois
    caminhos do gateway: o de texto puro (sem tools) e o de tool loop.
    """
    from app import openai_agent, openai_gateway
    from app.models import IncomingMessage

    used = {"text": False, "tool_loop": False, "tools": "nao chamado"}

    class _Text:
        text = "resposta sem tools"

    async def fake_generate_text_output(**kwargs):
        used["text"] = True
        return _Text()

    async def fake_run_tool_loop_output(**kwargs):
        used["tool_loop"] = True
        used["tools"] = kwargs.get("tools")
        raise AssertionError("sem provider o modelo nao pode entrar no tool loop")

    monkeypatch.setattr(openai_gateway, "generate_text_output", fake_generate_text_output)
    monkeypatch.setattr(openai_gateway, "run_tool_loop_output", fake_run_tool_loop_output)
    monkeypatch.setattr(
        openai_agent,
        "get_settings",
        lambda: type(
            "S", (), {
                "openai_api_key": "sk-test", "openai_model": "gpt-4.1-mini",
                "max_reply_chars": 900, "agent_persona_runtime_enabled": False,
            },
        )(),
    )

    assert commerce_tools_available() is False
    result = await openai_agent.generate_openai_reply_async(
        IncomingMessage(text="quero comprar um produto", sender_phone="5511999999999"),
        {},
        {"primary_intent": "commerce"},
    )
    assert used["tool_loop"] is False, f"tool loop acionado com tools={used['tools']}"
    assert used["text"] is True
    assert result.reply_text
