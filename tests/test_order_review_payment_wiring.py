"""A condicao de pagamento entra na REVISAO, nunca na confirmacao.

A ordem importa mais do que parece. Resolver a condicao no "confirmo" faria o
cliente autorizar um pedido e o sistema ir buscar, depois disso, um dado
comercial que pode ter mudado — e o prazo que ele viu na revisao nao seria
necessariamente o prazo do pedido criado.

Por isso a revisao resolve tudo (produto, estoque, condicao), calcula a digital
e so entao fica pendente. A confirmacao nao consulta nada: ela apenas diz "sim"
para aquela revisao especifica. E se qualquer coisa mudar no meio, a digital
muda e a confirmacao anterior deixa de servir.
"""

from __future__ import annotations

import pytest

from app.commerce.order_review import REVIEW_READY, run_order_review
from app.commerce.payment_conditions import (
    PAYMENT_CONDITION_CACHE_TTL_SECONDS,
    PAYMENT_CONDITION_REQUIRED,
    PAYMENT_CONDITION_SELECTION_REQUIRED,
    PAYMENT_CONDITION_UNAVAILABLE,
    PaymentConditionCache,
)
from app.commerce_context import CommerceCartItem, CommerceConversationState

TIPOC = {
    "id": "2", "name": "FKT-110C - Tipo C Kit Carregador USB",
    "reference": "FKT-110C", "price": 14.91, "stock": 490,
}
ATIVA = {"id": 247134, "nome": "à vista por transferência ou depósito", "excluido": False}
OUTRA = {"id": 300000, "nome": "30 dias", "excluido": False}
EXCLUIDA = {"id": 221254, "nome": "7 dias", "excluido": True}


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


def _explode(tool, args):
    raise AssertionError(f"a confirmacao nao pode consultar nada (pediu {tool!r})")


async def _executor_proibido(tool, args):
    _explode(tool, args)


def _fonte(linhas, *, falhas=0):
    estado = {"chamadas": 0, "restantes": falhas}

    async def ler():
        estado["chamadas"] += 1
        if estado["restantes"] > 0:
            estado["restantes"] -= 1
            raise RuntimeError("adaptador indisponivel")
        return list(linhas)

    ler.estado = estado
    return ler


def _estado(quantidade=2) -> CommerceConversationState:
    estado = CommerceConversationState(
        cart_items=[CommerceCartItem(product_id="2", name="x", quantity=quantidade)]
    )
    estado.mercos_customer_id = "555"
    return estado


# === 0. a estrutura existente nao serve — provado, nao suposto ============


def test_the_existing_payment_option_model_is_about_a_different_thing():
    """`SelectedPaymentOption` e FORMA de pagamento; a fonte entrega CONDICAO.

    Forcar a condicao comercial ali deixaria `method` (pix/cartao/boleto) sem
    sentido e sugeriria parcelas e descontos que a fonte nunca informou.
    """
    from app.commerce_context import SelectedPaymentOption

    campos = set(SelectedPaymentOption.model_fields)
    assert {"method", "installments", "discount_value", "tax_value"} <= campos

    # O que a fonte realmente entrega numa condicao, e que nao cabe ali.
    assert "valor_minimo" not in campos
    assert "disponivel_b2b" not in campos


def test_the_condition_lives_in_its_own_named_fields():
    estado = CommerceConversationState()
    assert estado.mercos_payment_condition_id is None
    assert estado.mercos_payment_condition_name is None


# === 1/2. uma ativa: selecionada ANTES da digital =========================


@pytest.mark.asyncio
async def test_a_single_active_condition_is_selected_during_the_review():
    estado = _estado()
    revisao = await run_order_review(
        estado, execute=_indice(), cache=PaymentConditionCache(),
        fetch_conditions=_fonte([EXCLUIDA, ATIVA]),
    )
    assert revisao.status == REVIEW_READY
    assert estado.mercos_payment_condition_id == "247134"
    assert estado.order_confirmation_status == "pending"
    assert estado.order_review_version == revisao.fingerprint


@pytest.mark.asyncio
async def test_the_fingerprint_carries_the_condition_identity():
    com_uma = await run_order_review(
        _estado(), execute=_indice(), cache=PaymentConditionCache(),
        fetch_conditions=_fonte([ATIVA]),
    )
    com_outra = await run_order_review(
        _estado(), execute=_indice(), cache=PaymentConditionCache(),
        fetch_conditions=_fonte([OUTRA]),
    )
    assert com_uma.fingerprint != com_outra.fingerprint


# === 3/4. zero e multiplas ================================================


@pytest.mark.asyncio
async def test_no_active_condition_keeps_the_review_from_pending():
    estado = _estado()
    revisao = await run_order_review(
        estado, execute=_indice(), cache=PaymentConditionCache(),
        fetch_conditions=_fonte([EXCLUIDA]),
    )
    assert revisao.status != REVIEW_READY
    assert PAYMENT_CONDITION_REQUIRED in revisao.missing
    assert estado.order_confirmation_status == "not_ready"


