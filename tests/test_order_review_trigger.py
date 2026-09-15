"""Quando o cliente pediu uma revisao — e quando ele so quis olhar o carrinho.

A distincao parece sutil e nao e. Olhar o carrinho e uma pergunta barata e sem
consequencia. Pedir para fechar dispara a resolucao inteira: preco, estoque,
condicao de pagamento, digital, e o pedido passa a estar pendente de
confirmacao. Confundir os dois faria "como ficou?" consultar condicoes
comerciais e deixar um pedido pronto para ser criado sem ninguem ter pedido isso.

O outro lado do par tambem importa: "pode fechar" nao e confirmacao. E o pedido
de VER a revisao. So depois de existir revisao pendente e que "confirmo" tem o
que confirmar — e nesse ponto ele nao consulta mais nada.
"""

from __future__ import annotations

import pytest

from app.commerce.turn_resolver import (
    ACTION_CONFIRM_ORDER,
    ACTION_REVIEW_ORDER,
    ACTION_SHOW_CART,
    ACTION_START_PURCHASE,
    resolve_commerce_turn,
)
from app.commerce_context import CommerceCartItem, CommerceConversationState

TIPOC = {
    "id": "2", "name": "FKT-110C - Tipo C Kit Carregador USB",
    "reference": "FKT-110C", "price": 14.91, "stock": 490,
}
ATIVA = {"id": 247134, "nome": "à vista por transferência ou depósito", "excluido": False}


def _indice(produtos=(TIPOC,)):
    chamadas: list[tuple[str, str]] = []

    async def executar(tool, args):
        pid = str(args.get("product_id") or "")
        chamadas.append((tool, pid))
        alvo = next((p for p in produtos if p["id"] == pid), None)
        if alvo is None:
            return {"ok": False, "error": "product_not_found"}
        if tool == "get_product":
            return {"ok": True, "product": dict(alvo)}
        return {"ok": True, "stock": alvo["stock"], "stock_confirmed": True}

    executar.chamadas = chamadas
    return executar


def _com_carrinho(quantidade=2, *, customer="555") -> CommerceConversationState:
    estado = CommerceConversationState(
        cart_items=[CommerceCartItem(product_id="2", name="x", quantity=quantidade)]
    )
    estado.mercos_customer_id = customer
    return estado


async def _condicoes():
    return [ATIVA]


# === 1/12. olhar o carrinho nao e pedir revisao ===========================


@pytest.mark.parametrize(
    "texto",
    ["como ficou?", "o que tem no carrinho?", "me mostra meu pedido",
     "o que eu escolhi?", "o que tem no meu pedido?"],
)
def test_looking_at_the_cart_is_not_a_review(texto):
    assert resolve_commerce_turn(texto, state=_com_carrinho()).action == ACTION_SHOW_CART


@pytest.mark.asyncio
async def test_showing_the_cart_resolves_nothing_and_changes_nothing():
    from app.commerce.turn_flow import run_commerce_turn

    estado = _com_carrinho()
    antes = estado.order_confirmation_status
    indice = _indice()
    await run_commerce_turn("como ficou?", state=estado, execute=indice)

    assert indice.chamadas == [], "olhar o carrinho consultou o indice"
    assert estado.order_confirmation_status == antes
    assert estado.order_review_version is None
    assert estado.mercos_payment_condition_id is None


# === 2/3/4. pedir para fechar dispara a revisao ===========================


@pytest.mark.parametrize(
    "texto",
    ["pode fechar", "fecha o pedido", "fechar pedido", "quero fechar",
     "quero finalizar", "finaliza o pedido", "finalizar pedido",
     "vamos finalizar", "vamos fechar", "pode concluir", "quero concluir",
     "concluir pedido", "revisa o pedido", "revisar pedido",
     "quero revisar antes", "prosseguir com o pedido"],
)
def test_closing_phrases_ask_for_a_review(texto):
    assert resolve_commerce_turn(texto, state=_com_carrinho()).action == ACTION_REVIEW_ORDER


@pytest.mark.asyncio
async def test_the_review_turn_calls_the_existing_review_function():
    """O turn flow nao reimplementa regra nenhuma: ele chama a funcao pronta."""
    from app.commerce.turn_flow import OUTCOME_ORDER_REVIEW, run_commerce_turn

    estado = _com_carrinho()
    indice = _indice()
    resultado = await run_commerce_turn(
        "pode fechar", state=estado, execute=indice, fetch_conditions=_condicoes
    )

    assert resultado.outcome == OUTCOME_ORDER_REVIEW
    assert ("get_product", "2") in indice.chamadas
    assert estado.order_confirmation_status == "pending"
    assert estado.order_review_version
    assert estado.mercos_payment_condition_id == "247134"


# === 6/7. revisao incompleta diz o motivo ================================


@pytest.mark.asyncio
async def test_a_review_without_a_customer_reports_that_reason():
    from app.commerce.turn_flow import OUTCOME_ORDER_INCOMPLETE, run_commerce_turn

    estado = _com_carrinho(customer=None)
    resultado = await run_commerce_turn(
        "pode fechar", state=estado, execute=_indice(), fetch_conditions=_condicoes
    )
    assert resultado.outcome == OUTCOME_ORDER_INCOMPLETE
    assert "customer_id" in (resultado.missing or [])
    assert estado.order_confirmation_status == "not_ready"


