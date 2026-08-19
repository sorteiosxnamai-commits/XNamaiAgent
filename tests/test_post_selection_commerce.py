from types import SimpleNamespace

import pytest

from app.cart_service import CartItemRequest, create_cart_items_checkout
from app.commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    evolve_commerce_state,
)
from app.models import IncomingMessage, SalesInterpretation
from app.payment_service import inspect_payment_options
from app.product_media import resolve_product_image


def _reference(product_id: str, name: str | None = None):
    return CommerceProductReference(product_id=product_id, name=name)


def _interpretation(**overrides):
    payload = {
        "domain": "commerce",
        "goal": "buy",
        "subject": {"product_type": "produto"},
        "preferences": {},
        "information_needed": ["catalog"],
        "references_previous_context": True,
        "needs_clarification": False,
        "purchase_action": "create_cart",
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


@pytest.mark.asyncio
async def test_real_wrapped_detail_price_reaches_cart_post():
    from app.tray_tools import execute_tool

    class Adapter:
        def __init__(self):
            self.calls = []

        async def get_product(self, product_id):
            return {
                "data": {
                    "product": {
                        "id": product_id,
                        "current_price": "6199.99",
                        "available": True,
                    }
                }
            }

        async def create_cart(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "cart_id": "C1",
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }

        async def get_cart_complete(self, session_id):
            return {
                "cart": {
                    "session_id": session_id,
                    "total": "6199.99",
                    "items": [{"product_id": "1025", "quantity": 1}],
                }
            }

    adapter = Adapter()

    async def execute(tool, arguments):
        return await execute_tool(tool, arguments, adapter)

    result = await create_cart_items_checkout(
        item_requests=[CartItemRequest(_reference("1025"), quantity=1)],
        state=CommerceConversationState(),
        execute=execute,
    )

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["product_id"] == "1025"
    assert adapter.calls[0]["variant_id"] is None
    assert adapter.calls[0]["quantity"] == 1
    assert adapter.calls[0]["price"] == "6199.99"
    assert len(adapter.calls[0]["session_id"]) == 32
    int(adapter.calls[0]["session_id"], 16)
    assert result.safety_reason is None


def test_cart_price_resolution_uses_positive_structured_commercial_value():
    from app.product_retrieval import resolve_commercial_price

    result = resolve_commercial_price(
        {
            "current_price": "0.00",
            "promotional_price": "4.999,90",
            "price": "5199.90",
        },
        require_positive=True,
    )

    assert str(result.amount) == "4999.90"
    assert result.source == "promotional_price"


@pytest.mark.asyncio
async def test_multi_item_uses_one_session_and_verifies_complete_cart():
    calls = []
    cart_session = None

    async def execute(tool, arguments):
        nonlocal cart_session
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": arguments["product_id"],
                "current_price": "100.00",
                "available": True,
            }
        if tool == "create_cart":
            if cart_session is None:
                cart_session = arguments["session_id"]
            assert arguments["session_id"] == cart_session
            return {
                "cart_id": "C1",
                "session_id": cart_session,
                "cart_url": f"https://loja.example/checkout/{cart_session}",
            }
        if tool == "get_cart_complete":
            assert arguments["session_id"] == cart_session
            return {
                "total": "300.00",
                "items": [
                    {"product_id": "A", "quantity": 1},
                    {"product_id": "B", "quantity": 2},
                ],
            }
        raise AssertionError(tool)

    result = await create_cart_items_checkout(
        item_requests=[
            CartItemRequest(_reference("A"), quantity=1, position=1),
            CartItemRequest(_reference("B"), quantity=2, position=2),
        ],
        state=CommerceConversationState(),
        execute=execute,
    )

    create_calls = [args for tool, args in calls if tool == "create_cart"]
    assert len(create_calls) == 2
    assert len(create_calls[0]["session_id"]) == 32
    int(create_calls[0]["session_id"], 16)
    assert create_calls[1]["session_id"] == create_calls[0]["session_id"]
    assert result.response_metadata["cart_state"]["cart_items"] == [
        {
            "product_id": "A",
            "variant_id": None,
            "quantity": 1,
            "unit_price": "100.00",
            "original_price": None,
        },
        {
            "product_id": "B",
            "variant_id": None,
            "quantity": 2,
            "unit_price": "100.00",
            "original_price": None,
        },
    ]
    assert result.commercial_data["cart"]["total"] == "300.00"
    evolved = evolve_commerce_state(CommerceConversationState(), result)
    assert [item.product_id for item in evolved.cart_items] == ["A", "B"]


