"""Regressions for the eight repeated clarifications in the 120-turn audit."""

import pytest

from tests.test_chatbo_pipeline_replay import Replay


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix,last,expected_action", [
    (["tem relógio masculino?", "não esse"], "me mostra outro", "select_product"),
    (["tem relógio preto?", "quero esse", "coloca no carrinho", "quero dois", "remove um"], "quero pagar", "transaction"),
    (["tem relógio preto?", "quero esse", "coloca no carrinho", "quero dois", "remove um", "quero pagar"], "como faço o pagamento?", "transaction"),
    (["tem relógio preto?", "quero esse", "coloca no carrinho", "quero dois", "remove um", "quero pagar", "como faço o pagamento?"], "qual condição de pagamento?", "transaction"),
    (["tem relógio preto?", "quero esse", "coloca no carrinho", "quero dois", "remove um", "quero pagar", "como faço o pagamento?", "qual condição de pagamento?"], "não quero pagar agora", "transaction"),
    (["não quero cadastrar", "tem relógio?", "não quero esse produto", "não quero pagar agora"], "sim", "select_product"),
    (["não quero cadastrar", "tem relógio?", "não quero esse produto", "não quero pagar agora", "sim"], "não", "reject_product"),
    (["não quero cadastrar", "tem relógio?", "não quero esse produto", "não quero pagar agora", "sim", "não"], "esse", "select_product"),
])
async def test_original_repeated_clarification_has_a_specific_consumer(monkeypatch, prefix, last, expected_action):
    replay = Replay(monkeypatch)
    for message in prefix:
        await replay.say(message)
    previous = replay.rows[-1]
    _, row = await replay.say(last)
    assert row["resolver_action"] == expected_action
    assert row["reply"] != previous["reply"]
    assert row["reply"] != "Qual característica ou preferência é mais importante para você?"


@pytest.mark.asyncio
async def test_cart_quantity_and_checkout_use_existing_product_and_preserve_cart(monkeypatch):
    replay = Replay(monkeypatch)
    for message in ("tem relógio preto?", "coloca no carrinho", "quero dois", "remove um"):
        await replay.say(message)
    state = replay.states["wa:5511988880001"]
    assert [(item["product_id"], item["quantity"]) for item in state["cart_items"]] == [("watch-1", 1)]
    assert replay.rows[1]["tools"] == []
    _, checkout = await replay.say("quero pagar")
    assert checkout["active_topic"] == "checkout"
    assert checkout["cart_before"] == [("watch-1", 1)]
    assert "característica ou preferência" not in checkout["reply"]


@pytest.mark.asyncio
async def test_order_status_without_reference_asks_for_order_number(monkeypatch):
    replay = Replay(monkeypatch)
    _, first = await replay.say("já fiz meu pedido")
    assert first["intent"] == "commerce"
    assert "número do pedido" in first["reply"].casefold()
    _, followup = await replay.say("qual status?")
    assert followup["intent"] == "commerce"
    assert followup["safety_reason"] != "scope_refusal"


@pytest.mark.asyncio
async def test_ambiguous_short_reference_names_the_displayed_choices(monkeypatch):
    replay = Replay(monkeypatch)
    await replay.say("tem relógio?")
    _, row = await replay.say("esse")
    assert row["presented_before"] == ["watch-1", "watch-2"]
    assert "primeiro" in row["reply"].casefold() and "segundo" in row["reply"].casefold()
    assert row["tools"] == []


@pytest.mark.asyncio
async def test_quantity_word_in_product_request_does_not_turn_into_cart_action(monkeypatch):
    replay = Replay(monkeypatch)
    _, row = await replay.say("quero um relógio preto")
    assert row["resolver_action"] == "search_product"
    assert "search_products" in row["tools"]
    assert row["cart_before"] == []


@pytest.mark.asyncio
async def test_short_position_and_cheapest_use_last_presented_products(monkeypatch):
    replay = Replay(monkeypatch)
    await replay.say("tem relógio?")
    _, second = await replay.say("e aquele segundo?")
    assert second["active_product_before"] is None
    assert second["presented_before"] == ["watch-1", "watch-2"]
    assert "get_product" in second["tools"]
    assert replay.states["wa:5511988880001"]["active_product"]["product_id"] == "watch-2"
    _, cheapest = await replay.say("o mais barato")
    assert cheapest["safety_reason"] is None
    assert replay.states["wa:5511988880001"]["active_product"]["product_id"] == "watch-1"


@pytest.mark.asyncio
async def test_rejecting_a_selected_product_offers_the_other_presented_option(monkeypatch):
    replay = Replay(monkeypatch)
    await replay.say("tem relógio?")
    await replay.say("tem preto?")
    assert replay.states["wa:5511988880001"]["active_product"]["product_id"] == "watch-1"
    _, rejected = await replay.say("não esse")
    assert rejected["reply"] != "Qual característica ou preferência é mais importante para você?"
    assert "Relógio Prata Clássico" in rejected["reply"]
    assert replay.states["wa:5511988880001"]["active_product"]["product_id"] == "watch-2"
    _, switched_back = await replay.say("me mostra outro")
    assert switched_back["reply"] != rejected["reply"]
    assert "Relógio Preto Esportivo" in switched_back["reply"]
    assert replay.states["wa:5511988880001"]["active_product"]["product_id"] == "watch-1"


@pytest.mark.asyncio
async def test_checkout_review_without_payment_condition_asks_for_it_specifically(monkeypatch):
    replay = Replay(monkeypatch)
    for message in ("tem relógio preto?", "coloca no carrinho", "quero me cadastrar",
                    "52998224725", "João Teste da Silva", "joao@example.com",
                    "confirmo o cadastro"):
        await replay.say(message)
    _, review = await replay.say("vamos fechar")
    assert review["reply"] != "Qual característica ou preferência é mais importante para você?"
    assert "condição de pagamento" in review["reply"].casefold()
    assert review["intent"] == "commerce"
    _, cart = await replay.say("meu carrinho")
    assert cart["cart_before"] == [("watch-1", 1)]
