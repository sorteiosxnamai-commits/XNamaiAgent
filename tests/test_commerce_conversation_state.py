from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.commerce_context import (
    CommerceConversationState,
    PresentedCommerceProduct,
    apply_commerce_domain_context,
    evolve_commerce_state,
    resolve_commerce_reference,
)
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _interpretation(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "inspect",
        "subject": {"product_type": "relógio"},
        "preferences": {},
        "information_needed": ["catalog"],
        "references_previous_context": True,
        "needs_clarification": False,
        "confidence": 0.98,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


def _state() -> CommerceConversationState:
    return CommerceConversationState(
        active_domain="commerce",
        active_topic="watch_case_size",
        active_product={
            "product_id": "202",
            "reference": "REF-202",
            "name": "Produto dois",
        },
        last_presented_products=[
            {"position": 1, "product_id": "101", "reference": "REF-101", "name": "Produto um"},
            {"position": 2, "product_id": "202", "reference": "REF-202", "name": "Produto dois"},
            {"position": 3, "product_id": "303", "reference": "REF-303", "name": "Tissot SuperSport Rugby"},
        ],
        purchase_stage="selection",
    )


def test_list_position_resolves_only_to_real_presented_product():
    interpretation = _interpretation(
        reference_type="list_position",
        reference_position=3,
    )

    resolved, resolved_by = resolve_commerce_reference(interpretation, _state())

    assert resolved is not None
    assert resolved.product_id == "303"
    assert resolved.reference == "REF-303"
    assert resolved_by == "product_id"


def test_current_product_reference_resolves_active_product():
    interpretation = _interpretation(reference_type="current_product")

    resolved, _ = resolve_commerce_reference(interpretation, _state())

    assert resolved is not None
    assert resolved.product_id == "202"


def test_explicit_product_name_resolves_against_latest_presented_list():
    interpretation = _interpretation(
        subject={"brand": "Tissot", "model": "SuperSport Rugby"},
        reference_type="explicit_product",
    )

    resolved, _ = resolve_commerce_reference(interpretation, _state())

    assert resolved is not None
    assert resolved.product_id == "303"


def test_new_presented_list_replaces_previous_positions():
    previous = _state()
    result = AgentResult(
        reply_text="Novas opções",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "901", "name": "Nova opção A"},
                {"id": "902", "name": "Nova opção B"},
            ]
        },
        response_metadata={"domain": "commerce", "presented_products": True},
    )

    updated = evolve_commerce_state(previous, result)
    reference, _ = resolve_commerce_reference(
        _interpretation(reference_type="list_position", reference_position=1),
        updated,
    )

    assert [item.product_id for item in updated.last_presented_products] == ["901", "902"]
    assert reference is not None
    assert reference.product_id == "901"


def test_openai_domain_is_not_reclassified_by_state():
    interpreted = _interpretation(domain="raffle", domain_change_explicit=False)

    contextual, changed = apply_commerce_domain_context(interpreted, _state())

    assert contextual.domain == "raffle"
    assert changed is False


def test_openai_payment_domain_remains_authoritative():
    state = _state().model_copy(update={"purchase_stage": "payment_discussion"})
    interpreted = _interpretation(
        domain="raffle",
        goal="buy",
        purchase_stage="payment_discussion",
        domain_change_explicit=False,
    )

    contextual, _ = apply_commerce_domain_context(interpreted, state)

    assert contextual.domain == "raffle"
    assert contextual.goal == "buy"


def test_explicit_raffle_change_is_preserved():
    interpreted = _interpretation(
        domain="raffle",
        goal=None,
        subject={},
        references_previous_context=False,
        domain_change_explicit=True,
    )

    contextual, changed = apply_commerce_domain_context(interpreted, _state())

    assert contextual.domain == "raffle"
    assert changed is False


def test_local_raffle_fallback_remains_available_after_interpreter_failure():
    interpreted = _interpretation(
        domain="raffle",
        goal=None,
        subject={},
        references_previous_context=False,
        domain_change_explicit=False,
    )
    interpreted._source = "deterministic_fallback"

    contextual, changed = apply_commerce_domain_context(interpreted, _state())

    assert contextual.domain == "raffle"
    assert changed is False


@pytest.mark.asyncio
async def test_contextual_variant_followup_uses_active_product_id(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def fake_execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {"id": "202", "name": "Produto dois", "has_variation": True}
        if tool == "list_product_variants":
            return {
                "variants": [
                    {"id": "v-black", "product_id": "202", "color": "Preto", "stock": 1}
                ]
            }
        raise AssertionError(f"unexpected tool: {tool}")

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    interpretation = _interpretation(
        preferences={"color": "preto"},
        reference_type="current_product",
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="tem ele preto?"),
        {"primary_intent": "commerce"},
        {},
        interpretation,
        commerce_state=_state(),
    )

    assert calls[0] == ("get_product", {"product_id": "202"})
    assert ("list_product_variants", {"product_id": "202"}) in calls
    assert result is not None
    assert result.response_metadata["active_product"]["product_id"] == "202"


@pytest.mark.asyncio
async def test_consultative_request_can_clarify_before_catalog(monkeypatch):
    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "execute_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("consultative clarification must not retrieve yet")
        ),
    )
    interpretation = _interpretation(
        goal="discover",
        preferences={},
        needs_clarification=True,
        clarification_question="Você prefere algo mais discreto ou marcante?",
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="qual relógio combina comigo?"),
        {"primary_intent": "commerce"},
        {},
        interpretation,
        commerce_state=CommerceConversationState(active_domain="commerce"),
    )

    assert result is not None
    assert result.safety_reason == "commerce_clarification"
    assert "discreto" in result.reply_text


def test_load_state_uses_existing_jsonb_and_only_delivered_responses(monkeypatch):
    import app.db as db

    captured = {}
    expected = _state().model_dump(mode="json")

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, query, params):
            captured.setdefault("queries", []).append(query)
            captured.setdefault("params_list", []).append(params)
            captured["query"] = query
            captured["params"] = params

        def fetchall(self):
            if "conversation_id" in captured.get("params", {}):
                return [
                    {
                        "provider_response": {
                            "_agent_context": {
                                "commerce_state": expected,
                            }
                        }
                    }
                ]
            return []

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    @contextmanager
    def fake_get_conn():
        yield FakeConnection()

    monkeypatch.setattr(
        db,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://configured"),
    )
    monkeypatch.setattr(db, "get_conn", fake_get_conn)
    monkeypatch.setattr(
        "app.customer_identity.resolve_linked_identity_candidates",
        lambda **_kwargs: [],
    )

    loaded = db.load_commerce_conversation_state(
        conversation_id="conversation-1",
        sender_phone="5511999999999",
        before_inbound_id=50,
        sender_key="whatsapp:5511999999999",
    )

    assert loaded == expected
    assert any("provider_send_ok = true" in query for query in captured["queries"])
    assert any(
        "inbound.id < %(before_inbound_id)s" in query for query in captured["queries"]
    )
    assert any(
        params.get("conversation_id") == "conversation-1"
        for params in captured["params_list"]
    )
    assert any(
        params.get("sender_phone") == "5511999999999"
        for params in captured["params_list"]
    )