@pytest.mark.asyncio
async def test_second_item_failure_reports_verified_partial_state():
    posts = 0

    async def execute(tool, arguments):
        nonlocal posts
        if tool == "get_product":
            return {
                "id": arguments["product_id"],
                "price": "50.00",
                "available": True,
            }
        if tool == "create_cart":
            posts += 1
            if posts == 2:
                return {"error": "offline", "status_code": 503}
            return {
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            return {
                "total": "50.00",
                "items": [{"product_id": "A", "quantity": 1}],
            }
        raise AssertionError(tool)

    result = await create_cart_items_checkout(
        item_requests=[
            CartItemRequest(_reference("A")),
            CartItemRequest(_reference("B")),
        ],
        state=CommerceConversationState(),
        execute=execute,
    )

    assert result.safety_reason == "cart_partial_failure"
    assert result.commercial_data["cart"]["verification_ok"] is True
    assert result.response_metadata["cart_state"]["cart_items"] == [
        {
            "product_id": "A",
            "variant_id": None,
            "quantity": 1,
            "unit_price": "50.00",
            "original_price": "50.00",
        },
    ]


@pytest.mark.asyncio
async def test_structured_multi_positions_resolve_to_real_ids(monkeypatch):
    import app.sales_agent as sales_agent

    captured = {}

    async def create_items(**kwargs):
        captured["requests"] = kwargs["item_requests"]
        from app.models import AgentResult
        return AgentResult(
            reply_text="ok",
            intent="commerce",
            commercial_data={"cart": {"status": "cart_created"}},
            response_metadata={
                "purchase_stage": "cart_created",
                "used_tray": True,
            },
        )

    monkeypatch.setattr(sales_agent, "create_cart_items_checkout", create_items)
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = CommerceConversationState(last_presented_products=[
        {"position": 1, "product_id": "A", "name": "Produto A"},
        {"position": 2, "product_id": "B", "name": "Produto B"},
    ])
    interpretation = _interpretation(purchase_items=[
        {"reference_type": "list_position", "reference_position": 1, "quantity": 1},
        {"reference_type": "list_position", "reference_position": 2, "quantity": 2},
    ])

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção múltipla"),
        {},
        {},
        interpretation,
        commerce_state=state,
    )

    assert result is not None
    assert [
        (item.product_reference.product_id, item.quantity)
        for item in captured["requests"]
    ] == [("A", 1), ("B", 2)]


@pytest.mark.asyncio
async def test_ambiguous_explicit_item_is_not_chosen_arbitrarily(monkeypatch):
    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = CommerceConversationState(last_presented_products=[
        {"position": 1, "product_id": "A", "name": "Linha Campo Azul"},
        {"position": 2, "product_id": "B", "name": "Linha Campo Verde"},
    ])
    interpretation = _interpretation(purchase_items=[{
        "reference_type": "explicit_product",
        "explicit_product_name": "Linha Campo",
        "quantity": 2,
    }])

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção ambígua"),
        {},
        {},
        interpretation,
        commerce_state=state,
    )

    assert result.safety_reason == "ambiguous_purchase_item"


