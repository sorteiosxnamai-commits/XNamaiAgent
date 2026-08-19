import pytest

from app.commerce_context import CommerceConversationState
from app.context_resume import (
    build_contextual_greeting,
    commerce_state_resumable_score,
    has_resumable_commerce,
    is_soft_greeting,
    is_unpaid_order_resume_request,
    merge_commerce_states,
    should_resume_pending_order,
)
from app.models import AgentResult, IncomingMessage


def test_merge_recovers_order_wiped_by_later_greeting():
    latest = {
        "active_domain": "commerce",
        "pending_action": None,
        "order_id": None,
    }
    previous = {
        "order_id": "0CC131B51070AEF",
        "order_payment_url": "https://pay.example/1",
        "pending_action": "awaiting_payment",
        "purchase_stage": "awaiting_payment",
        "active_product": {"product_id": "1", "name": "Seiko"},
    }
    merged = merge_commerce_states(latest, previous)
    assert merged["order_id"] == "0CC131B51070AEF"
    assert merged["order_payment_url"] == "https://pay.example/1"
    assert merged["pending_action"] == "awaiting_payment"


def test_soft_greeting_and_unpaid_resume_detection():
    assert is_soft_greeting("Opa, boa noite")
    assert is_unpaid_order_resume_request(
        "acabamos de conversar, eu nao fiz o pagamento ainda"
    )
    state = CommerceConversationState(
        order_id="0CC131B51070AEF",
        order_payment_url="https://pay.example/1",
        pending_action="awaiting_payment",
    )
    # Greeting keeps memory loaded but must not auto-dump payment.
    assert should_resume_pending_order("Opa, boa noite", state) is False
    assert should_resume_pending_order("como esta meu pedido?", state) is False
    assert should_resume_pending_order(
        "acabamos de conversar, eu nao fiz o pagamento ainda",
        state,
    ) is True
    assert should_resume_pending_order("sim", state) is True


def test_contextual_greeting_is_soft_and_non_intrusive():
    state = CommerceConversationState(
        order_id="0CC131B51070AEF",
        order_payment_url="https://pay.example/1",
        pending_action="awaiting_payment",
        active_product={"product_id": "1", "name": "Seiko SRPD79K1"},
    )
    result = build_contextual_greeting(state)
    assert "0CC131B51070AEF" not in result.reply_text
    assert "https://pay.example/1" not in result.reply_text
    assert "Seiko" not in result.reply_text
    assert result.response_metadata["response_source"] == "context_resume_soft"


@pytest.mark.asyncio
async def test_pipeline_greeting_keeps_memory_without_dumping_order(monkeypatch):
    import app.message_pipeline as pipeline
    import app.openai_agent as openai_agent

    state = CommerceConversationState(
        order_id="0CC131B51070AEF",
        order_payment_url="https://pay.example/pedido",
        pending_action="awaiting_payment",
        purchase_stage="awaiting_payment",
        active_product={"product_id": "77", "name": "Seiko"},
    ).model_dump(mode="json")

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
    monkeypatch.setattr(
        pipeline,
        "load_commerce_conversation_state",
        lambda **_kwargs: state,
    )
    monkeypatch.setattr(pipeline, "persist_customer_commerce_session", lambda **_kwargs: None)
    monkeypatch.setattr(pipeline, "upsert_customer_identity_links", lambda *a, **k: None)
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_kwargs: [])
    monkeypatch.setattr(
        openai_agent,
        "interpret_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("soft greeting before interpret")),
    )
    monkeypatch.setattr(
        openai_agent,
        "inspect_order_payment",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no payment dump on greeting")),
    )

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-1",
        sender_phone="5543999999999",
        sender_key="whatsapp:5543999999999",
        text="Opa, boa noite",
        raw={"inbound_id": 10},
    )
    result = await pipeline.process_incoming_message(incoming, {"found": False})
    assert "0CC131B51070AEF" not in result.reply_text
    assert "https://pay.example/pedido" not in result.reply_text
    assert result.response_metadata["commerce_state"]["order_id"] == "0CC131B51070AEF"
    assert result.response_metadata["working_memory"]["payment_pending"] is True


