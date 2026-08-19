import pytest

from app.commerce_context import CommerceConversationState
from app.context_resume import is_payment_link_request, is_unpaid_order_resume_request
from app.models import AgentResult, IncomingMessage
from app.order_context_recovery import (
    extract_handles_from_conversation,
    hydrate_state_from_handles,
)
from app.order_service import (
    extract_order_reference,
    is_order_lookup_request,
    order_reference_candidates,
)


def test_extracts_order_payment_and_customer_from_transcript():
    turns = [
        {
            "role": "user",
            "content": (
                "Paulo Regis Tironi\n07281035918\n85999498149\n"
                "tironinho@hotmail.com"
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Pedido criado. Pague em "
                "https://www.newstorerj.com.br/loja/pagamento.php"
                "?loja=687890&pedido=0CC131B51070AEF"
            ),
        },
    ]
    handles = extract_handles_from_conversation(
        state=CommerceConversationState(),
        recent_turns=turns,
        message_text="como ficou o meu pedido do seiko?",
    )
    assert "0CC131B51070AEF" in handles["order_ids"]
    assert any("pedido=0CC131B51070AEF" in url for url in handles["payment_urls"])
    assert ("cpf", "07281035918") in handles["documents"]
    assert "tironinho@hotmail.com" in handles["emails"]


def test_hydrate_fills_missing_order_and_checkout_fields():
    state = CommerceConversationState()
    handles = {
        "order_ids": ["0CC131B51070AEF"],
        "payment_urls": [
            "https://www.newstorerj.com.br/loja/pagamento.php?loja=687890&pedido=0CC131B51070AEF"
        ],
        "emails": ["tironinho@hotmail.com"],
        "documents": [("cpf", "07281035918")],
    }
    hydrated = hydrate_state_from_handles(state, handles)
    assert hydrated.order_id == "0CC131B51070AEF"
    assert hydrated.order_payment_url.endswith("pedido=0CC131B51070AEF")
    assert hydrated.pending_action == "awaiting_payment"
    assert hydrated.checkout_draft.customer.cpf == "07281035918"
    assert hydrated.checkout_draft.customer.email == "tironinho@hotmail.com"


def test_detects_followup_order_and_pix_requests():
    assert is_order_lookup_request("como ficou") is True
    assert is_order_lookup_request("como ficou o meu pedido do seiko?") is True
    assert is_payment_link_request("me da o pix para pagamento") is True
    assert is_payment_link_request("me da o link para pagamento") is True
    assert is_payment_link_request("quero o link de pagamento") is True
    assert is_unpaid_order_resume_request(
        "consegue confirmar se o pagamento caiu?"
    ) is True


def test_splits_glued_store_code_and_internal_order_id():
    candidates = order_reference_candidates("0CC131B51070AEF25400")
    # Tray expects the numeric internal id; store hex codes often 422.
    assert candidates[0] == "25400"
    assert candidates[1].upper() == "0CC131B51070AEF"
    assert extract_order_reference(
        "mais e esse pedido 0CC131B51070AEF25400?"
    ) == "25400"


@pytest.mark.asyncio
async def test_get_order_facts_retries_split_candidates_after_422():
    from app.order_service import get_order_facts

    calls: list[str] = []

    async def execute(name, args):
        assert name == "get_order_complete"
        order_id = str(args["order_id"])
        calls.append(order_id)
        if order_id == "25400":
            return {
                "success": True,
                "order_id": "25400",
                "status": "Aguardando pagamento",
                "status_group": "open",
            }
        if order_id.lower() in {"0cc131b51070aef25400", "0cc131b51070aef"}:
            return {"error": "commerce_upstream_error", "status_code": 422}
        return {"error": "commerce_upstream_error", "status_code": 404}

    result = await get_order_facts(
        state=CommerceConversationState(),
        execute=execute,
        order_id="0CC131B51070AEF25400",
    )
    assert result.commercial_data["order_id"] == "25400"
    assert calls[0] == "25400"


@pytest.mark.asyncio
async def test_get_order_facts_resolves_store_code_via_cpf_after_422():
    from app.commerce_context import CheckoutCustomer, CheckoutDraft
    from app.order_service import get_order_facts

    calls: list[tuple[str, dict]] = []

    async def execute(name, args):
        calls.append((name, args))
        if name == "get_order_complete":
            if str(args["order_id"]) == "25400":
                return {
                    "success": True,
                    "order_id": "25400",
                    "status": "Aguardando pagamento",
                    "status_group": "open",
                }
            return {"error": "commerce_upstream_error", "status_code": 422}
        if name == "search_customer":
            return {"customers": [{"id": 99}]}
        if name == "list_orders":
            return {
                "orders": [
                    {
                        "id": 25400,
                        "code": "0CC131B51070AEF",
                        "status": "Aguardando pagamento",
                        "status_group": "open",
                    }
                ]
            }
        raise AssertionError(name)

    state = CommerceConversationState(
        order_id="0CC131B51070AEF",
        checkout_draft=CheckoutDraft(
            customer=CheckoutCustomer(cpf="12345678909"),
        ),
    )
    result = await get_order_facts(
        state=state,
        execute=execute,
        order_id="0CC131B51070AEF",
    )
    assert result.commercial_data["order_id"] == "25400"
    assert any(name == "search_customer" for name, _ in calls)


