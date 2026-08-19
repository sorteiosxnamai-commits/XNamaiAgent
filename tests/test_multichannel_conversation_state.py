from types import SimpleNamespace

import pytest

from app.models import AgentResult, IncomingMessage


def test_context_filter_priority_is_conversation_then_sender_key_then_phone():
    from app.db import resolve_context_filter

    query, params = resolve_context_filter("conv-1", "instagram:user-a", "5511999999999")
    assert "conversation_id" in query
    assert params == {"conversation_id": "conv-1"}

    query, params = resolve_context_filter(None, "instagram:user-a", "5511999999999")
    assert "sender_key" in query
    assert params == {"sender_key": "instagram:user-a"}

    query, params = resolve_context_filter(None, None, "5511999999999")
    assert "sender_phone" in query
    assert params == {"sender_phone": "5511999999999"}


def test_social_memory_is_isolated_by_user_and_channel():
    import app.commerce_router as router

    router.clear_commerce_memory()
    instagram_a = IncomingMessage(channel="instagram", sender_key="instagram:123")
    instagram_b = IncomingMessage(channel="instagram", sender_key="instagram:456")
    facebook_same_external_id = IncomingMessage(channel="facebook", sender_key="facebook:123")

    router._remember_product(instagram_a, {"id": "product-a", "name": "Relógio A"})

    assert router._remembered_product(instagram_a)["id"] == "product-a"
    assert router._remembered_product(instagram_b) is None
    assert router._remembered_product(facebook_same_external_id) is None


@pytest.mark.asyncio
async def test_phone_less_social_conversation_reloads_commerce_state(monkeypatch):
    import app.message_pipeline as pipeline

    stored = {}
    seen_states = []

    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(audio_inbound_enabled=False, audio_outbound_enabled=False),
    )

    def load_state(*, conversation_id, sender_phone, before_inbound_id, sender_key=None):
        assert sender_phone is None
        return stored.get(conversation_id or sender_key, {})

    async def generate(incoming, customer_context):
        state = customer_context["_commerce_state"]
        seen_states.append(state)
        if incoming.text.startswith("Quero"):
            return AgentResult(
                reply_text="1. Tissot A\n2. Tissot B",
                intent="commerce",
                commercial_data={
                    "products": [
                        {"id": "A", "name": "Tissot A", "url": "https://loja.example/a"},
                        {"id": "B", "name": "Tissot B", "url": "https://loja.example/b"},
                    ]
                },
                response_metadata={"domain": "commerce", "presented_products": True},
            )
        if incoming.text == "Gostei do segundo":
            return AgentResult(
                reply_text="Você escolheu o Tissot B.",
                intent="commerce",
                response_metadata={
                    "domain": "commerce",
                    "active_product": {
                        "product_id": "B",
                        "name": "Tissot B",
                        "url": "https://loja.example/b",
                    },
                },
            )
        return AgentResult(reply_text="https://loja.example/b", intent="commerce")

    monkeypatch.setattr(pipeline, "load_commerce_conversation_state", load_state)
    monkeypatch.setattr(pipeline, "generate_agent_reply_async", generate)

    for inbound_id, text in enumerate(
        ("Quero um relógio Tissot até 5 mil", "Gostei do segundo", "Me manda o link"),
        start=1,
    ):
        incoming = IncomingMessage(
            channel="instagram",
            sender_key="instagram:user-a",
            conversation_id="conv-user-a",
            visitor_id="visitor-a",
            text=text,
            raw={"inbound_id": inbound_id},
        )
        result = await pipeline.process_incoming_message(incoming, {"found": False})
        stored["conv-user-a"] = result.response_metadata["commerce_state"]

    assert len(seen_states[1]["last_presented_products"]) == 2
    assert seen_states[2]["active_product"]["product_id"] == "B"
    assert stored["conv-user-a"]["active_product"]["product_id"] == "B"
    assert "conv-user-b" not in stored
