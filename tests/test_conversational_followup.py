"""Generic continuation for a question the agent itself asked outside the
deterministic commerce/registration flows.

Reproduces the two real-production incidents:

    "Quais são os benefícios do cadastro?" -> answer ending in "Quer saber
    mais?" -> "Sim" wrongly reset to a bare greeting instead of continuing.

    "Quer que eu te ajude com o cadastro por aqui?" -> "Prossiga" wrongly
    fell back to "Posso ajudar você com mais alguma informação ou produto?".
"""

from __future__ import annotations

import json

import pytest

from app.context_resume import resolve_followup_response
from app.models import AgentResult
from app.openai_agent import _annotate_agent_result, _trailing_question


def test_trailing_question_extracts_the_last_sentence():
    text = (
        "João Pedro, ao se cadastrar você facilita suas compras. "
        "Se quiser, posso explicar sobre o XNaMai Club. Quer saber mais?"
    )
    assert _trailing_question(text) == "Quer saber mais?"


def test_trailing_question_is_none_when_reply_does_not_ask_anything():
    assert _trailing_question("Tudo certo, cadastro concluído.") is None


@pytest.mark.parametrize("text", [
    "sim", "Sim!", "claro", "pode", "pode ser", "prossiga", "continue",
    "continua", "quero", "beleza", "ok", "com certeza",
])
def test_resolve_followup_response_affirms(text):
    assert resolve_followup_response(text, {"question": "Quer saber mais?"}) == "AFFIRM"


@pytest.mark.parametrize("text", ["não", "Não!", "agora não", "não quero", "depois"])
def test_resolve_followup_response_rejects(text):
    assert resolve_followup_response(text, {"question": "Quer saber mais?"}) == "REJECT"


@pytest.mark.parametrize("text", [
    "Quero ver carregadores",
    "Não, quero saber sobre cabos USB-C",
    "qual o preço do relógio?",
])
def test_resolve_followup_response_detects_new_topic(text):
    assert resolve_followup_response(text, {"question": "Quer saber mais?"}) == "NEW_TOPIC"


def test_resolve_followup_response_without_pending_is_unresolved():
    assert resolve_followup_response("sim", None) == "UNRESOLVED"


def test_general_reply_ending_in_a_question_sets_pending_followup():
    result = AgentResult(
        reply_text="Isso mesmo! Quer saber mais sobre o XNaMai Club?",
        intent="general",
        response_metadata={"domain": "general"},
    )
    annotated = _annotate_agent_result(result, domain="general")
    assert annotated.response_metadata["pending_followup"] == {
        "question": "Quer saber mais sobre o XNaMai Club?"
    }


def test_commerce_reply_never_sets_generic_pending_followup():
    """Commerce already has its own continuation machinery (active_product,
    last_presented_products, pending_commerce_action) even for a
    clarification that ends in '?' without setting pending_action — this
    layer must stay out of its way."""
    result = AgentResult(
        reply_text="Qual deles você quer: o primeiro ou o segundo modelo?",
        intent="commerce",
        response_metadata={"domain": "commerce"},
    )
    annotated = _annotate_agent_result(result, domain="commerce")
    assert annotated.response_metadata["pending_followup"] is None


def test_active_registration_pending_action_suppresses_pending_followup():
    result = AgentResult(
        reply_text="Sua empresa usa nome fantasia? Se sim, me envie.",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "pending_action": "awaiting_customer_registration_data",
        },
    )
    annotated = _annotate_agent_result(result, domain="commerce")
    assert annotated.response_metadata["pending_followup"] is None


def test_handoff_reply_never_sets_pending_followup():
    result = AgentResult(
        reply_text="Vou encaminhar para a equipe. Podem te chamar em instantes?",
        intent="general",
        handoff_required=True,
        response_metadata={"domain": "general"},
    )
    annotated = _annotate_agent_result(result, domain="general")
    assert annotated.response_metadata["pending_followup"] is None


async def _generate_reply_with_seeded_commerce_state(monkeypatch, text, seeded_state):
    """Call generate_agent_reply_async exactly as message_pipeline does, with
    a commerce_state round-tripped through JSON first — the same shape a
    fresh serverless invocation would reload, never a live Python object
    kept alive between turns."""
    from app import openai_agent
    from app.models import IncomingMessage

    captured: dict[str, str] = {}
    real_extract = openai_agent.extract_order_reference

    def spy_extract(candidate_text):
        captured["text"] = candidate_text
        return real_extract(candidate_text)

    monkeypatch.setattr(openai_agent, "extract_order_reference", spy_extract)
    customer_context = {
        "_commerce_state": json.loads(json.dumps(seeded_state)),
        "found": False,
    }
    message = IncomingMessage(
        channel="whatsapp", provider="ycloud", text=text,
        sender_phone="5511988880001", conversation_id="wa:5511988880001",
    )
    result = await openai_agent.generate_agent_reply_async(message, customer_context)
    return captured.get("text"), result


@pytest.mark.asyncio
async def test_followup_persists_across_requests_and_grounds_a_bare_sim(monkeypatch):
    """Realistic persistence: the pending question comes from a JSON-
    serialized commerce_state exactly as a fresh serverless invocation would
    reload it, not a live Python object kept alive between turns."""
    seeded_state = {"pending_followup": {"question": "Quer saber mais sobre o XNaMai Club?"}}
    seen_text, result = await _generate_reply_with_seeded_commerce_state(
        monkeypatch, "Sim", seeded_state
    )
    assert seen_text is not None
    assert "Quer saber mais sobre o XNaMai Club?" in seen_text
    assert "respondendo" in seen_text
    assert result.response_metadata.get("pending_followup") != seeded_state["pending_followup"]


@pytest.mark.asyncio
async def test_followup_new_topic_is_dropped_without_rewriting_the_message(monkeypatch):
    seeded_state = {"pending_followup": {"question": "Quer saber mais sobre o XNaMai Club?"}}
    seen_text, _ = await _generate_reply_with_seeded_commerce_state(
        monkeypatch, "Quero ver carregadores", seeded_state
    )
    assert seen_text == "Quero ver carregadores"


@pytest.mark.asyncio
async def test_registration_confirmation_is_never_intercepted_by_followup_layer(monkeypatch):
    """Regression: a generic follow-up pending from earlier in the
    conversation must never steal 'confirmo o cadastro' from the
    registration flow."""
    from tests.test_chatbo_pipeline_replay import Replay

    replay = Replay(monkeypatch)
    for message in ("quero me cadastrar", "52998224725", "João Teste da Silva", "joao@example.com"):
        await replay.say(message)
    key = "wa:5511988880001"
    assert replay.states[key]["pending_action"] == "awaiting_customer_registration_confirmation"
    # A stray general follow-up must not outrank the active registration gate.
    seeded = json.loads(json.dumps(replay.states[key]))
    seeded["pending_followup"] = {"question": "Quer saber mais sobre o XNaMai Club?"}
    replay.states[key] = seeded

    _, row = await replay.say("confirmo o cadastro")
    assert row["status_after"] == "created"
    assert row["tools"] == ["lookup_customer_by_document", "create_customer"]
