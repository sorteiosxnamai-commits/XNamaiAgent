from unittest.mock import AsyncMock

import pytest

from app.commerce_context import CommerceConversationState
from app.commerce.catalog_filters import matches
from app.wholesale_catalog import requested_queries, handle_wholesale_catalog
from app.conversation_choices import resolve_choice, handle_short_choice
from app.models import IncomingMessage


def state_for(queries):
    return CommerceConversationState(active_topic="product_catalog", catalog_listing={"queries": queries})


def test_demonstrative_refinement_preserves_power_then_explicit_power_replaces_it():
    first = requested_queries("caixas de som 120w", CommerceConversationState())
    second = requested_queries("Dessas, só as com Bluetooth", state_for(first))
    assert second[0]["filters"]["power"] == "120"
    assert matches(second[0]["query"], {"name": "Caixa de som Bluetooth 120W"})
    assert not matches(second[0]["query"], {"name": "Caixa de som Bluetooth 1200W"})
    third = requested_queries("só 200 watts", state_for(second))
    assert third[0]["filters"]["power"] == "200"
    assert "bluetooth" in third[0]["query"] and "120" not in third[0]["query"]
    assert first[0]["filters"]["power"] == "120"


def test_legacy_queries_upgrade_and_category_switch_discards_old_filters():
    old = [{"query": "caixa de som 120w", "offset": 20, "done": False}]
    refined = requested_queries("Dessas, só as com Bluetooth", state_for(old))
    assert refined[0]["offset"] == 0 and refined[0]["filters"]["power"] == "120"
    switched = requested_queries("agora quero fones de ouvido", state_for(refined))
    assert switched[0]["filters"]["category"] == "fone de ouvido"
    assert not switched[0]["filters"]["power"]
    assert "bluetooth" not in switched[0]["query"]
    refined[0]["offset"] = 20
    assert requested_queries("continue", state_for(refined)) == refined


def test_feature_that_names_another_category_does_not_replace_primary_category():
    refined = requested_queries("dessas só as com microfone", state_for([{"query": "caixa de som 120w"}]))
    assert refined[0]["filters"]["category"] == "caixa de som"
    assert refined[0]["filters"]["power"] == "120"
    assert "microfone" in refined[0]["query"]


@pytest.mark.parametrize("text", ["Dessas quero 2 unidades", "comprar essas", "adicione no carrinho"])
def test_purchase_is_not_a_catalog_refinement(text):
    assert requested_queries(text, state_for([{"query": "caixa de som 120w"}])) is None


def test_old_listing_does_not_apply_after_order_topic():
    state = state_for([{"query": "caixa de som 120w"}])
    state.active_topic = "order_status"
    assert requested_queries("Dessas, só as com Bluetooth", state) is None


@pytest.mark.parametrize("text", ["Agora quero de 40w, mantendo Bluetooth", "Quero 40 watts com Bluetooth"])
def test_semantic_refinement_handles_paraphrases_and_replaces_explicit_power(text):
    from app.models import SalesInterpretation
    from app.turn_understanding import TurnUnderstanding, attach_turn_understanding
    interpretation = SalesInterpretation(domain="commerce", goal="find", confidence=.99,
        references_previous_context=True, needs_clarification=False)
    interpretation._source = "openai"
    attach_turn_understanding(interpretation, TurnUnderstanding(
        primary_intent="commerce_find", confidence=.99, catalog_mode="refine", catalog_queries=["40w bluetooth"]))
    refined = requested_queries(text, state_for([{"query": "caixa de som 120w bluetooth"}]), interpretation)
    assert matches(refined[0]["query"], {"name": "Caixa de som Bluetooth 40W"})
    assert not matches(refined[0]["query"], {"name": "Caixa de som Bluetooth 120W"})
    assert not matches(refined[0]["query"], {"name": "Caixa de som 40W"})


