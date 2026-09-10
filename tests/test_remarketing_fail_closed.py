"""Sem provider comercial, o remarketing falha FECHADO.

Um item que carrega ``order_id`` ou ``cart_session_id`` só pode ser disparado depois
de verificar que o pedido ainda não foi pago. Sem fonte comercial configurada essa
verificação é impossível, então o item NÃO pode ser enviado: registra-se a tentativa
como falha com ``commerce_provider_unavailable``. Nunca inventar estado comercial e
nunca cobrar quem já comprou.
"""

from types import SimpleNamespace

import pytest

from app.models import BrevoSendResult


def _install_batch(monkeypatch, remarketing, items, sent, finished):
    monkeypatch.setattr(
        remarketing,
        "get_settings",
        lambda: SimpleNamespace(remarketing_enabled=True, remarketing_batch_size=25),
    )
    monkeypatch.setattr(
        remarketing,
        "claim_due_remarketing_attempts",
        lambda _limit: items,
    )
    monkeypatch.setattr(
        remarketing,
        "finish_remarketing_attempt",
        lambda attempt_id, **kwargs: finished.append((attempt_id, kwargs)),
    )

    async def fake_send(incoming, text):
        sent.append((incoming, text))
        return BrevoSendResult(ok=True)

    monkeypatch.setattr(remarketing, "send_brevo_reply", fake_send)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commercial_ref",
    [{"order_id": "order-1"}, {"cart_session_id": "cart-1"}],
    ids=["order_id", "cart_session_id"],
)
async def test_item_needing_verification_is_not_sent_without_provider(
    monkeypatch, commercial_ref
):
    import app.remarketing as remarketing

    sent: list = []
    finished: list = []
    item = {
        "id": 11,
        "conversation_status_id": 21,
        "touch_number": 1,
        "stage": "awaiting_payment",
        "channel": "whatsapp",
        "sender_phone": "5511999999999",
        **commercial_ref,
    }
    _install_batch(monkeypatch, remarketing, [item], sent, finished)

    result = await remarketing.run_remarketing_batch()

    assert sent == [], "item que exige verificação comercial não pode ser disparado"
    assert result == {"claimed": 1, "sent": 0, "failed": 1}
    assert len(finished) == 1
    attempt_id, kwargs = finished[0]
    assert attempt_id == 11
    assert kwargs["send_ok"] is False
    assert kwargs["error"] == "commerce_provider_unavailable"
    assert kwargs["message_text"] == ""
    assert kwargs["provider_response"] == {"verification": "unavailable"}


@pytest.mark.asyncio
async def test_item_without_commercial_reference_is_still_sent(monkeypatch):
    import app.remarketing as remarketing

    sent: list = []
    finished: list = []
    item = {
        "id": 12,
        "conversation_status_id": 22,
        "touch_number": 1,
        "stage": "browsing",
        "channel": "whatsapp",
        "sender_phone": "5511999999999",
    }
    _install_batch(monkeypatch, remarketing, [item], sent, finished)

    result = await remarketing.run_remarketing_batch()

    assert len(sent) == 1, "item sem referência comercial nunca dependeu de verificação"
    assert result == {"claimed": 1, "sent": 1, "failed": 0}
    assert finished[0][0] == 12
    assert finished[0][1]["send_ok"] is True


@pytest.mark.asyncio
async def test_blank_commercial_reference_does_not_block_send(monkeypatch):
    """Strings vazias/whitespace não são referência comercial — mesmo caminho de antes."""
    import app.remarketing as remarketing

    sent: list = []
    finished: list = []
    item = {
        "id": 13,
        "conversation_status_id": 23,
        "touch_number": 1,
        "stage": "browsing",
        "channel": "whatsapp",
        "sender_phone": "5511999999999",
        "order_id": "  ",
        "cart_session_id": None,
    }
    _install_batch(monkeypatch, remarketing, [item], sent, finished)

    result = await remarketing.run_remarketing_batch()

    assert len(sent) == 1
    assert result == {"claimed": 1, "sent": 1, "failed": 0}


@pytest.mark.asyncio
async def test_remarketing_batch_never_calls_a_commerce_provider(monkeypatch):
    """Guarda de comportamento REAL: zero chamadas à fronteira comercial.

    A versão anterior só assertava ``not hasattr(remarketing, "TrayAdapterClient")``,
    que é uma propriedade estática de um símbolo que já não existe — passaria
    mesmo que o batch voltasse a consultar o comercial por outro nome. Aqui o
    provider real é substituído por um espião e ``app.commerce.tools.execute_tool``
    por uma função que explode: qualquer consulta comercial, por qualquer
    caminho, falha o teste.
    """
    import app.remarketing as remarketing
    from app.commerce import provider as provider_module
    from app.commerce import tools as commerce_tools

    assert not hasattr(remarketing, "TrayAdapterClient")
    assert not hasattr(remarketing, "execute_tool")

    provider_calls: list = []

    class SpyProvider:
        name = "spy"

        async def execute(self, capability, arguments):
            provider_calls.append((capability, arguments))
            return {"ok": True}

    async def exploding_execute_tool(*_args, **_kwargs):
        raise AssertionError("remarketing não pode chamar execute_tool")

    monkeypatch.setattr(commerce_tools, "execute_tool", exploding_execute_tool)
    # O espião entra como provider real da fronteira; o finalizador devolve o
    # NullCommerceProvider para não vazar estado global para outros testes.
    provider_module.set_commerce_provider(SpyProvider())
    monkeypatch.setattr(
        commerce_tools, "get_commerce_provider", provider_module.get_commerce_provider
    )
    try:
        await _run_batch_and_assert_no_commerce(
            monkeypatch, remarketing, provider_calls
        )
    finally:
        provider_module.reset_commerce_provider()


async def _run_batch_and_assert_no_commerce(monkeypatch, remarketing, provider_calls):
    sent: list = []
    finished: list = []
    items = [
        {
            "id": 14,
            "conversation_status_id": 24,
            "touch_number": 1,
            "stage": "awaiting_payment",
            "channel": "whatsapp",
            "sender_phone": "5511999999999",
            "order_id": "order-9",
        },
        {
            "id": 15,
            "conversation_status_id": 25,
            "touch_number": 1,
            "stage": "browsing",
            "channel": "whatsapp",
            "sender_phone": "5511999999998",
        },
    ]
    _install_batch(monkeypatch, remarketing, items, sent, finished)

    def explode(*_args, **_kwargs):
        raise AssertionError("nenhum estado comercial pode ser inventado ou consultado")

    monkeypatch.setattr(remarketing, "complete_paid_remarketing", explode)

    result = await remarketing.run_remarketing_batch()

    assert result == {"claimed": 2, "sent": 1, "failed": 1}
    assert [attempt_id for attempt_id, _ in finished] == [14, 15]
    assert provider_calls == [], (
        "nenhuma capacidade comercial pode ser consultada pelo remarketing: "
        f"{provider_calls}"
    )
