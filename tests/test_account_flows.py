import pytest

from app.account_flows import _asks_if_cpf_is_accepted, handle_account_flows
from app.commerce_context import CommerceConversationState
from app.models import IncomingMessage


@pytest.mark.parametrize(
    "text",
    (
        "consigo fazer por cpf?",
        "Posso comprar usando CPF?",
        "vocês aceitam CPF no cadastro?",
    ),
)
def test_recognizes_cpf_registration_policy_questions(text):
    assert _asks_if_cpf_is_accepted(text)


@pytest.mark.asyncio
async def test_answers_cpf_policy_without_calling_commerce_or_model():
    calls = []

    async def execute(name, payload):
        calls.append((name, payload))
        return {}

    result = await handle_account_flows(
        IncomingMessage(text="consigo fazer por cpf?"),
        state=CommerceConversationState(),
        execute=execute,
    )

    assert result is not None
    assert "tanto por CPF quanto por CNPJ" in result.reply_text
    assert "nome completo, CPF, endereço, telefone e e-mail" in result.reply_text
    assert result.response_metadata["response_source"] == "cpf_registration_policy"
    assert calls == []