@pytest.mark.asyncio
async def test_refined_provider_call_keeps_all_constraints():
    execute = AsyncMock(return_value={"ok": True, "products": [{"id": "1", "name": "Caixa de som Bluetooth 120W"}]})
    result = await handle_wholesale_catalog("Dessas, só as com Bluetooth",
        state=state_for([{"query": "caixa de som 120w"}]), execute=execute)
    tool, args = execute.await_args.args
    assert tool == "search_products" and args["strict"] is True
    assert "120w" in args["query"] and "bluetooth" in args["query"]
    assert len(result.commercial_data["products"]) == 1


@pytest.mark.asyncio
async def test_cpf_after_unpunctuated_registration_offer_collects_without_mutation(monkeypatch):
    monkeypatch.setattr("app.capability_catalog.runtime_commerce_capabilities", lambda: frozenset())
    history = [{"role": "assistant", "content": "Com CNPJ, entre no catálogo. Com CPF, o cadastro é pelo atendimento. Se quiser, já te explico como fazer o cadastro."}]
    state = CommerceConversationState()
    assert resolve_choice("cpf", state, history).value == "cpf"
    execute = AsyncMock(side_effect=AssertionError("No authorization to create or pay"))
    result = await handle_short_choice(IncomingMessage(text="cpf"), state=state, execute=execute, recent_turns=history)
    assert result.response_metadata["pending_action"] == "awaiting_customer_registration_data"
    execute.assert_not_awaited()


@pytest.mark.parametrize("latest", ["Aqui estão os fones disponíveis.", "Com CPF você cadastra. Agora veja os fones.", "O pagamento será confirmado no fechamento."])
def test_registration_offer_does_not_survive_topic_change(latest):
    state = CommerceConversationState(pending_followup={"question": "CPF ou CNPJ?"})
    assert resolve_choice("cpf", state, [{"role": "assistant", "content": latest}]) is None


@pytest.mark.asyncio
async def test_ambiguous_punctuation_defers_to_contextual_interpreter():
    history = [{"role": "assistant", "content": "Boa tarde! Tudo bem? Com CPF, o cadastro é pelo atendimento. Seja bem-vindo!"}]
    execute = AsyncMock(side_effect=AssertionError("Choosing a modality authorizes no operation"))
    result = await handle_short_choice(IncomingMessage(text="cpf"), state=CommerceConversationState(),
        execute=execute, recent_turns=history, defer_ambiguous=True)
    assert result is None
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,source,confidence,collects", [
    ("cpf", "openai", .99, True), ("cnpj", "openai", .99, False),
    ("cpf", "fallback", .99, False), ("cpf", "openai", .5, False),
])
async def test_semantic_cpf_choice_activates_local_collection_without_creation(monkeypatch, choice, source, confidence, collects):
    from app.models import SalesInterpretation
    from app.turn_understanding import TurnUnderstanding, attach_turn_understanding
    monkeypatch.setattr("app.capability_catalog.runtime_commerce_capabilities", lambda: frozenset())
    interpretation = SalesInterpretation(domain="store_general", confidence=confidence,
        references_previous_context=True, needs_clarification=False)
    interpretation._source = source
    attach_turn_understanding(interpretation, TurnUnderstanding(primary_intent="store_general",
        confidence=confidence, registration_choice=choice))
    execute = AsyncMock(side_effect=AssertionError("A chosen modality only starts local collection"))
    result = await handle_short_choice(IncomingMessage(text="cpf"), state=CommerceConversationState(),
        execute=execute, recent_turns=[{"role":"assistant", "content":"Com CPF, o cadastro é pelo atendimento. Seja bem-vindo!"}],
        interpretation=interpretation, defer_ambiguous=True)
    if collects:
        assert result.response_metadata["pending_action"] == "awaiting_customer_registration_data"
        assert result.response_metadata["customer_registration_state"]["draft"]["person_type"] == "F"
    else:
        assert result is None
    execute.assert_not_awaited()
