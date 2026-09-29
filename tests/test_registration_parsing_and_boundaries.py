"""Cadastro: deteccao no Intent Router, parser natural, dados do canal e fronteiras."""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.customer_registration import (
    PENDING_REGISTRATION_CONFIRMATION,
    PENDING_REGISTRATION_DATA,
    extract_registration_fields,
    handle_customer_registration_turn,
)
from app.models import AgentResult
from app.sales.intent_router import account_intent_from_text, is_customer_registration_request

REPO = pathlib.Path(__file__).resolve().parents[1]
CPF = "529.982.247-25"
CNPJ = "11.222.333/0001-81"


# --- deteccao (gramatica, nao lista de frases) ------------------------------------


@pytest.mark.parametrize("text", [
    "gostaria de me cadastrar", "quero me cadastrar", "gostaria de fazer meu cadastro", "quero fazer o cadastro",
    "preciso me cadastrar", "como faço meu cadastro?", "cadastro", "quero criar cadastro", "queria me cadastrar",
    "posso me cadastrar?", "Cadastro!", "QUERO ME CADASTRAR", "gostaria de realizar o cadastro",
    "onde faço o cadastro", "me cadastra aí", "quero abrir um cadastro de cliente", "cadastrar",
    "preciso fazer cadastro pra comprar",
])
def test_registration_requests_are_recognised(text):
    assert is_customer_registration_request(text)
    assert account_intent_from_text(text) == "customer_registration"


@pytest.mark.parametrize("text", [
    "já tenho cadastro", "quero atualizar meu cadastro", "não quero me cadastrar", "meu cadastro está desatualizado",
    "quero me cadastrar no club", "cadastro no clube", "o produto está cadastrado no site?", "oi", "quanto custa o cabo?",
    "quero alterar o email do cadastro", "cancelar cadastro", "",
])
def test_non_registration_messages_are_not_recognised(text):
    assert not is_customer_registration_request(text)


# --- parser natural -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"meu email é Maria@Example.com e meu cpf é {CPF}", {"email": "Maria@Example.com", "document": "52998224725"}),
        (f"cpf {CPF.replace('.', '').replace('-', '')}", {"document": "52998224725"}),
        (f"cnpj {CNPJ}", {"document": "11222333000181"}),
        ("meu nome é Maria Souza e meu cpf é 52998224725",
         {"legal_name": "Maria Souza", "document": "52998224725"}),
        ("me chamo João da Silva", {"legal_name": "João da Silva"}),
        ("meu telefone é (11) 98888-7777", {"phone": "11988887777"}),
        ("razão social é Loja Exemplo Ltda, nome fantasia é Exemplo", {"legal_name": "Loja Exemplo Ltda",
                                                                         "trade_name": "Exemplo"}),
        ("sou pessoa jurídica", {"person_type": "PJ"}),
    ],
)
def test_natural_values_are_extracted(text, expected):
    found = extract_registration_fields(text)
    for key, value in expected.items():
        assert found.get(key) == value, (key, found)


def test_labelled_lines_still_work_and_win():
    found = extract_registration_fields("Nome: Ana Lima\nCPF: 529.982.247-25\nE-mail: ana@example.com")
    assert found == {"legal_name": "Ana Lima", "document": "529.982.247-25", "email": "ana@example.com"}


def test_invalid_cpf_is_captured_only_when_announced_so_the_error_is_reported():
    assert extract_registration_fields("meu cpf é 123.456.789-00")["document"] == "12345678900"
    assert "document" not in extract_registration_fields("o código é 12345678900")


def test_phone_is_not_mistaken_for_a_cpf_and_nothing_is_invented():
    found = extract_registration_fields("pode ligar no 11988887777")
    assert "document" not in found
    assert extract_registration_fields("quero me cadastrar") == {}
    assert extract_registration_fields("oi, tudo bem?") == {}


# --- dados do canal e precedencia ------------------------------------------------------


async def _turn(text, state, **kwargs):
    async def execute(name, arguments):  # pragma: no cover - nenhum envio nestes testes
        raise AssertionError(f"nenhuma chamada comercial era esperada: {name}")

    result = await handle_customer_registration_turn(text, state=state, execute=execute, **kwargs)
    return result, evolve_commerce_state(state, result)


@pytest.mark.asyncio
async def test_whatsapp_phone_is_reused_and_the_customer_can_override_it():
    state = CommerceConversationState()
    started, state = await _turn("quero me cadastrar", state, sender_phone="5511988887777")
    assert "telefone" not in started.reply_text.casefold()
    assert state.customer_registration["draft"]["phone"] == "11988887777"
    assert state.customer_registration["sources"]["phone"] == "channel"
    _, state = await _turn("meu telefone é (21) 97777-6666", state, sender_phone="5511988887777")
    assert state.customer_registration["draft"]["phone"] == "21977776666"  # explicito > canal
    assert state.customer_registration["sources"]["phone"] == "customer"


