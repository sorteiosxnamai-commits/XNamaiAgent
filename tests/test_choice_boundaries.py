"""Short conversational choices cannot steal a checkout or revive stale actions."""
from unittest.mock import AsyncMock

import pytest

from app.commerce_context import CommerceConversationState
from app.conversation_choices import handle_short_choice
from app.models import IncomingMessage


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [
    "send_product_link", "create_cart", "show_images", "show_payment_options",
    "choose_checkout_channel",
])
async def test_current_document_question_supersedes_old_non_authorizing_action(monkeypatch, pending):
    monkeypatch.setattr("app.capability_catalog.runtime_commerce_capabilities", lambda: frozenset())
    state = CommerceConversationState(
        pending_action=pending,
        pending_followup={"question": "Você quer comprar com CPF ou CNPJ?"},
        active_product={"product_id": "old-product"},
    )
    execute = AsyncMock(side_effect=AssertionError("A document choice does not authorize a provider call"))
    result = await handle_short_choice(
        IncomingMessage(text="cpf"), state=state, execute=execute,
        recent_turns=[{"role": "assistant", "content": "Você quer comprar com CPF ou CNPJ? 😊"}],
    )
    assert result is not None
    assert result.response_metadata["pending_action"] == "awaiting_customer_registration_data"
    assert result.response_metadata["response_source"] == "contextual_registration_choice"
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state_data,text,question", [
    ({"cart_session_id": "cart-1", "purchase_stage": "payment_discussion", "checkout_channel_preference": "site"},
     "pix", "Você prefere Pix ou cartão?"),
    ({"cart_id": "cart-1", "purchase_stage": "checkout",
      "selected_payment_option": {"id": "pix-1", "name": "Pix", "method": "pix"}},
     "cartão", "Você prefere Pix ou cartão?"),
    ({"cart_session_id": "cart-1", "purchase_stage": "shipping",
      "shipping_quotes": [{"shipping_id": "pickup", "name": "Retirada", "price": "0.00"}]},
     "retirada", "Prefere entrega ou retirada?"),
])
async def test_existing_checkout_choice_reaches_its_verified_selection_handler(state_data, text, question):
    # inspect_payment_options legitimately clears pending_action for site checkout.
    # Losing that flag does not turn an active cart into a generic policy question.
    state = CommerceConversationState(**state_data, pending_followup={"question": question})
    execute = AsyncMock(side_effect=AssertionError("The choice guard itself must not mutate or query"))
    assert await handle_short_choice(IncomingMessage(text=text), state=state, execute=execute) is None
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [
    "awaiting_customer_registration_data", "awaiting_customer_registration_confirmation",
    "awaiting_order_confirmation", "awaiting_checkout_data", "awaiting_shipping_selection",
])
async def test_document_question_does_not_cancel_strong_collection_or_confirmation(pending):
    state = CommerceConversationState(pending_action=pending,
        pending_followup={"question": "Você quer comprar com CPF ou CNPJ?"})
    before = state.model_dump()
    execute = AsyncMock(side_effect=AssertionError("No implicit confirmation"))
    assert await handle_short_choice(IncomingMessage(text="cpf"), state=state, execute=execute) is None
    assert state.model_dump() == before
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_order_alone_does_not_turn_generic_pix_preference_into_transaction():
    state = CommerceConversationState(order_id="old-order", active_topic="commercial_guidance",
        pending_followup={"question": "Qual forma de pagamento você prefere?"})
    execute = AsyncMock(side_effect=AssertionError("Old order is not checkout authorization"))
    result = await handle_short_choice(IncomingMessage(text="pix"), state=state, execute=execute)
    assert result is not None
    assert result.response_metadata["informational_only"]
    assert not result.response_metadata.get("selected_payment_option")
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_latest_question_does_not_start_registration_from_stale_pending_question():
    state = CommerceConversationState(pending_followup={"question": "Você quer comprar com CPF ou CNPJ?"})
    result = await handle_short_choice(IncomingMessage(text="cpf"), state=state, execute=AsyncMock(),
        recent_turns=[{"role": "assistant", "content": "Qual marca de fone você procura?"}])
    assert result is not None and result.reply_text.endswith("?")
    assert not result.response_metadata.get("pending_action")
    assert not result.response_metadata.get("customer_registration")