@pytest.mark.asyncio
async def test_product_and_position_images_use_real_tray_urls(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        assert tool == "get_product"
        return {
            "id": arguments["product_id"],
            "name": f"Produto {arguments['product_id']}",
            "primary_image_url": f"https://cdn.tray.example/{arguments['product_id']}.jpg",
        }

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = CommerceConversationState(
        active_product={"product_id": "A"},
        last_presented_products=[
            {"position": 1, "product_id": "A"},
            {"position": 2, "product_id": "B"},
        ],
    )
    current = await sales_agent.handle_sales_message(
        IncomingMessage(text="imagem atual"),
        {},
        {},
        _interpretation(
            goal="inspect",
            purchase_action=None,
            image_request=True,
            reference_type="current_product",
        ),
        commerce_state=state,
    )
    second = await sales_agent.handle_sales_message(
        IncomingMessage(text="imagem da posição"),
        {},
        {},
        _interpretation(
            goal="inspect",
            purchase_action=None,
            image_request=True,
            reference_type="list_position",
            reference_position=2,
        ),
        commerce_state=state,
    )

    assert "https://cdn.tray.example/A.jpg" in current.reply_text
    assert "https://cdn.tray.example/B.jpg" in second.reply_text


@pytest.mark.asyncio
async def test_inbound_photo_with_image_request_identifies_product(monkeypatch):
    """Customer photo must not be blocked by the outbound show-image guard."""
    from app import sales_agent
    from app.models import AgentResult

    async def fake_image_search(message):
        assert message.image_url
        return AgentResult(
            reply_text="Encontrei o Christopher Ward Sealander.",
            intent="commerce",
            safety_reason=None,
            commercial_data={
                "products": [
                    {"id": "cw-1", "name": "Christopher Ward C63 Sealander"}
                ]
            },
            response_metadata={"image_search": True, "used_tray": True},
        )

    monkeypatch.setattr(
        "app.image_product_id.handle_image_product_search",
        fake_image_search,
    )
    monkeypatch.setattr(
        "app.image_product_id.image_search_eligible",
        lambda message: True,
    )
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(
            text="qual o preço do relogio da foto?",
            input_modality="text_with_image",
            attachment_type="image",
            image_url="https://cdn.example.com/sealander.jpg",
        ),
        {},
        {},
        _interpretation(
            goal="inspect",
            purchase_action=None,
            image_request=True,
        ),
        commerce_state=CommerceConversationState(),
    )

    assert result is not None
    assert "Sealander" in result.reply_text
    assert "antes de consultar a imagem" not in result.reply_text.casefold()
    assert result.commercial_data["products"][0]["id"] == "cw-1"


@pytest.mark.asyncio
async def test_price_on_plausible_matches_asks_which_not_kingfisher(monkeypatch):
    """Soft nearby siblings must not be priced as the confirmed photo product."""
    from app import sales_agent

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )

    state = CommerceConversationState(
        active_domain="commerce",
        product_resolution_state="plausible_matches",
        last_presented_products=[
            {
                "position": 1,
                "product_id": "12295",
                "name": "Christopher Ward C63 Sealander Kingfisher",
                "brand": "Christopher Ward",
            },
            {
                "position": 2,
                "product_id": "12296",
                "name": "Christopher Ward C63 Sealander Dagger",
                "brand": "Christopher Ward",
            },
        ],
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="qual o preço?"),
        {},
        {},
        _interpretation(
            goal="inspect",
            purchase_action=None,
            reference_type="previous_recommendation",
        ),
        commerce_state=state,
    )

    assert result is not None
    assert result.response_metadata.get("fallback_reason") == (
        "plausible_matches_price_blocked"
    ) or result.safety_reason == "exact_product_ambiguous_brand"
    assert "Kingfisher" not in (result.reply_text or "") or "qual você quer" in (
        result.reply_text or ""
    ).casefold()
    assert "qual você quer" in (result.reply_text or "").casefold()
    assert "12295" not in (result.reply_text or "")


