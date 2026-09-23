from types import SimpleNamespace

import httpx
import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.customer_registration import (
    PENDING_REGISTRATION_CONFIRMATION,
    PENDING_REGISTRATION_DATA,
    customer_payload,
    handle_customer_registration_turn,
    parse_registration_fields,
    validate_registration_draft,
)


VALID_FIELDS = """\
Tipo: PF
Nome completo/Razão social: Maria da Silva
CPF/CNPJ: 529.982.247-25
E-mail: maria@example.com
Telefone: (11) 99999-8888
"""


def test_labelled_registration_parser_never_invents_absent_fields():
    parsed = parse_registration_fields(VALID_FIELDS)
    assert parsed == {
        "person_type": "PF",
        "legal_name": "Maria da Silva",
        "document": "529.982.247-25",
        "email": "maria@example.com",
        "phone": "(11) 99999-8888",
    }
    assert "trade_name" not in parsed


@pytest.mark.parametrize(
    "draft,error",
    [
        ({"person_type": "PF", "legal_name": "Maria", "document": "11111111111", "email": "maria@example.com", "phone": "11999998888"}, "invalid_cpf"),
        ({"person_type": "PJ", "legal_name": "Empresa", "document": "11111111111111", "email": "contato@example.com", "phone": "11999998888"}, "invalid_cnpj"),
        ({"person_type": "PF", "legal_name": "Maria", "document": "52998224725", "email": "invalido", "phone": "11999998888"}, "invalid_email"),
    ],
)
def test_registration_validation_rejects_invalid_customer_data(draft, error):
    _, errors = validate_registration_draft(draft)
    assert error in errors.values()


@pytest.mark.asyncio
async def test_registration_requires_review_and_explicit_confirmation():
    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "customer_id": "321"}

    state = CommerceConversationState()
    started = await handle_customer_registration_turn(
        "quero me cadastrar", state=state, execute=execute,
    )
    assert "Tipo: PF ou PJ" in started.reply_text
    assert calls == []
    state = evolve_commerce_state(state, started)
    assert state.pending_action == PENDING_REGISTRATION_DATA

    reviewed = await handle_customer_registration_turn(
        VALID_FIELDS, state=state, execute=execute,
    )
    assert "Confira os dados" in reviewed.reply_text
    assert "52998224725" not in reviewed.reply_text
    assert "maria@example.com" not in reviewed.reply_text
    assert calls == []
    state = evolve_commerce_state(state, reviewed)
    assert state.pending_action == PENDING_REGISTRATION_CONFIRMATION

    created = await handle_customer_registration_turn(
        "confirmo o cadastro", state=state, execute=execute,
    )
    assert "sucesso" in created.reply_text
    assert len(calls) == 1
    assert calls[0][0] == "create_customer"
    assert calls[0][1] == customer_payload(state.customer_registration["draft"])
    state = evolve_commerce_state(state, created)
    assert state.mercos_customer_id == "321"
    assert state.pending_action is None


@pytest.mark.asyncio
async def test_unknown_mutation_result_is_never_retried():
    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": False, "code": "mutation_state_unknown"}

    normalized, errors = validate_registration_draft(parse_registration_fields(VALID_FIELDS))
    assert errors == {}
    state = CommerceConversationState(
        pending_action=PENDING_REGISTRATION_CONFIRMATION,
        customer_registration={"status": "review", "draft": normalized},
    )
    result = await handle_customer_registration_turn(
        "confirmo", state=state, execute=execute,
    )
    assert result.handoff_required is True
    assert "Não vou reenviar" in result.reply_text
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_mercos_provider_creates_customer_only_when_gate_is_open():
    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.provider import MercosCommerceProvider

    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(201, json={"id": 321})

    client = MercosAdaptorClient(
        base_url="https://adaptor.example.com",
        api_key="internal",
        customer_mutations_enabled=True,
        transport=httpx.MockTransport(handler),
    )
    provider = MercosCommerceProvider(client, tenant_id="xnamai")
    normalized, errors = validate_registration_draft(parse_registration_fields(VALID_FIELDS))
    assert errors == {}
    result = await provider.execute("create_customer", customer_payload(normalized))
    assert result == {"ok": True, "customer_id": "321"}
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/customers"


@pytest.mark.asyncio
@pytest.mark.offline_eval
async def test_registration_route_skips_openai_interpretation(monkeypatch):
    import app.openai_agent as agent

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("OpenAI interpretation must not run for registration")

    monkeypatch.setattr(agent, "interpret_message", forbidden)
    monkeypatch.setattr(agent, "load_recent_conversation_turns", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "app.capability_catalog.runtime_commerce_capabilities",
        lambda: frozenset({"create_customer"}),
    )
    result = await agent.generate_agent_reply_async(
        agent.IncomingMessage(text="quero me cadastrar"),
        {"_commerce_state": CommerceConversationState()},
    )
    assert "Tipo: PF ou PJ" in result.reply_text
    assert result.response_metadata["used_openai_interpreter"] is False
    assert result.response_metadata["used_openai_responder"] is False


@pytest.mark.asyncio
async def test_disabled_registration_does_not_collect_personal_data():
    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "customer_id": "321"}

    result = await handle_customer_registration_turn(
        "quero me cadastrar",
        state=CommerceConversationState(),
        execute=execute,
        registration_enabled=False,
    )
    assert result.handoff_required is True
    assert result.safety_reason == "customer_registration_unavailable"
    assert "CPF" not in result.reply_text
    assert calls == []


def test_build_provider_propagates_customer_mutation_gate():
    from app.commerce.mercos.provider import build_mercos_provider

    settings = SimpleNamespace(
        mercos_adaptor_url="https://adaptor.example.com",
        mercos_adaptor_api_key="internal",
        mercos_adaptor_timeout_seconds=5,
        mercos_customer_mutations_enabled=True,
        commerce_tenant_id="xnamai",
        database_url="",
    )
    provider = build_mercos_provider(settings)
    assert "create_customer" in provider.runtime_capabilities


def test_customer_payload_is_redacted_from_observability():
    from app.observability import redact_value

    normalized, errors = validate_registration_draft(parse_registration_fields(VALID_FIELDS))
    assert errors == {}
    redacted = redact_value(customer_payload(normalized))
    serialized = repr(redacted)
    for private_value in (
        "Maria da Silva",
        "52998224725",
        "maria@example.com",
        "11999998888",
    ):
        assert private_value not in serialized
    assert redacted["razao_social"] == "[REDACTED]"
    assert redacted["nome_fantasia"] == "[REDACTED]"
    assert redacted["cnpj"] == "[REDACTED]"
    assert redacted["email"] == "[REDACTED]"
    assert redacted["telefones"] == "[REDACTED]"
