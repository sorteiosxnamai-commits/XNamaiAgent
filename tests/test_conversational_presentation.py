import pytest

from app.models import AgentResult, IncomingMessage
from app.response_composer import compose_outbound_reply
from app.published_knowledge import bind_publication, reset_publication, supports_policy_line


def test_complete_advice_preserves_last_question_and_paragraphs_beyond_900_chars():
    text = "\n\n".join(["Uma orientação geral para organizar a compra. " * 7] * 4) + "\n\nQual é o perfil da sua loja?"
    result = AgentResult(reply_text=text, intent="commerce", response_metadata={
        "informational_only": True, "preserve_conversational_answer": True})
    result = compose_outbound_reply(IncomingMessage(channel="whatsapp", text="Como começo?"), result)
    assert result.reply_text.endswith("Qual é o perfil da sua loja?")
    assert len(result.reply_text) > 900
    assert result.reply_text.count("\n\n") == 4


@pytest.mark.parametrize("line,expected", [
    ("**O pedido mínimo é de R$ 800,00.**", True),
    ("- O pedido mínimo é de R$ 800,00.", True),
    ("O pedido mínimo é de **R$ 800,00**.", True),
    ("O pedido mínimo é de R$ 800,00. O fone também.", False),
    ("O pedido mínimo é de R$ 900,00.", False),
    ("O fone custa R$ 800,00.", False),
])
def test_policy_presentation_never_authorizes_extra_claims(line, expected):
    token = bind_publication({"knowledge_documents": [{"id": "minimum", "topic": "minimum_order",
        "status": "approved", "content": "O pedido mínimo é de R$ 800,00."}]})
    try:
        assert supports_policy_line(line) is expected
    finally:
        reset_publication(token)


def test_original_validation_failure_survives_fallback_validation():
    from app.agent_contracts import build_agent_decision
    from app.factual_validator import apply_factual_validation
    result = AgentResult(reply_text="O produto custa R$ 987,00.", intent="commerce",
                         response_metadata={"domain": "commerce", "response_source": "consultative_openai"})
    decision = build_agent_decision(IncomingMessage(text="Qual o preço?"), result, openai_call_count=0)
    apply_factual_validation(result, decision=decision, mode="enforce")
    apply_factual_validation(result, decision=decision, mode="enforce")
    assert result.response_metadata["factual_validation_initial"]["violations"]
    assert result.response_metadata["factual_validation_initial"]["fallback_applied"]


@pytest.mark.parametrize("reply,valid", [
    ("3. Finalize o pedido\n4. Escolha a entrega.", True),
    ("Diversificar sua oferta ajuda a atender perfis diferentes.", True),
    ("Uma promoção é uma ação comercial para destacar produtos.", True),
    ("Temos promoção especial neste modelo.", False),
    ("Este fone está em promoção.", False),
    ("Seu pedido 95805 foi localizado.", False),
])
def test_general_advice_is_not_mistaken_for_current_commercial_claim(reply, valid):
    from app.agent_contracts import build_agent_decision
    from app.factual_validator import validate_factual_response
    result = AgentResult(reply_text=reply, intent="commerce", response_metadata={
        "domain": "commerce", "response_source": "consultative_openai", "informational_only": True})
    decision = build_agent_decision(IncomingMessage(text="Pode explicar?"), result, openai_call_count=0)
    assert validate_factual_response(result, decision=decision, mode="enforce").valid is valid