@pytest.mark.asyncio
async def test_a_review_with_insufficient_stock_reports_that_reason():
    from app.commerce.turn_flow import OUTCOME_ORDER_INCOMPLETE, run_commerce_turn

    poucos = {**TIPOC, "stock": 1}
    estado = _com_carrinho(quantidade=10)
    resultado = await run_commerce_turn(
        "pode fechar", state=estado, execute=_indice([poucos]),
        fetch_conditions=_condicoes,
    )
    assert resultado.outcome == OUTCOME_ORDER_INCOMPLETE
    assert any("estoque" in m for m in (resultado.missing or []))
    assert estado.order_confirmation_status == "not_ready"


# === 8. confirmar nao refaz nada ==========================================


@pytest.mark.asyncio
async def test_confirming_after_a_review_touches_nothing():
    from app.commerce.turn_flow import OUTCOME_ORDER_READY, run_commerce_turn

    estado = _com_carrinho()
    await run_commerce_turn(
        "pode fechar", state=estado, execute=_indice(), fetch_conditions=_condicoes
    )
    versao = estado.order_review_version
    estado.pending_commerce_action = "confirm_order"

    async def proibido(tool, args):
        raise AssertionError(f"a confirmacao nao pode consultar {tool!r}")

    resultado = await run_commerce_turn("confirmo", state=estado, execute=proibido)
    assert resultado.outcome == OUTCOME_ORDER_READY
    assert estado.confirmed_order_review_version == versao


def test_confirm_only_wins_over_review_when_a_review_is_pending():
    """Sem revisao pendente, "pode fechar" pede revisao; com ela, confirma."""
    sem = _com_carrinho()
    assert resolve_commerce_turn("pode fechar", state=sem).action == ACTION_REVIEW_ORDER

    com = _com_carrinho()
    com.order_confirmation_status = "pending"
    # Pendencia SEM versao nao e revisao real: e um estado pela metade, e nao
    # deve autorizar confirmacao nenhuma.
    com.order_review_version = "v1"
    com.pending_commerce_action = "confirm_order"
    assert resolve_commerce_turn("pode fechar", state=com).action == ACTION_CONFIRM_ORDER


@pytest.mark.parametrize("texto", ["confirmo", "pode criar", "sim, pode fechar"])
def test_explicit_confirmations_are_always_confirmations(texto):
    estado = _com_carrinho()
    estado.order_confirmation_status = "pending"
    estado.pending_commerce_action = "confirm_order"
    assert resolve_commerce_turn(texto, state=estado).action == ACTION_CONFIRM_ORDER


# === 9/10. mudanca invalida a revisao pendente ============================


@pytest.mark.asyncio
async def test_changing_the_cart_invalidates_a_pending_review():
    from app.commerce.turn_flow import run_commerce_turn

    estado = CommerceConversationState(
        cart_items=[CommerceCartItem(product_id="2", name="x", quantity=2)]
    )
    estado.mercos_customer_id = "555"
    estado.active_product = None
    await run_commerce_turn(
        "pode fechar", state=estado, execute=_indice(), fetch_conditions=_condicoes
    )
    antiga = estado.order_review_version
    assert estado.order_confirmation_status == "pending"

    from app.commerce_context import CommerceProductReference

    estado.active_product = CommerceProductReference(product_id="2", name="x")
    await run_commerce_turn("coloca 5", state=estado, execute=_indice())

    assert estado.order_confirmation_status == "not_ready"
    assert estado.order_review_version != antiga


@pytest.mark.asyncio
async def test_a_new_review_after_the_change_produces_a_new_version():
    from app.commerce.turn_flow import run_commerce_turn
    from app.commerce_context import CommerceProductReference

    estado = _com_carrinho()
    estado.active_product = CommerceProductReference(product_id="2", name="x")
    await run_commerce_turn(
        "pode fechar", state=estado, execute=_indice(), fetch_conditions=_condicoes
    )
    antiga = estado.order_review_version

    await run_commerce_turn("coloca 5", state=estado, execute=_indice())
    await run_commerce_turn(
        "pode fechar", state=estado, execute=_indice(), fetch_conditions=_condicoes
    )

    assert estado.order_review_version not in (None, antiga)
    assert estado.order_confirmation_status == "pending"


# === 11. comprar de carrinho vazio nao revisa =============================


def test_starting_a_purchase_with_an_empty_cart_is_not_a_review():
    estado = CommerceConversationState()
    resolucao = resolve_commerce_turn("quero fazer um pedido", state=estado)
    assert resolucao.action == ACTION_START_PURCHASE
    assert resolucao.requires_catalog_search is False


@pytest.mark.asyncio
async def test_starting_a_purchase_does_not_run_a_review():
    from app.commerce.turn_flow import run_commerce_turn

    estado = CommerceConversationState()
    indice = _indice()
    await run_commerce_turn("quero fazer um pedido", state=estado, execute=indice)
    assert indice.chamadas == []
    assert estado.order_review_version is None
