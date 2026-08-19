from types import SimpleNamespace

import pytest

from app.models import BrevoSendResult, IncomingMessage
from app.remarketing import (
    _build_remarketing_message,
    _remarketing_stage,
    is_remarketing_opt_out,
    remarketing_identity_key,
)


def test_opt_out_requires_a_clear_standalone_command():
    assert is_remarketing_opt_out("SAIR") is True
    assert is_remarketing_opt_out("Não quero mais mensagens") is True
    assert is_remarketing_opt_out("Como faço para sair da conta?") is False


def test_remarketing_identity_keeps_the_omnichannel_sender_key():
    incoming = IncomingMessage(
        channel="instagram",
        sender_key="instagram:user-123",
        visitor_id="visitor-123",
        conversation_id="conversation-123",
    )

    assert remarketing_identity_key(incoming) == "instagram:user-123"


def test_stage_prioritizes_payment_checkout_and_cart():
    assert _remarketing_stage({"order_payment_status": "pending"}) == "awaiting_payment"
    assert _remarketing_stage({"purchase_stage": "shipping"}) == "checkout"
    assert _remarketing_stage({"cart_session_id": "cart-1"}) == "cart"
    assert _remarketing_stage({"active_product": {"name": "Relógio"}}) == "product_selection"


def test_message_is_stage_specific_and_always_contains_opt_out():
    text = _build_remarketing_message(
        {
            "sender_name": "Maria Silva",
            "stage": "cart",
            "touch_number": 3,
            "cart_url": "https://loja.example/carrinho",
        }
    )

    assert "Oi, Maria!" in text
    assert "última mensagem" in text
    assert "https://loja.example/carrinho" in text
    assert "responda SAIR" in text


@pytest.mark.asyncio
async def test_batch_replies_only_through_the_origin_channel(monkeypatch):
    import app.remarketing as remarketing

    sent = []
    finished = []
    monkeypatch.setattr(
        remarketing,
        "get_settings",
        lambda: SimpleNamespace(remarketing_enabled=True, remarketing_batch_size=25),
    )
    monkeypatch.setattr(
        remarketing,
        "claim_due_remarketing_attempts",
        lambda _limit: [
            {
                "id": 10,
                "conversation_status_id": 20,
                "touch_number": 1,
                "stage": "product_selection",
                "product_name": "Relógio",
                "channel": "instagram",
                "sender_key": "instagram:user-1",
                "visitor_id": "visitor-1",
                "conversation_id": "conversation-1",
                "sender_name": "Ana",
                "order_id": None,
                "cart_url": None,
                "payment_url": None,
            }
        ],
    )

    async def fake_send(incoming, text):
        sent.append((incoming, text))
        return BrevoSendResult(ok=True, dry_run=False)

    monkeypatch.setattr(remarketing, "send_brevo_reply", fake_send)
    monkeypatch.setattr(
        remarketing,
        "finish_remarketing_attempt",
        lambda attempt_id, **kwargs: finished.append((attempt_id, kwargs)),
    )

    result = await remarketing.run_remarketing_batch()

    assert result == {"claimed": 1, "sent": 1, "failed": 0}
    assert sent[0][0].channel == "instagram"
    assert sent[0][0].visitor_id == "visitor-1"
    assert finished[0][0] == 10
    assert finished[0][1]["send_ok"] is True


@pytest.mark.asyncio
async def test_paid_order_is_closed_before_any_remarketing_send(monkeypatch):
    import app.remarketing as remarketing

    completed = []
    monkeypatch.setattr(
        remarketing,
        "get_settings",
        lambda: SimpleNamespace(remarketing_enabled=True, remarketing_batch_size=25),
    )
    monkeypatch.setattr(
        remarketing,
        "claim_due_remarketing_attempts",
        lambda _limit: [
            {
                "id": 11,
                "conversation_status_id": 21,
                "touch_number": 1,
                "stage": "awaiting_payment",
                "channel": "whatsapp",
                "sender_phone": "5511999999999",
                "order_id": "order-1",
            }
        ],
    )

    class Client:
        async def get_order_payment(self, _order_id):
            return {"success": True, "payment": {"has_payment": True}}

    monkeypatch.setattr(remarketing, "TrayAdapterClient", Client)
    monkeypatch.setattr(
        remarketing,
        "complete_paid_remarketing",
        lambda conversation_id, attempt_id: completed.append(
            (conversation_id, attempt_id)
        ),
    )

    async def fail_send(*_args):
        raise AssertionError("paid customer must not receive remarketing")

    monkeypatch.setattr(remarketing, "send_brevo_reply", fail_send)

    result = await remarketing.run_remarketing_batch()

    assert result == {"claimed": 1, "sent": 0, "failed": 0}
    assert completed == [(21, 11)]
