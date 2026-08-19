from types import SimpleNamespace

import pytest

from app.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _interpretation(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "buy",
        "subject": {"product_type": "produto"},
        "preferences": {},
        "information_needed": [],
        "references_previous_context": True,
        "needs_clarification": False,
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


def _pipeline_settings():
    return SimpleNamespace(
        audio_inbound_enabled=False,
        audio_outbound_enabled=False,
    )


def _presented_state() -> dict:
    return CommerceConversationState(
        active_domain="commerce",
        last_presented_products=[
            {"position": 1, "product_id": "A", "name": "Produto A"},
            {"position": 2, "product_id": "B", "name": "Produto B"},
            {"position": 3, "product_id": "C", "name": "Produto C"},
        ],
        purchase_stage="selection",
    ).model_dump(mode="json")


def _cart_executor(*, fail_get_product: bool = False):
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            if fail_get_product:
                raise RuntimeError("diagnostic failure")
            return {
                "id": arguments["product_id"],
                "name": f"Produto {arguments['product_id']}",
                "current_price": "100.00",
                "available": True,
                "has_variation": False,
            }
        if tool == "create_cart":
            return {
                "cart_id": "CART-1",
                "session_id": "SESSION-1",
                "cart_url": "https://loja.example/checkout/SESSION-1",
            }
        if tool == "get_cart_complete":
            create = next(args for name, args in calls if name == "create_cart")
            return {
                "items": [{
                    "product_id": create["product_id"],
                    "variant_id": create.get("variant_id"),
                    "quantity": create["quantity"],
                }],
                "total": "100.00",
            }
        raise AssertionError(tool)

    return execute, calls


async def _no_openai_responder(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_pipeline_resolves_first_presented_product_and_reaches_cart(
    monkeypatch,
    capsys,
):
    import app.message_pipeline as message_pipeline
    import app.openai_agent as openai_agent
    import app.sales_agent as sales_agent

    interpretation = _interpretation(
        reference_type="list_position",
        reference_position=1,
        purchase_action="create_cart",
    )

    async def interpret(*_args, **_kwargs):
        return interpretation

    execute, calls = _cart_executor()
    monkeypatch.setattr(message_pipeline, "get_settings", _pipeline_settings)
    monkeypatch.setattr(
        message_pipeline,
        "load_commerce_conversation_state",
        lambda **_kwargs: _presented_state(),
    )
    monkeypatch.setattr(
        openai_agent,
        "load_recent_conversation_turns",
        lambda **_kwargs: [
            {"role": "assistant", "content": "1. Produto A\n2. Produto B\n3. Produto C"}
        ],
    )
    monkeypatch.setattr(openai_agent, "interpret_message", interpret)
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent,
        "_sales_response_with_openai",
        _no_openai_responder,
    )

    result = await message_pipeline.process_incoming_message(
        IncomingMessage(
            text="seleção de compra por posição",
            conversation_id="conversation",
            raw={"inbound_id": 10},
        ),
        {},
    )

    assert [name for name, _ in calls] == [
        "get_product",
        "create_cart",
        "get_cart_complete",
    ]
    assert calls[0][1]["product_id"] == "A"
    assert result.safety_reason is None
    output = capsys.readouterr().out
    assert "[sales.purchase.orchestrator]" in output
    assert "'stage': 'reference_resolution', 'status': 'success'" in output
    assert "'stage': 'variant_resolution', 'status': 'success'" in output
    assert "'stage': 'price_resolution', 'status': 'success'" in output
    assert "'stage': 'cart_http', 'status': 'success'" in output
    assert "'stage': 'completed', 'status': 'success'" in output


@pytest.mark.asyncio
async def test_pipeline_direct_product_resolution_reaches_same_cart_path(
    monkeypatch,
):
    import app.message_pipeline as message_pipeline
    import app.openai_agent as openai_agent
    import app.sales_agent as sales_agent

    interpretation = _interpretation(
        subject={
            "product_type": "produto",
            "brand": "Marca",
            "model": "Modelo",
        },
        purchase_action="create_cart",
        ready_for_retrieval=True,
    )

    async def interpret(*_args, **_kwargs):
        return interpretation

    async def resolve_product(_interpretation):
        return AgentResult(
            reply_text="Produto resolvido.",
            intent="commerce",
            commercial_data={
                "products": [{
                    "id": "P1",
                    "name": "Marca Modelo",
                    "brand": "Marca",
                    "model": "Modelo",
                }]
            },
            response_metadata={"used_tray": True},
        )

    execute, calls = _cart_executor()
    monkeypatch.setattr(message_pipeline, "get_settings", _pipeline_settings)
    monkeypatch.setattr(
        message_pipeline,
        "load_commerce_conversation_state",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        openai_agent,
        "load_recent_conversation_turns",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(openai_agent, "interpret_message", interpret)
    monkeypatch.setattr(
        sales_agent,
        "_execute_compiled_product_retrieval",
        resolve_product,
    )
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent,
        "_sales_response_with_openai",
        _no_openai_responder,
    )

    result = await message_pipeline.process_incoming_message(
        IncomingMessage(text="compra direta de produto", raw={"inbound_id": 11}),
        {},
    )

    assert [name for name, _ in calls] == [
        "get_product",
        "create_cart",
        "get_cart_complete",
    ]
    assert calls[0][1]["product_id"] == "P1"
    assert result.safety_reason is None


@pytest.mark.asyncio
async def test_unexpected_pre_cart_exception_records_stage_and_type(
    monkeypatch,
    capsys,
):
    import app.sales_agent as sales_agent

    execute, calls = _cart_executor(fail_get_product=True)
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent,
        "_sales_response_with_openai",
        _no_openai_responder,
    )
    state = CommerceConversationState.from_payload(_presented_state())

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção de compra por posição"),
        {},
        {},
        _interpretation(
            reference_type="list_position",
            reference_position=1,
            purchase_action="create_cart",
        ),
        commerce_state=state,
    )

    assert result.safety_reason == "cart_technical_failure"
    assert not any(name == "create_cart" for name, _ in calls)
    output = capsys.readouterr().out
    assert "[sales.cart.failure]" in output
    assert "'stage': 'product_resolution'" in output
    assert "'exception_type': 'RuntimeError'" in output


@pytest.mark.asyncio
async def test_real_cart_client_logs_safe_request_and_response_shape(capsys):
    from app.tray_adapter_client import TrayAdapterClient

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "cart_id": "C1",
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }

    class Client:
        async def request(self, *_args, **_kwargs):
            return Response()

    await TrayAdapterClient(
        "https://tray.example",
        "secret",
        Client(),
    ).create_cart(
        product_id="P1",
        quantity=1,
        price="100.00",
    )

    output = capsys.readouterr().out
    assert "[sales.cart.http.request]" in output
    assert "'price_valid': True" in output
    assert "[sales.cart.http.response]" in output
    assert "'status_code': 200" in output
    assert "'has_session_id': True" in output
    assert "secret" not in output
    assert "https://loja.example/checkout/S1" not in output
