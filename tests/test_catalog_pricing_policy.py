"""Membership pricing is institutional knowledge, not customer/order evidence."""
import pytest

from app.agent_contracts import build_agent_decision
from app.club_xnamai import handle_club_turn
from app.commerce_context import CommerceConversationState
from app.factual_validator import validate_factual_response
from app.models import AgentResult, IncomingMessage
from app.published_knowledge import bind_publication, published_policy, reset_publication, supports_policy_line

POLICY = "Os preços do catálogo são exclusivos para membros do Club Xnamai. Para não membros, há acréscimo de 15%."


def document(**changes):
    return {"id": "catalog-pricing", "topic": "catalog_pricing", "status": "approved",
            "content": POLICY, **changes}


def test_published_catalog_pricing_reaches_prompt_even_on_short_catalog_request():
    from app.published_knowledge import policy_reference_block
    import json
    from pathlib import Path
    docs = json.loads(Path("docs/knowledge/xnamai.review.json").read_text(encoding="utf-8"))["knowledge_documents"]
    pricing = next(d for d in docs if d["topic"] == "catalog_pricing")
    block = policy_reference_block({"knowledge_documents": docs})
    assert pricing["content"] in block
    assert "15%" in block and "149,97" not in block


@pytest.mark.asyncio
async def test_store_guidance_keeps_pricing_and_next_step_in_final_whatsapp_message():
    from app.sales_agent import handle_sales_message
    from app.response_composer import compose_outbound_reply
    docs = [document(), {"id": "positioning", "topic": "commercial_positioning", "status": "approved",
                         "content": "A XNamai busca oferecer preços de caixa fechada mesmo comprando poucas unidades, sem precisar levar grandes quantidades de cada produto."},
            {"id": "minimum", "topic": "minimum_order", "status": "approved", "content": "O pedido mínimo é de R$ 800,00."}]
    token = bind_publication({"knowledge_documents": docs})
    try:
        incoming = IncomingMessage(channel="whatsapp", text="Boa tarde, gostaria de saber sobre seus produtos, fones, mouses, atacado, varejo e preços")
        result = await handle_sales_message(incoming, {"primary_intent": "commerce"}, {}, None, recent_turns=[])
        expected = result.reply_text
        final = compose_outbound_reply(incoming, result)
        assert POLICY in final.reply_text
        assert final.reply_text == expected
        assert final.reply_text.endswith("Assim te oriento pelo caminho certo.")
    finally:
        reset_publication(token)


@pytest.mark.parametrize("documents", [
    [],
    [document(status="draft")],
    [document(status="deleted")],
    [document(valid_until="2020-01-01T00:00:00Z")],
    [document(), document(id="conflicting", content="Não membros pagam acréscimo de 25%.")],
])
def test_missing_unpublished_expired_or_ambiguous_pricing_never_invents_percentage(documents):
    token = bind_publication({"knowledge_documents": documents})
    try:
        assert published_policy("catalog_pricing") is None
        result = handle_club_turn("ainda não sou membro do Club Xnamai", state=CommerceConversationState())
        assert "%" not in result.reply_text
        from app.store_guidance import build_store_guidance
        guidance = build_store_guidance("Quero saber sobre seus produtos, atacado e varejo e preços")
        assert guidance is not None and "%" not in guidance.reply_text
    finally:
        reset_publication(token)


def test_approved_pricing_is_quoted_exactly_and_supported():
    token = bind_publication({"knowledge_documents": [document()]})
    try:
        assert published_policy("catalog_pricing") == POLICY
        from app.store_guidance import build_store_guidance
        guidance = build_store_guidance("Quero saber sobre seus produtos, atacado e varejo e preços")
        assert guidance is not None and POLICY in guidance.reply_text
        result = AgentResult(reply_text=POLICY, intent="commerce", response_metadata={"domain": "commerce"})
        decision = build_agent_decision(IncomingMessage(text="qual é a regra de preço?"), result, openai_call_count=0)
        assert validate_factual_response(result, decision=decision, mode="shadow").valid
    finally:
        reset_publication(token)


@pytest.mark.parametrize("claim", [
    "O fone custa R$ 115,00.",
    "Seu pedido agora custa R$ 920,00.",
    "O fone está em promoção com desconto de 15% hoje.",
    POLICY + " O fone custa R$ 115,00.",
    "Para quem não é membro, há um acréscimo de 25%.",
    "Seu pedido tem acréscimo de 15% porque você não é membro.",
    "Você vai pagar 15% a mais neste pedido.",
])
def test_institutional_percentage_never_authorizes_product_price_order_total_or_promotion(claim):
    token = bind_publication({"knowledge_documents": [document()]})
    try:
        assert not supports_policy_line(claim)
        result = AgentResult(reply_text=claim, intent="commerce", response_metadata={"domain": "commerce"})
        decision = build_agent_decision(IncomingMessage(text="quero um fone"), result, openai_call_count=0)
        assert not validate_factual_response(result, decision=decision, mode="shadow").valid
    finally:
        reset_publication(token)


def test_pricing_quote_needs_current_publication_even_when_percentage_is_correct():
    result = AgentResult(reply_text=POLICY, intent="commerce", response_metadata={"domain": "commerce"})
    decision = build_agent_decision(IncomingMessage(text="E para não membros?"), result, openai_call_count=0)
    token = bind_publication({})
    try:
        assert not validate_factual_response(result, decision=decision, mode="enforce").valid
    finally:
        reset_publication(token)


@pytest.mark.parametrize("outcome_name", ["browse", "ambiguous"])
def test_club_offer_after_long_catalog_keeps_surcharge_percentage(outcome_name):
    from app.commerce.turn_flow import CommerceTurnOutcome
    from app.sales_agent import _render_commerce_turn
    from app.response_composer import compose_outbound_reply
    token = bind_publication({"knowledge_documents": [document()]})
    try:
        state = CommerceConversationState()
        outcome = CommerceTurnOutcome(outcome=outcome_name, products=[{"id": str(i), "name": "Fone " + "modelo " * 29, "price": 10} for i in range(20)])
        result = _render_commerce_turn(outcome, state)
        assert POLICY in result.reply_text and len(result.reply_text) > 900
        final = compose_outbound_reply(IncomingMessage(channel="whatsapp"), result)
        assert POLICY in final.reply_text
    finally:
        reset_publication(token)
