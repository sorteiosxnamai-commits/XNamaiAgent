"""Customer registration conversations and definitive/ambiguous mutation results."""

import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.customer_registration import handle_customer_registration_turn, customer_payload, validate_registration_draft
from app.commerce.mercos.customer_validation import parse_customer_validation


CPF = "52998224725"
CNPJ = "11222333000181"


async def turn(state, message, execute, **kwargs):
    result = await handle_customer_registration_turn(message, state=state, execute=execute, **kwargs)
    return evolve_commerce_state(state, result), result


@pytest.mark.asyncio
async def test_pf_one_question_at_a_time_with_whatsapp_phone():
    calls = []

    async def execute(name, args):
        calls.append((name, args))
        return {"ok": True, "found": False} if name.startswith("lookup") else {"ok": True, "customer_id": "71"}

    state = CommerceConversationState()
    state, result = await turn(state, "quero me cadastrar", execute, sender_phone="5543999999999")
    assert "CPF ou CNPJ" in result.reply_text and "nome" not in result.reply_text
    state, result = await turn(state, CPF, execute, sender_phone="5543999999999")
    assert "nome completo" in result.reply_text
    state, result = await turn(state, "João Pedro Ferreira Matos", execute, sender_phone="5543999999999")
    assert "e-mail" in result.reply_text
    state, result = await turn(state, "joao@example.com", execute, sender_phone="5543999999999")
    assert "Confira" in result.reply_text and "telefone" in result.reply_text.casefold()
    assert CPF not in result.reply_text and "joao@example.com" not in result.reply_text
    state, result = await turn(state, "ok", execute)
    assert calls == []
    state, result = await turn(state, "confirmo o cadastro", execute)
    assert [name for name, _ in calls] == ["lookup_customer_by_document", "create_customer"]
    assert calls[1][1] == {"tipo": "F", "razao_social": "João Pedro Ferreira Matos", "cnpj": CPF,
                            "emails": [{"email": "joao@example.com"}], "telefones": [{"numero": "43999999999"}]}
    assert state.mercos_customer_id == "71"


@pytest.mark.asyncio
async def test_pj_payload_and_422_collect_then_reconfirm():
    calls = []

    async def execute(name, args):
        calls.append((name, args))
        if name.startswith("lookup"):
            return {"ok": True, "found": False}
        if len([call for call in calls if call[0] == "create_customer"]) == 1:
            return {"ok": False, "code": "customer_validation", "fields": ["telefones"]}
        return {"ok": True, "customer_id": "72"}

    state = CommerceConversationState()
    for message in ("quero cadastrar minha empresa", CNPJ, "Empresa Exemplo Ltda",
                    "nome fantasia: Loja Exemplo", "contato@example.com"):
        state, result = await turn(state, message, execute, sender_phone="5543999999999")
    assert "Confira" in result.reply_text
    state, result = await turn(state, "confirmo o cadastro", execute, sender_phone="5543999999999")
    assert "telefone" in result.reply_text and result.handoff_required is False
    state, result = await turn(state, "43988887777", execute, sender_phone="5543999999999")
    assert "Confira" in result.reply_text
    state, result = await turn(state, "confirmo o cadastro", execute)
    posts = [args for name, args in calls if name == "create_customer"]
    assert len(posts) == 2 and posts[-1]["tipo"] == "J"
    assert posts[-1]["nome_fantasia"] == "Loja Exemplo"
    assert posts[-1]["telefones"] == [{"numero": "43988887777"}]
    assert state.mercos_customer_id == "72"


@pytest.mark.asyncio
async def test_pending_sync_links_on_later_lookup_without_second_post():
    calls = []
    synced = False

    async def execute(name, args):
        nonlocal synced
        calls.append(name)
        if name == "lookup_customer_by_document":
            return {"ok": True, "found": synced, "customer_id": "73" if synced else None}
        return {"ok": True, "status": "CREATED_PENDING_SYNC"}

    state = CommerceConversationState()
    for message in ("quero me cadastrar", CPF, "João Pedro Ferreira Matos", "joao@example.com"):
        state, _ = await turn(state, message, execute, sender_phone="5543999999999")
    state, result = await turn(state, "confirmo o cadastro", execute)
    assert state.customer_registration["status"] == "created_pending_sync"
    assert calls == ["lookup_customer_by_document", "create_customer"]
    synced = True
    state, result = await turn(state, "quero me cadastrar", execute)
    assert state.mercos_customer_id == "73"
    assert calls == ["lookup_customer_by_document", "create_customer", "lookup_customer_by_document"]


@pytest.mark.asyncio
async def test_existing_document_links_without_post():
    calls = []
    async def execute(name, args):
        calls.append(name)
        return {"ok": True, "found": True, "customer_id": "74"}

    state = CommerceConversationState()
    for message in ("quero me cadastrar", CPF, "João Pedro Ferreira Matos", "joao@example.com"):
        state, _ = await turn(state, message, execute, sender_phone="5543999999999")
    state, result = await turn(state, "confirmo o cadastro", execute)
    assert calls == ["lookup_customer_by_document"] and state.mercos_customer_id == "74"


def test_parser_only_recognizes_proven_required_fields():
    assert parse_customer_validation({"telefones": ["No mínimo 1 telefone é requerido"]}) == ("telefones",)
    assert parse_customer_validation({"emails": ["No mínimo 1 e-mail é requerido"]}) == ("emails",)
    assert parse_customer_validation({"bairro": ["Este campo é obrigatório."]}) == ("bairro",)
    assert parse_customer_validation({"random": ["Este campo é obrigatório."]}) == ()


