from types import SimpleNamespace

import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.models import AgentResult, IncomingMessage
from app.order_service import (
    extract_order_reference,
    extract_valid_tax_document,
    find_order_by_customer_document,
    get_order_facts,
    is_order_lookup_request,
)


def test_tax_document_validation_accepts_real_check_digits_only():
    assert extract_valid_tax_document("CPF 529.982.247-25") == ("cpf", "52998224725")
    assert extract_valid_tax_document("CNPJ 11.222.333/0001-81") == ("cnpj", "11222333000181")
    assert extract_valid_tax_document("CPF 529.982.247-24") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Cód.Pedido: 195", "195"),
        ("qual o status do pedido 195?", "195"),
        ("acompanhar pedido nº ABC-123", "abc-123"),
    ],
)
def test_order_reference_is_extracted_from_customer_phrasing(text, expected):
    assert extract_order_reference(text) == expected
    assert is_order_lookup_request(text) is True


@pytest.mark.asyncio
async def test_missing_direct_order_requests_document_and_persists_lookup_state():
    async def execute(tool, arguments):
        assert tool == "get_order_complete"
        assert arguments == {"order_id": "195"}
        return {"error": "commerce_upstream_error", "status_code": 404}

    result = await get_order_facts(
        state=CommerceConversationState(),
        execute=execute,
        order_id="195",
    )
    state = evolve_commerce_state(CommerceConversationState(), result)

    assert result.safety_reason == "order_not_found"
    assert "CPF ou CNPJ" in result.reply_text
    assert state.pending_action == "awaiting_order_customer_document"
    assert state.order_lookup_id == "195"


@pytest.mark.asyncio
async def test_empty_success_payload_is_not_treated_as_an_existing_order():
    async def execute(_tool, _arguments):
        return {"success": False}

    result = await get_order_facts(
        state=CommerceConversationState(),
        execute=execute,
        order_id="195",
    )

    assert result.safety_reason == "order_not_found"
    assert result.commercial_data["success"] is False


@pytest.mark.asyncio
async def test_document_confirms_only_requested_order_for_customer():
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_customer":
            return {"customers": [{"id": "customer-7", "name": "Cliente"}]}
        if tool == "list_orders":
            return {
                "orders": [
                    {
                        "order_id": "tray-900", "code": "195",
                        "customer_id": "customer-7", "status": "A ENVIAR",
                    },
                    {
                        "order_id": "tray-901", "code": "196",
                        "customer_id": "customer-7", "status": "FINALIZADO",
                    },
                ]
            }
        if tool == "get_order_complete":
            return {
                "order_id": "tray-900",
                "status": "ENVIADO",
                "status_group": "shipped",
                "sending_code": "TRACK-900",
            }
        raise AssertionError(tool)

    state = CommerceConversationState(
        pending_action="awaiting_order_customer_document",
        order_lookup_id="195",
    )
    result = await find_order_by_customer_document(
        state=state,
        execute=execute,
        document_kind="cpf",
        document="52998224725",
    )

    assert calls == [
        ("search_customer", {"cpf": "52998224725", "limit": 5}),
        ("list_orders", {"customer_id": "customer-7"}),
        ("get_order_complete", {"order_id": "tray-900"}),
    ]
    assert result.commercial_data["order_id"] == "tray-900"
    assert result.commercial_data["status"] == "ENVIADO"
    assert "tray-901" not in str(result.model_dump())


@pytest.mark.asyncio
async def test_customer_lookup_does_not_reveal_unrequested_orders():
    async def execute(tool, _arguments):
        if tool == "search_customer":
            return {"customers": [{"id": "customer-7"}]}
        if tool == "list_orders":
            return {
                "orders": [{
                    "order_id": "private-999",
                    "code": "195",
                    "customer_id": "another-customer",
                }]
            }
        raise AssertionError(tool)

    result = await find_order_by_customer_document(
        state=CommerceConversationState(
            pending_action="awaiting_order_customer_document",
            order_lookup_id="195",
        ),
        execute=execute,
        document_kind="cpf",
        document="52998224725",
    )

    assert result.safety_reason == "order_customer_mismatch"
    assert "private-999" not in result.reply_text
    assert "private-999" not in str(result.commercial_data)


@pytest.mark.asyncio
async def test_pending_document_is_handled_before_openai_interpretation(monkeypatch):
    import app.openai_agent as openai_agent

    captured = {}

    async def find_order(**kwargs):
        captured.update(kwargs)
        return AgentResult(
            reply_text="Pedido confirmado.",
            intent="commerce",
            commercial_data={"order_id": "195", "success": True},
            response_metadata={"domain": "commerce", "used_tray": True},
        )

    monkeypatch.setattr(openai_agent, "find_order_by_customer_document", find_order)
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_kwargs: [])
    monkeypatch.setattr(
        openai_agent,
        "interpret_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("document recovery must bypass interpretation")
        ),
    )
    monkeypatch.setattr(
        openai_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = CommerceConversationState(
        pending_action="awaiting_order_customer_document",
        order_lookup_id="195",
    )

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="529.982.247-25"),
        {"_commerce_state": state.model_dump(mode="json")},
    )

    assert result.reply_text == "Pedido confirmado."
    assert captured["document_kind"] == "cpf"
    assert captured["document"] == "52998224725"
    assert result.response_metadata["used_openai_interpreter"] is False


@pytest.mark.asyncio
async def test_explicit_order_status_bypasses_openai_and_calls_tray(monkeypatch):
    import app.openai_agent as openai_agent

    captured = {}

    async def get_order(**kwargs):
        captured.update(kwargs)
        return AgentResult(
            reply_text="Informe o CPF ou CNPJ do comprador.",
            intent="commerce",
            safety_reason="order_not_found",
            commercial_data={"success": False},
            response_metadata={"domain": "commerce", "used_tray": True},
        )

    monkeypatch.setattr(openai_agent, "get_order_facts", get_order)
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_kwargs: [])
    monkeypatch.setattr(
        openai_agent,
        "interpret_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("explicit order lookup must bypass interpretation")
        ),
    )

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(
            text="Me tira uma dúvida, fiz um pedido e queria saber como está! Cód.Pedido: 195"
        ),
        {"_commerce_state": {}},
    )

    assert captured["order_id"] == "195"
    assert result.safety_reason == "order_not_found"
    assert result.response_metadata["used_openai_interpreter"] is False
