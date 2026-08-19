from app.checkout_service import checkout_capabilities, select_checkout_channel
from app.commerce_context import CommerceConversationState, evolve_commerce_state


def _cart_state() -> CommerceConversationState:
    return CommerceConversationState(
        active_domain="commerce",
        cart_session_id="SESSION-1",
        cart_url="https://loja.example/checkout/SESSION-1",
        cart_items=[
            {
                "product_id": "P1",
                "variant_id": "123",
                "quantity": 2,
            }
        ],
        purchase_stage="cart_created",
        pending_action="choose_checkout_channel",
    )


def test_checkout_capabilities_reflect_only_supported_backend_paths():
    facts = checkout_capabilities(_cart_state())

    assert facts == {
        "cart_ready": True,
        "whatsapp_checkout_supported": False,
        "whatsapp_order_supported": True,
        "whatsapp_hosted_payment_supported": True,
        "whatsapp_native_payment_supported": False,
        "whatsapp_payment_supported": False,
        "pix_direct_enabled": False,
        "site_checkout_supported": True,
        "requires_channel_choice": True,
        "selected_channel": None,
        "selected_channel_supported": None,
        "sensitive_payment_data_allowed_in_chat": False,
    }


def test_site_choice_updates_checkout_state_and_survives_roundtrip():
    state = _cart_state()
    result = select_checkout_channel(state, "site")
    updated = evolve_commerce_state(state, result)
    restored = CommerceConversationState.from_payload(
        updated.model_dump(mode="json")
    )

    assert result.safety_reason is None
    assert result.commercial_data["checkout"]["cart_url"] == (
        "https://loja.example/checkout/SESSION-1"
    )
    assert result.commercial_data["checkout"]["selected_channel_supported"] is True
    assert restored.checkout_channel_preference == "site"
    assert restored.purchase_stage == "checkout_ready"
    assert restored.pending_action is None
    assert restored.cart_session_id == "SESSION-1"
    assert restored.cart_items[0].quantity == 2
    assert checkout_capabilities(restored)["selected_channel"] == "site"
    assert checkout_capabilities(restored)["requires_channel_choice"] is False


def test_whatsapp_choice_enables_order_but_not_payment_execution():
    state = _cart_state()
    result = select_checkout_channel(state, "whatsapp")
    updated = evolve_commerce_state(state, result)

    assert result.safety_reason is None
    assert result.commercial_data["checkout"]["whatsapp_checkout_supported"] is False
    assert result.commercial_data["checkout"]["whatsapp_order_supported"] is True
    assert result.commercial_data["checkout"]["whatsapp_hosted_payment_supported"] is True
    assert result.commercial_data["checkout"]["whatsapp_native_payment_supported"] is False
    assert result.commercial_data["checkout"]["whatsapp_payment_supported"] is False
    assert result.commercial_data["checkout"]["selected_channel_supported"] is True
    assert result.commercial_data["checkout"]["requires_channel_choice"] is False
    assert "cart_url" not in result.commercial_data["checkout"]
    assert updated.checkout_channel_preference == "whatsapp"
    assert updated.purchase_stage == "checkout_data"
    assert updated.pending_action == "awaiting_checkout_data"
    assert "Nome completo:" in result.reply_text
    assert "Estado/UF:" in result.reply_text
