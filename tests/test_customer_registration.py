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
        if name == "lookup_customer_by_document":
            return {"ok": True, "found": False}
        return {"ok": True, "customer_id": "321"}

    state = CommerceConversationState()
    started = await handle_customer_registration_turn(
        "quero me cadastrar", state=state, execute=execute,
    )
    # pede so o que falta (sem o formulario inteiro), e nada e enviado ainda
    assert "CPF ou CNPJ" in started.reply_text and "e-mail" in started.reply_text
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
    # duplicidade consultada ANTES da unica criacao
    assert [name for name, _ in calls] == ["lookup_customer_by_document", "create_customer"]
    assert calls[0][1] == {"document": "52998224725"}
    assert calls[1][1] == customer_payload(state.customer_registration["draft"])
    state = evolve_commerce_state(state, created)
    assert state.mercos_customer_id == "321"
    assert state.pending_action is None


@pytest.mark.asyncio
async def test_unknown_mutation_result_is_never_retried():
    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        if name == "lookup_customer_by_document":
            return {"ok": True, "found": False}
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
    assert [name for name, _ in calls] == ["lookup_customer_by_document", "create_customer"]


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
    from app.commerce.mercos.customer_index import CreationClaim, CustomerLookupResult, CustomerLookupStatus

    class ClaimingIndex:
        """Index that grants the claim (the lock itself is proven on PostgreSQL)."""

        finished = []

        def claim_creation(self, document):
            return CreationClaim(True, CustomerLookupResult(CustomerLookupStatus.NOT_FOUND))

        def finish_creation(self, document, *, outcome, customer_id=None):
            self.finished.append((outcome, customer_id))

    index = ClaimingIndex()
    provider = MercosCommerceProvider(client, tenant_id="xnamai", customer_index=index)
    normalized, errors = validate_registration_draft(parse_registration_fields(VALID_FIELDS))
    assert errors == {}
    result = await provider.execute("create_customer", customer_payload(normalized))
    assert result == {"ok": True, "customer_id": "321"}
    assert index.finished == [("created", "321")]
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
        lambda: frozenset({"create_customer", "lookup_customer_by_document"}),
    )
    result = await agent.generate_agent_reply_async(
        agent.IncomingMessage(text="quero me cadastrar"),
        {"_commerce_state": CommerceConversationState()},
    )
    assert "CPF ou CNPJ" in result.reply_text
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
    assert "create_customer" not in provider.runtime_capabilities


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
    assert redacted["emails"] == "[REDACTED]"
    assert redacted["telefones"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_create_customer_alone_does_not_enable_registration(monkeypatch):
    """Sem busca por documento nao ha como evitar duplicidade: nada e coletado."""
    import app.openai_agent as agent

    monkeypatch.setattr(agent, "load_recent_conversation_turns", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "app.capability_catalog.runtime_commerce_capabilities",
        lambda: frozenset({"create_customer"}),
    )
    result = await agent.generate_agent_reply_async(
        agent.IncomingMessage(text="quero me cadastrar"),
        {"_commerce_state": CommerceConversationState()},
    )
    assert result.safety_reason == "customer_registration_unavailable"
    assert "CPF" not in result.reply_text


@pytest.mark.asyncio
async def test_create_customer_without_index_never_posts():
    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.provider import MercosCommerceProvider

    requests = []
    client = MercosAdaptorClient(
        base_url="https://adaptor.example.com", api_key="internal", customer_mutations_enabled=True,
        transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(201)),
    )
    normalized, _ = validate_registration_draft(parse_registration_fields(VALID_FIELDS))
    result = await MercosCommerceProvider(client, tenant_id="xnamai").execute(
        "create_customer", customer_payload(normalized))
    assert result["ok"] is False and result["lookup_status"] == "INDEX_NOT_READY"
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), [
    ("AMBIGUOUS", "ambiguous"), ("CREATION_PENDING", "creation_pending"),
    ("INDEX_NOT_READY", "lookup_failed"), ("TIMEOUT", "lookup_failed"),
])
async def test_lookup_other_than_found_or_not_found_blocks_creation(status, expected):
    calls = []

    async def execute(name, arguments):
        calls.append(name)
        return {"ok": False, "status": status, "found": False, "customer_id": None}

    normalized, _ = validate_registration_draft(parse_registration_fields(VALID_FIELDS))
    state = CommerceConversationState(
        pending_action=PENDING_REGISTRATION_CONFIRMATION,
        customer_registration={"status": "review", "draft": normalized},
    )
    result = await handle_customer_registration_turn("confirmo o cadastro", state=state, execute=execute)
    assert calls == ["lookup_customer_by_document"]  # zero POST
    assert result.handoff_required is True
    assert result.response_metadata["customer_registration_state"]["status"] == expected
    for secret in ("52998224725", "maria@example.com"):
        assert secret not in result.reply_text

