"""Repair is accepted only after the real factual validator checks its output."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent_contracts import build_agent_decision
from app.commerce_context import CommerceConversationState
from app.config import Settings
from app.conversation_repair import repair_informational_claims
from app.factual_validator import validate_factual_response
from app.models import AgentResult, IncomingMessage


def original(text="O produto custa R$ 999,00.", *, informational=True):
    return AgentResult(reply_text=text, intent="commerce",
        commercial_data={"products": [{"id": "verified-1", "name": "Fone", "current_price": "199.90"}]},
        response_metadata={"domain": "commerce", "informational_only": informational,
                           "used_openai_responder": True, "knowledge_sources": ["approved-policy"]})


async def repair(result):
    incoming = IncomingMessage(text="Como escolho produtos para minha loja?")
    decision = build_agent_decision(incoming, result, openai_call_count=1)
    output = await repair_informational_claims(incoming, result, decision=decision,
        settings=Settings(OPENAI_API_KEY="test"), state=CommerceConversationState().model_dump(mode="json"), trusted_domains=[])
    return output, decision


@pytest.mark.asyncio
async def test_accepts_valid_repair_preserving_evidence_and_original(monkeypatch):
    import app.openai_gateway as gateway
    generate = AsyncMock(return_value=SimpleNamespace(text="Você pode começar pelas categorias mais procuradas pelos seus clientes.", refusal=None))
    monkeypatch.setattr(gateway, "generate_text_output", generate)
    before = original()
    output, decision = await repair(before)
    assert output is not before
    assert validate_factual_response(output, decision=decision, mode="enforce").valid
    assert output.commercial_data == before.commercial_data
    assert output.response_metadata["knowledge_sources"] == ["approved-policy"]
    assert output.response_metadata["claim_repair"]["accepted"]
    assert before.reply_text == "O produto custa R$ 999,00."
    generate.assert_awaited_once()
    assert "tools" not in generate.await_args.kwargs


@pytest.mark.asyncio
async def test_new_unsupported_claim_cannot_pass_as_repair(monkeypatch):
    import app.openai_gateway as gateway
    generate = AsyncMock(return_value=SimpleNamespace(text="O produto custa R$ 500,00.", refusal=None))
    monkeypatch.setattr(gateway, "generate_text_output", generate)
    before = original()
    output, decision = await repair(before)
    assert output is before
    assert not output.response_metadata["claim_repair"]["accepted"]
    assert not validate_factual_response(output, decision=decision, mode="enforce").valid
    generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_pricing_repair_uses_published_rule_without_customer_membership_inference(monkeypatch):
    from app.published_knowledge import bind_publication, reset_publication
    policy = "Os preços do catálogo são exclusivos para membros do Club Xnamai. Para quem não é membro, há um acréscimo de 15%."
    token = bind_publication({"knowledge_documents": [{"id": "pricing", "topic": "catalog_pricing",
        "status": "approved", "content": policy}]})
    generate = AsyncMock(return_value=SimpleNamespace(text=policy, refusal=None))
    monkeypatch.setattr("app.openai_gateway.generate_text_output", generate)
    try:
        output, _ = await repair(original("Você vai pagar 15% a mais neste pedido."))
        assert output.reply_text == policy
        assert output.response_metadata["claim_repair"]["accepted"]
        assert policy in generate.await_args.kwargs["messages"][0]["content"]
    finally:
        reset_publication(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [original("Você pode começar pelas categorias que seus clientes procuram."),
                                        original(informational=False)])
async def test_valid_or_operational_reply_never_calls_repair_model(monkeypatch, result):
    import app.openai_gateway as gateway
    generate = AsyncMock(side_effect=AssertionError("Repair should not run"))
    monkeypatch.setattr(gateway, "generate_text_output", generate)
    output, _ = await repair(result)
    assert output is result
    generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["refusal", "empty", "exception"])
async def test_refusal_empty_and_failure_return_original(monkeypatch, failure):
    import app.openai_gateway as gateway
    generate = AsyncMock(side_effect=RuntimeError("unavailable")) if failure == "exception" else AsyncMock(
        return_value=SimpleNamespace(text="" if failure == "empty" else "Recusado", refusal=failure == "refusal"))
    monkeypatch.setattr(gateway, "generate_text_output", generate)
    before = original()
    output, _ = await repair(before)
    assert output is before
    assert not output.response_metadata["claim_repair"]["accepted"]
    generate.assert_awaited_once()