@pytest.mark.asyncio
async def test_pipeline_recovers_order_from_transcript_for_status(monkeypatch):
    import app.message_pipeline as pipeline
    import app.openai_agent as openai_agent

    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "audio_inbound_enabled": False,
                "audio_outbound_enabled": False,
                "agent_policy_mode": "off",
                "agent_factual_validation_mode": "off",
                "agent_quality_judge_mode": "off",
                "agent_trusted_fact_domains": "",
                "max_reply_chars": 900,
            },
        )(),
    )
    monkeypatch.setattr(pipeline, "load_commerce_conversation_state", lambda **_k: {})
    monkeypatch.setattr(pipeline, "persist_customer_commerce_session", lambda **_k: None)
    monkeypatch.setattr(pipeline, "upsert_customer_identity_links", lambda *a, **k: None)
    monkeypatch.setattr(
        openai_agent,
        "load_recent_conversation_turns",
        lambda **_k: [
            {
                "role": "assistant",
                "content": (
                    "Pedido criado: "
                    "https://pay.example/pagamento.php?loja=1&pedido=0CC131B51070AEF"
                ),
            }
        ],
    )

    async def fake_facts(*, state, execute, order_id=None, allow_customer_recovery=True):
        assert state.order_id == "0CC131B51070AEF"
        return AgentResult(
            reply_text="Pedido 0CC131B51070AEF aguardando pagamento.",
            intent="commerce",
            response_metadata={"domain": "commerce", "used_tray": True},
        )

    monkeypatch.setattr(openai_agent, "get_order_facts", fake_facts)
    monkeypatch.setattr(
        openai_agent,
        "interpret_message",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no interpret")),
    )

    result = await pipeline.process_incoming_message(
        IncomingMessage(
            channel="whatsapp",
            conversation_id="conv-1",
            sender_phone="85999498149",
            text="como ficou o meu pedido do seiko?",
            raw={"inbound_id": 21},
        ),
        {"found": False},
    )
    assert "0CC131B51070AEF" in result.reply_text


@pytest.mark.asyncio
async def test_pipeline_pix_request_reuses_payment_link_from_transcript(monkeypatch):
    import app.message_pipeline as pipeline
    import app.openai_agent as openai_agent

    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "audio_inbound_enabled": False,
                "audio_outbound_enabled": False,
                "agent_policy_mode": "off",
                "agent_factual_validation_mode": "off",
                "agent_quality_judge_mode": "off",
                "agent_trusted_fact_domains": "",
                "max_reply_chars": 900,
            },
        )(),
    )
    monkeypatch.setattr(pipeline, "load_commerce_conversation_state", lambda **_k: {})
    monkeypatch.setattr(pipeline, "persist_customer_commerce_session", lambda **_k: None)
    monkeypatch.setattr(pipeline, "upsert_customer_identity_links", lambda *a, **k: None)
    payment_url = (
        "https://www.newstorerj.com.br/loja/pagamento.php"
        "?loja=687890&pedido=0CC131B51070AEF"
    )
    monkeypatch.setattr(
        openai_agent,
        "load_recent_conversation_turns",
        lambda **_k: [{"role": "assistant", "content": f"Pague aqui: {payment_url}"}],
    )

    async def fake_payment(*, state, execute, order_id=None):
        assert order_id == "0CC131B51070AEF"
        return AgentResult(
            reply_text="Consulta sem URL.",
            intent="commerce",
            commercial_data={"order_id": order_id, "payment": {"status": "pending"}},
            response_metadata={"domain": "commerce", "used_tray": True},
        )

    monkeypatch.setattr(openai_agent, "inspect_order_payment", fake_payment)
    monkeypatch.setattr(
        openai_agent,
        "handle_sales_message",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no new cart")),
    )

    result = await pipeline.process_incoming_message(
        IncomingMessage(
            channel="whatsapp",
            conversation_id="conv-1",
            sender_phone="85999498149",
            text="me da o link para pagamento",
            raw={"inbound_id": 22},
        ),
        {"found": False},
    )
    assert "0CC131B51070AEF" in result.reply_text
    assert payment_url in result.reply_text
    assert result.response_metadata.get("response_source") == "context_resume_payment_url"