@pytest.mark.asyncio
async def test_deictic_price_without_image_ignores_stale_active(monkeypatch):
    """Caption-only 'qual o preço desse?' must not quote the previous watch."""
    from app import sales_agent

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )

    state = CommerceConversationState(
        active_domain="commerce",
        product_resolution_state="found_available",
        active_product={
            "product_id": "15716",
            "name": "Relógio Christopher Ward C63 Sealander Automático Rosa",
            "brand": "Christopher Ward",
        },
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="qual o preço desse?"),
        {},
        {},
        _interpretation(
            goal="inspect",
            purchase_action=None,
            reference_type="current_product",
        ),
        commerce_state=state,
    )

    assert result is not None
    assert result.response_metadata.get("fallback_reason") == (
        "deictic_price_without_image"
    )
    assert "15716" not in (result.reply_text or "")
    assert "Sealander" not in (result.reply_text or "")
    assert "foto" in (result.reply_text or "").casefold()


@pytest.mark.asyncio
async def test_inbound_image_ignores_stale_kingfisher_context(monkeypatch):
    from app import sales_agent
    from app.models import AgentResult

    async def fake_image_search(message):
        assert message.image_url
        return AgentResult(
            reply_text="Pela foto, o Rosa C63-36ADA4.",
            intent="commerce",
            commercial_data={
                "products": [{"id": "rosa-1", "name": "Sealander Rosa"}],
            },
            response_metadata={
                "used_tray": True,
                "response_source": "image_vision",
            },
        )

    monkeypatch.setattr(
        "app.image_product_id.handle_image_product_search",
        fake_image_search,
    )
    monkeypatch.setattr(
        "app.image_product_id.image_search_eligible",
        lambda message: True,
    )
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )

    state = CommerceConversationState(
        active_domain="commerce",
        active_product={
            "product_id": "12295",
            "name": "Kingfisher",
            "brand": "Christopher Ward",
        },
        product_resolution_state="found_available",
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(
            text="qual o preço?",
            input_modality="text_with_image",
            attachment_type="image",
            image_url="https://cdn.example.com/rosa.jpg",
        ),
        {},
        {},
        _interpretation(
            goal="inspect",
            purchase_action=None,
            reference_type="current_product",
        ),
        commerce_state=state,
    )

    assert result is not None
    assert "Rosa" in result.reply_text
    assert "Kingfisher" not in result.reply_text


@pytest.mark.asyncio
async def test_image_request_without_product_asks_for_photo_not_name_first(monkeypatch):
    from app import sales_agent

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="não sei o nome"),
        {},
        {},
        _interpretation(
            goal="inspect",
            purchase_action=None,
            image_request=True,
        ),
        commerce_state=CommerceConversationState(),
    )

    assert result is not None
    assert "antes de consultar a imagem" not in result.reply_text.casefold()
    assert "foto" in result.reply_text.casefold()


@pytest.mark.asyncio
async def test_missing_image_is_honest():
    async def execute(_tool, _arguments):
        return {"id": "A", "name": "Produto sem foto"}

    result = await resolve_product_image(
        product_reference=_reference("A"),
        execute=execute,
    )

    assert result.safety_reason == "product_image_not_available"
    assert result.response_metadata.get("outbound_image_url") is None
    assert result.response_metadata["image_url_found"] is False
    assert result.response_metadata["media_send_supported"] is False
    assert result.response_metadata["media_send_failed"] is False


@pytest.mark.asyncio
async def test_brevo_conversations_image_fallback_remains_text(monkeypatch):
    import app.brevo_client as brevo
    from app.models import AgentResult

    monkeypatch.setattr(
        brevo,
        "get_settings",
        lambda: SimpleNamespace(
            dry_run=True,
            brevo_reply_mode="dry_run",
            brevo_send_audio_as_attachment=False,
        ),
    )
    result = AgentResult(
        reply_text="Imagem oficial:\nhttps://cdn.tray.example/A.jpg",
        response_metadata={
            "outbound_image_url": "https://cdn.tray.example/A.jpg",
        },
    )

    sent = await brevo.send_brevo_reply(IncomingMessage(visitor_id="V1"), result)

    assert sent.provider_response["text"].endswith("/A.jpg")
    assert "image" not in sent.provider_response
    assert result.response_metadata["image_url_found"] is True
    assert result.response_metadata["media_send_supported"] is False
    assert result.response_metadata["media_send_failed"] is False
    assert result.response_metadata["fallback_link_sent"] is False
    assert result.response_metadata["fallback_link_failed"] is False