@pytest.mark.asyncio
async def test_draft_value_beats_channel_value():
    state = CommerceConversationState()
    _, state = await _turn("quero me cadastrar", state)
    _, state = await _turn("nome: Ana Lima", state, sender_name="Ana WhatsApp")
    _, state = await _turn(f"cpf {CPF}", state, sender_name="Ana WhatsApp")
    assert state.customer_registration["draft"]["legal_name"] == "Ana Lima"


@pytest.mark.asyncio
async def test_channel_person_name_is_never_used_as_company_name():
    state = CommerceConversationState()
    _, state = await _turn("quero me cadastrar", state, sender_name="Ana Lima", sender_phone="5511988887777")
    result, state = await _turn(f"cnpj {CNPJ} email loja@example.com", state, sender_name="Ana Lima",
                                sender_phone="5511988887777")
    assert state.pending_action == PENDING_REGISTRATION_DATA
    assert "legal_name" not in state.customer_registration["draft"]
    assert "razão social" in result.reply_text


@pytest.mark.asyncio
async def test_invalid_value_is_pointed_out_without_resending_the_whole_form():
    state = CommerceConversationState()
    _, state = await _turn("quero me cadastrar", state, sender_phone="5511988887777")
    result, state = await _turn("meu cpf é 123.456.789-00, email ana@example.com, nome: Ana Lima", state,
                                sender_phone="5511988887777")
    assert "CPF informado não é válido" in result.reply_text
    assert "Tipo: PF ou PJ" not in result.reply_text
    assert state.pending_action == PENDING_REGISTRATION_DATA


@pytest.mark.asyncio
async def test_handoff_ends_a_pending_registration():
    state = CommerceConversationState(pending_action=PENDING_REGISTRATION_CONFIRMATION,
                                      customer_registration={"status": "review", "draft": {"email": "a@b.com"}})
    state = evolve_commerce_state(state, AgentResult(reply_text="Vou chamar a equipe.", handoff_required=True))
    assert state.pending_action is None
    assert state.customer_registration["draft"] == {}


# --- fronteiras -------------------------------------------------------------------------


def test_qualification_slots_and_dialogue_phase_never_touch_registration_pii():
    for name in ("app/sales/qualification_slots.py", "app/sales/dialogue_phase.py"):
        source = (REPO / name).read_text(encoding="utf-8").casefold()
        assert "customer_registration" not in source and "cpf" not in source and "cnpj" not in source, name


def test_registration_flow_never_imports_the_llm_or_persona():
    tree = ast.parse((REPO / "app/customer_registration.py").read_text(encoding="utf-8"))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not [m for m in imported if any(t in m for t in ("openai", "persona", "prompt", "llm"))], imported


def test_registration_draft_is_redacted_in_logs():
    from app.observability import redact_value

    redacted = redact_value({"customer_registration": {"draft": {
        "document": "52998224725", "email": "a@b.com", "phone": "11988887777", "legal_name": "Ana Lima"}}})
    text = str(redacted)
    for secret in ("52998224725", "a@b.com", "11988887777", "Ana Lima"):
        assert secret not in text


# --- URLs oficiais: fonte unica --------------------------------------------------------


def _validate(reply: str):
    from app.agent_contracts import build_agent_decision
    from app.factual_validator import validate_factual_response
    from app.models import IncomingMessage

    result = AgentResult(reply_text=reply)
    decision = build_agent_decision(IncomingMessage(channel="whatsapp", sender_key="whatsapp:t", text="club"),
                                    result, openai_call_count=0)
    return validate_factual_response(result, decision=decision, mode="enforce")


def test_official_public_urls_is_the_single_source():
    from app.site_knowledge import CLUB_URL, SITE_URL, STORE_URL, official_public_urls

    assert official_public_urls() == {SITE_URL, STORE_URL, CLUB_URL}
    validator = (REPO / "app/factual_validator.py").read_text(encoding="utf-8")
    assert "official_public_urls" in validator
    assert "clubxnamai" not in validator  # nada de URL chumbada no validador


@pytest.mark.parametrize("url", [
    "https://www.clubxnamai.com.br/", "https://www.clubxnamai.com.br", "https://www.xnamai.com/",
    "https://xnamai.meuspedidos.com.br/",
])
def test_official_entry_points_pass_the_validator(url):
    report = _validate(f"Confira em {url}")
    assert not [v for v in report.violations if v.kind == "url"], report.violations


@pytest.mark.parametrize("url", [
    "https://www.clubxnamai.com.br/checkout?plano=anual", "https://xnamai.meuspedidos.com.br/produto/123",
    "https://www.xnamai.com/pagamento/abc",
])
def test_product_checkout_and_payment_paths_still_need_evidence(url):
    report = _validate(f"Pague em {url}")
    assert [v for v in report.violations if v.kind == "url"], report.violations