@pytest.mark.asyncio
async def test_unmapped_422_is_definitive_handoff_without_retry():
    calls = []
    async def execute(name, args):
        calls.append(name)
        return ({"ok": True, "found": False} if name.startswith("lookup") else
                {"ok": False, "code": "customer_validation", "fields": []})

    state = CommerceConversationState()
    for message in ("quero me cadastrar", CPF, "João Pedro Ferreira Matos", "joao@example.com"):
        state, _ = await turn(state, message, execute, sender_phone="5543999999999")
    state, result = await turn(state, "confirmo o cadastro", execute)
    assert result.handoff_required and state.customer_registration["status"] == "failed"
    assert calls == ["lookup_customer_by_document", "create_customer"]


@pytest.mark.asyncio
async def test_provider_422_releases_claim_and_returns_only_field_names():
    import httpx
    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.customer_index import CreationClaim, CustomerLookupResult, CustomerLookupStatus
    from app.commerce.mercos.provider import MercosCommerceProvider

    class Index:
        outcomes = []
        def claim_creation(self, document):
            return CreationClaim(True, CustomerLookupResult(CustomerLookupStatus.NOT_FOUND))
        def finish_creation(self, document, *, outcome, customer_id=None):
            self.outcomes.append(outcome)

    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(422, json={"details": {"telefones": ["No mínimo 1 telefone é requerido"]}})

    adaptor = MercosAdaptorClient(base_url="https://adaptor.example.com", api_key="internal",
                                  customer_mutations_enabled=True, transport=httpx.MockTransport(handler))
    index = Index()
    provider = MercosCommerceProvider(adaptor, tenant_id="xnamai", customer_index=index)
    result = await provider.execute("create_customer", {"tipo": "F", "razao_social": "Cliente Exemplo", "cnpj": CPF})
    assert result == {"ok": False, "code": "customer_validation", "fields": ["telefones"]}
    assert index.outcomes == ["released"] and len(requests) == 1


def test_alphanumeric_cnpj_uses_receita_check_digits():
    normalized, errors = validate_registration_draft({"document": "12ABC34501DE35", "legal_name": "Empresa Exemplo",
                                                      "email": "a@example.com", "phone": "43999999999"})
    assert errors == {} and normalized["document"] == "12ABC34501DE35"


@pytest.mark.asyncio
async def test_adaptor_2xx_without_id_keeps_claim_and_resolves_after_sync(monkeypatch):
    from app.commerce.mercos.customer_index import CreationClaim, CustomerLookupResult, CustomerLookupStatus
    from app.commerce.mercos.provider import MercosCommerceProvider

    class Index:
        def __init__(self):
            self.status = CustomerLookupStatus.NOT_FOUND
            self.outcomes = []

        def claim_creation(self, document):
            return CreationClaim(self.status == CustomerLookupStatus.NOT_FOUND,
                                 CustomerLookupResult(self.status, "72" if self.status == CustomerLookupStatus.FOUND else None))

        def finish_creation(self, document, *, outcome, customer_id=None):
            self.outcomes.append(outcome)
            if outcome == "created_pending_sync":
                self.status = CustomerLookupStatus.CREATION_PENDING
            elif outcome == "created":
                self.status = CustomerLookupStatus.FOUND

        def lookup_customer_by_document(self, document):
            return CustomerLookupResult(self.status, "72" if self.status == CustomerLookupStatus.FOUND else None)

    class Adaptor:
        customer_mutations_enabled = True
        posts = 0

        async def create_customer(self, payload):
            self.posts += 1
            return None

    index, adaptor = Index(), Adaptor()
    provider = MercosCommerceProvider(adaptor, tenant_id="xnamai", customer_index=index)

    async def sync():
        index.status = CustomerLookupStatus.FOUND
        return {"ok": True}

    monkeypatch.setattr(provider, "run_customer_sync", sync)
    payload = {"tipo": "F", "razao_social": "Cliente Exemplo", "cnpj": CPF}
    first = await provider.execute("create_customer", payload)
    second = await provider.execute("create_customer", payload)
    assert first == {"ok": True, "customer_id": "72"}
    assert second["linked_existing"] is True
    assert adaptor.posts == 1
    assert index.outcomes == ["created_pending_sync", "created"]


@pytest.mark.asyncio
async def test_adaptor_2xx_without_sync_id_never_reposts(monkeypatch):
    from app.commerce.mercos.customer_index import CreationClaim, CustomerLookupResult, CustomerLookupStatus
    from app.commerce.mercos.provider import MercosCommerceProvider

    class Index:
        pending = False
        def claim_creation(self, document):
            status = CustomerLookupStatus.CREATION_PENDING if self.pending else CustomerLookupStatus.NOT_FOUND
            return CreationClaim(not self.pending, CustomerLookupResult(status))
        def finish_creation(self, document, *, outcome, customer_id=None):
            self.pending = True
        def lookup_customer_by_document(self, document):
            return CustomerLookupResult(CustomerLookupStatus.CREATION_PENDING)

    class Adaptor:
        customer_mutations_enabled = True
        posts = 0
        async def create_customer(self, payload):
            self.posts += 1
            return None

    index, adaptor = Index(), Adaptor()
    provider = MercosCommerceProvider(adaptor, tenant_id="xnamai", customer_index=index)

    async def sync():
        return {"ok": False}
    monkeypatch.setattr(provider, "run_customer_sync", sync)
    payload = {"tipo": "F", "razao_social": "Cliente Exemplo", "cnpj": CPF}
    assert (await provider.execute("create_customer", payload))["status"] == "CREATED_PENDING_SYNC"
    assert (await provider.execute("create_customer", payload))["lookup_status"] == "CREATION_PENDING"
    assert adaptor.posts == 1