@pytest.mark.asyncio
async def test_image_fallback_delivery_failure_is_not_reported_as_native_media(monkeypatch):
    import app.brevo_client as brevo
    from app.models import AgentResult

    monkeypatch.setattr(
        brevo,
        "get_settings",
        lambda: SimpleNamespace(
            dry_run=False,
            brevo_reply_mode="conversations",
            brevo_send_audio_as_attachment=False,
        ),
    )

    async def fail_send(_incoming, _text, *, audio_file=None):
        assert audio_file is None
        return brevo.BrevoSendResult(
            ok=False,
            dry_run=False,
            error="provider_failure",
        )

    monkeypatch.setattr(brevo, "_send_conversations_reply", fail_send)
    result = AgentResult(
        reply_text="Imagem oficial:\nhttps://cdn.tray.example/A.jpg",
        response_metadata={
            "outbound_image_url": "https://cdn.tray.example/A.jpg",
        },
    )

    sent = await brevo.send_brevo_reply(
        IncomingMessage(visitor_id="V1"),
        result,
    )

    assert sent.ok is False
    assert result.response_metadata["image_url_found"] is True
    assert result.response_metadata["media_send_supported"] is False
    assert result.response_metadata["media_send_failed"] is False
    assert result.response_metadata["fallback_link_sent"] is False
    assert result.response_metadata["fallback_link_failed"] is True


@pytest.mark.asyncio
async def test_payment_options_and_requested_installment_use_tray_values():
    async def execute(tool, arguments):
        if tool == "get_cart_complete":
            assert arguments == {"session_id": "S1"}
            return {"items": [], "total": "1000.00"}
        assert tool == "get_payment_options"
        assert arguments == {"cart_session_id": "S1"}
        return {
            "payment_options": {
                "pix": {"value": 900.00},
                "installments": [
                    {"count": 10, "value": 100.00, "interest": True},
                ],
            }
        }
    result = await inspect_payment_options(
        state=CommerceConversationState(
            cart_session_id="S1",
            cart_url="https://loja.example/checkout/S1",
        ),
        installment_count=10,
        execute=execute,
    )

    assert result.commercial_data["requested_installment"] == {
        "count": 10,
        "value": 100.00,
        "interest": True,
    }
    assert result.commercial_data["payment_options"]["pix"]["value"] == 900.00


@pytest.mark.asyncio
async def test_unavailable_installment_is_not_calculated():
    async def execute(_tool, _arguments):
        return {
            "payment_options": {
                "installments": [{"count": 6, "value": 170.00}],
            }
        }

    result = await inspect_payment_options(
        state=CommerceConversationState(cart_session_id="S1"),
        installment_count=10,
        execute=execute,
    )

    assert result.commercial_data["requested_installment"] is None
    assert "não informou" in result.reply_text


@pytest.mark.asyncio
async def test_payment_question_in_sales_flow_uses_active_cart(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        return {
            "payment_options": {
                "installments": [{"count": 10, "value": 123.45}],
            }
        }

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="parcelamento"),
        {},
        {},
        _interpretation(
            goal="inspect",
            purchase_action=None,
            payment_action="installment",
            installment_count=10,
            information_needed=["payment"],
        ),
        commerce_state=CommerceConversationState(
            active_domain="commerce",
            cart_session_id="S1",
            cart_url="https://loja.example/checkout/S1",
        ),
    )

    assert calls == [
        ("get_cart_complete", {"session_id": "S1"}),
        ("get_payment_options", {"cart_session_id": "S1"}),
    ]
    assert result.commercial_data["requested_installment"]["value"] == 123.45


def test_sales_responder_forbids_turning_preferences_into_product_facts():
    from app.sales_agent import SALES_RESPONDER_INSTRUCTIONS

    assert "Preferências do cliente no plano não são fatos confirmados" in (
        SALES_RESPONDER_INSTRUCTIONS
    )
    assert "dimensões reais" in SALES_RESPONDER_INSTRUCTIONS
