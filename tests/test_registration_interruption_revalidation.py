"""A deterministic registration question must consume data, not a commerce detour."""

import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.customer_registration import handle_customer_registration_turn


@pytest.mark.asyncio
async def test_product_question_while_waiting_for_legal_name_preserves_draft():
    async def no_tool(*_args, **_kwargs):
        raise AssertionError("registration must not call a tool before confirmation")

    state = CommerceConversationState()
    for message in ("quero me cadastrar", "52998224725"):
        result = await handle_customer_registration_turn(message, state=state, execute=no_tool)
        state = evolve_commerce_state(state, result)
    assert state.pending_action == "awaiting_customer_registration_data"
    before = dict(state.customer_registration["draft"])
    detour = await handle_customer_registration_turn(
        "vocês têm iPhone 17?", state=state, execute=no_tool,
    )
    assert detour is None
    assert state.customer_registration["draft"] == before


@pytest.mark.asyncio
async def test_422_address_fields_are_consumed_once_with_cep_enrichment(monkeypatch):
    calls = []
    async def execute(name, args):
        calls.append((name, args))
        if name == "lookup_customer_by_document":
            return {"ok": True, "found": False}
        if len([item for item in calls if item[0] == "create_customer"]) == 1:
            return {"ok": False, "code": "customer_validation",
                    "fields": ["bairro", "cep", "cidade", "estado", "numero", "rua"]}
        return {"ok": True, "customer_id": "301"}

    async def cep_lookup(value):
        assert value == "01001000"
        return {"address": "Praça da Sé", "neighborhood": "Sé",
                "city": "São Paulo", "state": "SP", "zipcode": value}

    monkeypatch.setattr("app.checkout_data_service.lookup_address_by_zipcode", cep_lookup)
    state = CommerceConversationState()
    for message in ("quero me cadastrar", "52998224725", "João Teste da Silva", "joao@example.com"):
        result = await handle_customer_registration_turn(
            message, state=state, execute=execute, sender_phone="5511999999999")
        state = evolve_commerce_state(state, result)
    result = await handle_customer_registration_turn("confirmo o cadastro", state=state, execute=execute)
    state = evolve_commerce_state(state, result)
    assert "cep" in result.reply_text.casefold()
    result = await handle_customer_registration_turn("01001000", state=state, execute=execute)
    state = evolve_commerce_state(state, result)
    assert "número" in result.reply_text.casefold() or "numero" in result.reply_text.casefold()
    result = await handle_customer_registration_turn("123", state=state, execute=execute)
    state = evolve_commerce_state(state, result)
    assert "Confira" in result.reply_text
    result = await handle_customer_registration_turn("confirmo o cadastro", state=state, execute=execute)
    assert result.handoff_required is False
    posts = [args for name, args in calls if name == "create_customer"]
    assert len(posts) == 2
    assert posts[1]["cep"] == "01001000" and posts[1]["numero"] == "123"
    assert posts[1]["rua"] == "Praça da Sé" and posts[1]["cidade"] == "São Paulo"