@pytest.mark.asyncio
async def test_order_status_uses_recovered_state_order_id(monkeypatch):
    import app.message_pipeline as pipeline
    import app.openai_agent as openai_agent

    state = {
        "order_id": "0CC131B51070AEF",
        "pending_action": "awaiting_payment",
        "order_payment_url": "https://pay.example/pedido",
    }
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
    monkeypatch.setattr(
        pipeline,
        "load_commerce_conversation_state",
        lambda **_kwargs: state,
    )
    monkeypatch.setattr(pipeline, "persist_customer_commerce_session", lambda **_kwargs: None)
    monkeypatch.setattr(pipeline, "upsert_customer_identity_links", lambda *a, **k: None)
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_kwargs: [])

    async def fake_facts(*, state, execute, order_id=None, allow_customer_recovery=True):
        assert order_id is None
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
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no interpret")),
    )

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-1",
        sender_phone="5543999999999",
        text="como esta meu pedido?",
        raw={"inbound_id": 11},
    )
    result = await pipeline.process_incoming_message(incoming, {"found": False})
    assert "0CC131B51070AEF" in result.reply_text


def test_resumable_score_prefers_order_over_empty_greeting_state():
    assert commerce_state_resumable_score({"order_id": "1"}) > commerce_state_resumable_score({})
    assert has_resumable_commerce({"cart_session_id": "abc"}) is True


def test_identity_person_key_prefers_cpf():
    from app.customer_identity import person_key_from_parts

    assert person_key_from_parts(cpf="12345678901", phone="5543999999999") == "cpf:12345678901"
    assert person_key_from_parts(phone="5543999999999") == "phone:5543999999999"
    assert person_key_from_parts(sender_key="instagram:abc") == "sender:instagram:abc"


def test_working_memory_is_compact_and_policy_aware():
    from app.working_memory import WORKING_MEMORY_USAGE_POLICY, build_working_memory

    memory = build_working_memory(
        CommerceConversationState(
            order_id="0CC131B51070AEF",
            order_payment_url="https://pay.example/1",
            pending_action="awaiting_payment",
            active_product={"product_id": "1", "name": "Seiko"},
            checkout_draft={
                "customer": {
                    "name": "Paulo",
                    "cpf": "07281035918",
                    "email": "a@b.com",
                    "phone": "85999498149",
                }
            },
        )
    )
    assert memory["has_open_order"] is True
    assert memory["payment_pending"] is True
    assert memory["known_checkout_fields"]["cpf"] is True
    assert "07281035918" not in str(memory["known_checkout_fields"])
    assert memory["usage_policy"] == WORKING_MEMORY_USAGE_POLICY


@pytest.mark.asyncio
async def test_pipeline_always_passes_sender_key_with_conversation_id(monkeypatch):
    import app.message_pipeline as pipeline

    seen = {}

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

    def load_state(**kwargs):
        seen.update(kwargs)
        return {}

    async def generate(incoming, customer_context):
        return AgentResult(reply_text="ok", intent="general")

    monkeypatch.setattr(pipeline, "load_commerce_conversation_state", load_state)
    monkeypatch.setattr(pipeline, "generate_agent_reply_async", generate)
    monkeypatch.setattr(pipeline, "upsert_customer_identity_links", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "persist_customer_commerce_session", lambda **_kwargs: None)

    await pipeline.process_incoming_message(
        IncomingMessage(
            channel="whatsapp",
            conversation_id="conv-new",
            sender_key="whatsapp:5543999999999",
            sender_phone="5543999999999",
            text="oi",
            raw={"inbound_id": 99},
        ),
        {"found": False},
    )
    assert seen["sender_key"] == "whatsapp:5543999999999"
    assert seen["conversation_id"] == "conv-new"
