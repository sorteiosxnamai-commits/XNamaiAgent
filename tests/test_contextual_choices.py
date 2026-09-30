from unittest.mock import AsyncMock

import pytest

from app.commerce_context import CommerceConversationState
from app.conversation_choices import handle_short_choice, resolve_choice, short_option
from app.models import IncomingMessage


def context(question="Você quer comprar com CPF ou CNPJ?"):
    return CommerceConversationState(active_topic="commercial_guidance",
        pending_followup={"question": question},
        active_product={"product_id": "legacy-watch", "name": "Relógio antigo"},
        order_id="legacy-order")


@pytest.mark.parametrize("text", ["cpf", "CPF", "com CPF", "prefiro cpf", "pessoa física", "pf"])
@pytest.mark.asyncio
async def test_cpf_answer_starts_collection_even_with_old_product_without_search_or_creation(monkeypatch, text):
    monkeypatch.setattr("app.capability_catalog.runtime_commerce_capabilities", lambda: frozenset())
    execute = AsyncMock(side_effect=AssertionError("No external lookup or mutation for choosing CPF"))
    result = await handle_short_choice(IncomingMessage(text=text), state=context(), execute=execute)
    assert result.response_metadata["pending_action"] == "awaiting_customer_registration_data"
    assert result.response_metadata["response_source"] == "contextual_registration_choice"
    assert result.response_metadata["pending_followup"] is None
    assert "catálogo agora" not in result.reply_text
    assert "CPF ou CNPJ" not in result.reply_text
    assert result.response_metadata["customer_registration_state"]["draft"]["person_type"] == "F"
    execute.assert_not_awaited()


@pytest.mark.parametrize("text,question,kind", [
    ("cnpj", "Você quer comprar com CPF ou CNPJ?", "registration"),
    ("pix", "Qual forma de pagamento você prefere?", "payment"),
    ("cartão", "Prefere Pix ou cartão?", "payment"),
    ("retirada", "Prefere entrega ou retirada?", "delivery"),
])
@pytest.mark.asyncio
async def test_other_business_choices_remain_information_not_provider_authorization(text, question, kind):
    execute = AsyncMock(side_effect=AssertionError("Not a transaction authorization"))
    state = context(question)
    assert resolve_choice(text, state).kind == kind
    result = await handle_short_choice(IncomingMessage(text=text), state=state, execute=execute)
    assert result.response_metadata["informational_only"]
    assert not result.response_metadata.get("pending_action")
    assert not result.handoff_required
    execute.assert_not_awaited()


@pytest.mark.parametrize("text", ["cpf", "cnpj", "pix", "retirada"])
@pytest.mark.asyncio
async def test_missing_question_clarifies_instead_of_searching(text):
    result = await handle_short_choice(IncomingMessage(text=text), state=CommerceConversationState(), execute=AsyncMock())
    assert result.reply_text.endswith("?")
    assert result.response_metadata["response_source"] == "contextual_choice"
    assert "customer_registration" not in result.response_metadata


@pytest.mark.parametrize("text", ["capa cpf123", "suporte de cartão", "fone bluetooth", "52998224725", "não quero cpf", "CPF ou CNPJ?"])
def test_products_documents_negations_and_questions_are_not_choices(text):
    assert short_option(text) is None


def test_latest_question_wins_and_change_of_subject_cancels_old_choice():
    state = context()
    assert resolve_choice("cpf", state, [{"role": "assistant", "content": "Você quer comprar com CPF ou CNPJ? 😊"}])
    assert resolve_choice("cpf", state, [{"role": "assistant", "content": "Qual marca de fone você procura?"}]) is None
    assert resolve_choice("cpf", state, [{"role": "assistant", "content": "Aqui está a lista de produtos."}]) is None


def test_legacy_assistant_transcript_cannot_restore_foreign_order_or_payment_link():
    from app.order_context_recovery import extract_handles_from_conversation
    history = [{"role": "assistant", "content": "Pedido 123456789: https://old-store.example/pagar?pedido=123456789",
                "metadata": {"memory_scope_trusted": False}}]
    handles = extract_handles_from_conversation(state=CommerceConversationState(), recent_turns=history)
    assert handles["order_ids"] == [] and handles["payment_urls"] == []
    handles = extract_handles_from_conversation(state=CommerceConversationState(), recent_turns=history,
                                               message_text="Consulte o pedido 95805")
    assert handles["order_ids"] == ["95805"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", ["awaiting_customer_registration_data", "awaiting_customer_registration_confirmation", "confirm_order"])
async def test_existing_operation_keeps_its_authorization_handler(pending):
    state = context()
    state.pending_action = pending
    assert await handle_short_choice(IncomingMessage(text="cpf"), state=state, execute=AsyncMock()) is None


@pytest.mark.parametrize("text", ["cpf", "cnpj", "pix", "retirada"])
def test_catalog_resolver_cannot_reinterpret_business_choices_as_products(text):
    from app.commerce.turn_resolver import resolve_commerce_turn, ACTION_TRANSACTION
    result = resolve_commerce_turn(text, state=context())
    assert result.action == ACTION_TRANSACTION
    assert not result.requires_catalog_search


@pytest.mark.asyncio
async def test_ricardo_two_turn_pipeline_keeps_question_and_enters_registration(monkeypatch):
    from tests.test_chatbo_pipeline_replay import Replay
    from tests.test_conversation_routing import advice
    from app import openai_agent, consultative_agent
    from app.models import AgentResult
    from app.config import get_settings
    replay = Replay(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("AGENT_CONVERSATION_FIRST_ENABLED", "true")
    monkeypatch.setenv("AGENT_CONSULTATIVE_ENABLED", "true")
    monkeypatch.setenv("AGENT_CONSULTATIVE_EMERGENCY_OFF", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(openai_agent, "interpret_message", AsyncMock(return_value=advice()))
    monkeypatch.setattr(consultative_agent, "consult", AsyncMock(return_value=AgentResult(
        reply_text="Você quer comprar com CPF ou CNPJ? 😊", intent="commerce",
        response_metadata={"informational_only": True, "response_source": "consultative_openai"})))
    forbidden = AsyncMock(side_effect=AssertionError("CPF must not reach the catalog or create_customer"))
    monkeypatch.setattr(openai_agent, "execute_tool", forbidden)
    try:
        first, _ = await replay.say("gostaria de comprar com vocês")
        assert first.response_metadata["commerce_state"]["pending_followup"]
        second, row = await replay.say("cpf")
        assert second.response_metadata["commerce_state"]["pending_action"] == "awaiting_customer_registration_data"
        assert row["response_source"] == "contextual_registration_choice"
        assert row["tools"] == []
        assert second.safety_reason != "product_not_found"
        forbidden.assert_not_awaited()
    finally:
        get_settings.cache_clear()
