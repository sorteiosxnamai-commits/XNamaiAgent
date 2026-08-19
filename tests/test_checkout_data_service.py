import pytest
from types import SimpleNamespace

from app.checkout_data_service import (
    _normalize_phone,
    checkout_data_template,
    enrich_checkout_data_from_cep,
    lookup_address_by_zipcode,
    normalize_brazilian_state,
    repair_checkout_data_with_openai,
    should_repair_checkout_data,
    update_checkout_data,
)
from app.commerce_context import CommerceConversationState, evolve_commerce_state
from openai_test_utils import install_fake_openai_client


@pytest.mark.parametrize("value,expected", [
    ("+55 43 99999-9999", "43999999999"),
    ("5543999999999", "43999999999"),
    ("55 43 3333-4444", "4333334444"),
    ("43999999999", "43999999999"),
    ("4333334444", "4333334444"),
    ("(43) 99999-9999", "43999999999"),
    ("5543 9999", None),
    ("999999999", None),
    ("", None),
    (None, None),
])
def test_normalize_phone_canonizes_to_adapter_contract(value, expected):
    assert _normalize_phone(value) == expected


def test_update_checkout_data_strips_ddi_and_validates_phone():
    state = CommerceConversationState(
        checkout_channel_preference="whatsapp",
        checkout_draft={
            "customer": {"type": "0"},
            "address": {"country": "BRA", "type": "1", "zip_code": "86480000"},
        },
    )
    result = update_checkout_data(state, {
        "name": "Test User",
        "phone": "+55 43 99999-9999",
        "email": "test@example.com",
        "cpf": "52998224725",
        "address": "Rua Test",
        "number": "123",
        "neighborhood": "Centro",
        "city": "Cornelio Procopio",
        "state": "PR",
        "zipcode": "86480000",
    })
    updated = evolve_commerce_state(state, result)

    assert result.commercial_data["field_errors"] == {}
    assert updated.checkout_draft.customer.phone == "43999999999"
    assert len(updated.checkout_draft.customer.phone) == 11


@pytest.mark.parametrize("value,expected", [
    ("Paraná", "PR"),
    ("parana", "PR"),
    ("PR", "PR"),
    ("São Paulo", "SP"),
    ("distrito federal", "DF"),
    ("estado desconhecido", None),
])
def test_normalize_brazilian_state_accepts_names_and_uf(value, expected):
    assert normalize_brazilian_state(value) == expected


def test_checkout_template_requests_only_unresolved_fields():
    template = checkout_data_template(
        ["email", "state"],
        {"state": "invalid_state"},
    )

    assert "E-mail: <seu e-mail>" in template
    assert "Estado/UF: <nome do estado ou sigla>" in template
    assert "Nome completo:" not in template
    assert "Não precisa repetir" in template


def test_customer_block_normalizes_parana_and_requests_only_missing_email():
    state = CommerceConversationState(checkout_channel_preference="whatsapp")
    result = update_checkout_data(state, {
        "name": "Paulo Regis Tironi",
        "cpf": "07281035918",
        "phone": "85999498149",
        "address": "Rua informada pelo cliente",
        "zipcode": "86480000",
        "number": "81",
        "neighborhood": "Centro",
        "city": "Conselheiro Mairinck",
        "state": "parana",
    })
    updated = evolve_commerce_state(state, result)

    assert result.commercial_data["field_errors"] == {}
    assert result.commercial_data["missing_fields"] == ["email"]
    assert updated.checkout_draft.address.state == "PR"
    assert "E-mail: <seu e-mail>" in result.reply_text
    assert "Estado/UF:" not in result.reply_text


def test_dense_checkout_message_uses_repair_for_possible_missed_fields():
    assert should_repair_checkout_data(
        "Nome\nCPF\nTelefone\nCEP\nCidade",
        {"name": "Nome", "cpf": "52998224725", "phone": "43999999999"},
        ["email"],
        {},
    ) is True
    assert should_repair_checkout_data(
        "meu email mudou",
        {"email": "novo@example.com"},
        [],
        {},
    ) is False


@pytest.mark.asyncio
async def test_openai_repair_changes_only_unresolved_fields(monkeypatch):
    import app.checkout_data_service as service
    from app.models import CheckoutDataInput

    class Completions:
        async def parse(self, **_kwargs):
            message = SimpleNamespace(
                parsed=CheckoutDataInput(name="Nome inventado", state="PR"),
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class Client:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=Completions())

    install_fake_openai_client(monkeypatch, Client)
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-key",
            openai_model="gpt-4.1-mini",
        ),
    )

    repaired = await repair_checkout_data_with_openai(
        message_text="Maria\nParan",
        updates={"name": "Maria", "state": "Paran"},
        missing_fields=[],
        field_errors={"state": "invalid_state"},
    )

    assert repaired == {"name": "Maria", "state": "PR"}


@pytest.mark.asyncio
async def test_incomplete_checkout_is_validated_before_any_tray_call(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        raise AssertionError("Tray must not be called with incomplete checkout data")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    state = CommerceConversationState(
        checkout_channel_preference="whatsapp",
        cart_session_id="SESSION-1",
        cart_items=[{"product_id": "1997", "quantity": 1}],
    )
    partial = update_checkout_data(state, {
        "name": "Paulo Regis Tironi",
        "cpf": "07281035918",
        "phone": "85999498149",
        "address": "Rua informada pelo cliente",
        "zipcode": "86480000",
        "number": "81",
        "neighborhood": "Centro",
        "city": "Conselheiro Mairinck",
        "state": "Paraná",
    })

    advanced = await sales_agent._advance_whatsapp_checkout(
        state,
        partial,
        payment_preference=None,
        installment_count=None,
    )

    assert calls == []
    assert advanced.commercial_data["missing_fields"] == ["email"]
    assert "E-mail: <seu e-mail>" in advanced.reply_text


@pytest.mark.asyncio
async def test_cep_lookup_returns_only_validated_checkout_fields(monkeypatch):
    import app.checkout_data_service as service

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "cep": "86480-000",
                "logradouro": "Rua Principal",
                "bairro": "Centro",
                "localidade": "Conselheiro Mairinck",
                "uf": "PR",
                "ibge": "ignored",
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return Response()

    monkeypatch.setattr(service.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(
            checkout_cep_lookup_enabled=True,
            checkout_cep_lookup_url="https://viacep.test/ws",
        ),
    )

    resolved = await lookup_address_by_zipcode("86480-000")

    assert resolved == {
        "address": "Rua Principal",
        "neighborhood": "Centro",
        "city": "Conselheiro Mairinck",
        "state": "PR",
        "zipcode": "86480000",
    }
    assert "ibge" not in resolved


@pytest.mark.asyncio
async def test_cep_enrichment_fills_address_without_inventing_customer_fields(
    monkeypatch,
):
    import app.checkout_data_service as service

    async def lookup(_zipcode):
        return {
            "address": "Rua Principal",
            "neighborhood": "Centro",
            "city": "Conselheiro Mairinck",
            "state": "PR",
            "zipcode": "86480000",
        }

    monkeypatch.setattr(service, "lookup_address_by_zipcode", lookup)
    enriched = await enrich_checkout_data_from_cep(
        {"zipcode": "86480000", "number": "81", "name": "Paulo"},
        missing_fields=["address", "neighborhood", "city", "state", "email"],
        field_errors={},
    )

    assert enriched["address"] == "Rua Principal"
    assert enriched["state"] == "PR"
    assert enriched["number"] == "81"
    assert enriched["name"] == "Paulo"
    assert "email" not in enriched