@pytest.mark.asyncio
async def test_several_active_conditions_require_a_choice():
    estado = _estado()
    revisao = await run_order_review(
        estado, execute=_indice(), cache=PaymentConditionCache(),
        fetch_conditions=_fonte([ATIVA, OUTRA]),
    )
    assert PAYMENT_CONDITION_SELECTION_REQUIRED in revisao.missing
    assert estado.mercos_payment_condition_id is None, "escolheu por conta propria"
    assert {c["id"] for c in revisao.payment_condition_options} == {247134, 300000}
    assert estado.order_confirmation_status == "not_ready"


# === 5/6/7. cache =========================================================


@pytest.mark.asyncio
async def test_a_second_review_inside_the_ttl_does_not_refetch():
    cache = PaymentConditionCache()
    fonte = _fonte([ATIVA])
    await run_order_review(_estado(), execute=_indice(), cache=cache, fetch_conditions=fonte)
    await run_order_review(_estado(), execute=_indice(), cache=cache, fetch_conditions=fonte)
    assert fonte.estado["chamadas"] == 1


@pytest.mark.asyncio
async def test_an_expired_cache_refetches_once():
    cache = PaymentConditionCache()
    fonte = _fonte([ATIVA])
    await run_order_review(
        _estado(), execute=_indice(), cache=cache, fetch_conditions=fonte, now=0.0
    )
    await run_order_review(
        _estado(), execute=_indice(), cache=cache, fetch_conditions=fonte,
        now=PAYMENT_CONDITION_CACHE_TTL_SECONDS + 1,
    )
    assert fonte.estado["chamadas"] == 2


@pytest.mark.asyncio
async def test_a_failed_refresh_blocks_instead_of_reusing_the_stale_value():
    cache = PaymentConditionCache()
    await run_order_review(
        _estado(), execute=_indice(), cache=cache, fetch_conditions=_fonte([ATIVA]), now=0.0
    )
    estado = _estado()
    revisao = await run_order_review(
        estado, execute=_indice(), cache=cache,
        fetch_conditions=_fonte([ATIVA], falhas=1),
        now=PAYMENT_CONDITION_CACHE_TTL_SECONDS + 1,
    )
    assert PAYMENT_CONDITION_UNAVAILABLE in revisao.missing
    assert revisao.status != REVIEW_READY
    assert estado.order_confirmation_status == "not_ready"


# === 8. confirmar nao consulta nada =======================================


@pytest.mark.asyncio
async def test_confirming_a_ready_review_touches_nothing():
    """A confirmacao diz "sim" para uma revisao; ela nao refaz a revisao."""
    from app.commerce.turn_flow import OUTCOME_ORDER_READY, run_commerce_turn

    estado = _estado()
    await run_order_review(
        estado, execute=_indice(), cache=PaymentConditionCache(),
        fetch_conditions=_fonte([ATIVA]),
    )
    assert estado.order_confirmation_status == "pending"
    estado.pending_commerce_action = "confirm_order"

    resultado = await run_commerce_turn("confirmo", state=estado, execute=_executor_proibido)
    assert resultado.outcome == OUTCOME_ORDER_READY
    assert estado.confirmed_order_review_version == estado.order_review_version


# === 9/10. mudanca de condicao invalida a confirmacao =====================


@pytest.mark.asyncio
async def test_changing_the_condition_changes_the_fingerprint():
    estado = _estado()
    primeira = await run_order_review(
        estado, execute=_indice(), cache=PaymentConditionCache(),
        fetch_conditions=_fonte([ATIVA]),
    )
    segunda = await run_order_review(
        estado, execute=_indice(), cache=PaymentConditionCache(),
        fetch_conditions=_fonte([OUTRA]),
    )
    assert primeira.fingerprint != segunda.fingerprint
    assert estado.order_review_version == segunda.fingerprint


@pytest.mark.asyncio
async def test_an_older_confirmation_stops_being_valid():
    """Confirmar a revisao antiga depois da condicao mudar criaria o pedido com
    prazo que o cliente nao aprovou."""
    from app.commerce.turn_flow import OUTCOME_ORDER_READY, run_commerce_turn

    estado = _estado()
    await run_order_review(
        estado, execute=_indice(), cache=PaymentConditionCache(),
        fetch_conditions=_fonte([ATIVA]),
    )
    versao_antiga = estado.order_review_version
    estado.confirmed_order_review_version = versao_antiga

    await run_order_review(
        estado, execute=_indice(), cache=PaymentConditionCache(),
        fetch_conditions=_fonte([OUTRA]),
    )
    assert estado.order_review_version != versao_antiga
    assert estado.confirmed_order_review_version != estado.order_review_version

    estado.pending_commerce_action = "confirm_order"
    resultado = await run_commerce_turn("confirmo", state=estado, execute=_executor_proibido)
    assert resultado.outcome == OUTCOME_ORDER_READY
    # A confirmacao vale para a revisao ATUAL, nunca para a que ficou para tras.
    assert estado.confirmed_order_review_version == estado.order_review_version
