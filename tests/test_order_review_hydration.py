"""A revisao busca os fatos onde eles estao, e recusa quando nao estao.

Adicionar ao carrinho nao pode custar rede — o adaptador serializa chamadas e
dorme dois segundos depois de cada uma. Por isso o item entra com o que a
conversa ja tinha, e o preco so e resolvido UMA VEZ, na revisao.

Resolver ali tambem e mais correto: o preco que vale e o do momento em que o
pedido e montado, nao o que foi mencionado dez mensagens atras. E a consulta nao
sai para a rede — `get_product` e `check_inventory` do provider leem o indice
local, que o sync mantem atualizado.

O que estes testes protegem e a recusa. Sem preco, com estoque insuficiente ou
com condicao de pagamento nao confirmada, a revisao NAO fica pronta. Um pedido
criado sobre fato ausente aparece errado no ERP do cliente, e nao tem desfazer.
"""

from __future__ import annotations

import pytest

from app.commerce.order_review import (
    REVIEW_INCOMPLETE,
    REVIEW_READY,
    build_hydrated_order_review,
)
from app.commerce_context import CommerceCartItem, CommerceConversationState

TIPOC = {
    "id": "2", "name": "FKT-110C - Tipo C Kit Carregador USB",
    "reference": "FKT-110C", "price": 14.91, "stock": 490,
}
CABO = {"id": "1", "name": "CB702V - Cabo Usb", "reference": "CB702V", "price": 5.25, "stock": 3}


#: Sentinela: `estoque=None` e um valor de teste legitimo ("estoque
#: desconhecido"), entao "nao passei nada" precisa de outro marcador.
_PADRAO = object()


def _indice(produtos, *, stock_confirmed=True, estoque=_PADRAO):
    """Duplo do INDICE LOCAL, no formato que o provider devolve."""
    chamadas: list[tuple[str, str]] = []

    async def executar(tool, args):
        identificador = str(args.get("product_id") or "")
        chamadas.append((tool, identificador))
        alvo = next((p for p in produtos if p["id"] == identificador), None)
        if alvo is None:
            return {"ok": False, "error": "product_not_found"}
        if tool == "get_product":
            return {"ok": True, "product": dict(alvo)}
        if tool == "check_inventory":
            valor = alvo.get("stock") if estoque is _PADRAO else estoque
            return {
                "ok": True, "stock": valor, "available": None if valor is None else valor > 0,
                "stock_confirmed": stock_confirmed,
            }
        raise AssertionError(f"a revisao nao deveria chamar {tool!r}")

    executar.chamadas = chamadas
    return executar


def _estado(*itens, customer="555") -> CommerceConversationState:
    estado = CommerceConversationState(
        cart_items=[
            CommerceCartItem(product_id=p["id"], name=p.get("name"), quantity=q)
            for p, q in itens
        ]
    )
    estado.mercos_customer_id = customer
    return estado


# === A. preco vem do indice, nao da conversa ==============================


@pytest.mark.asyncio
async def test_the_review_fills_the_price_from_the_local_index():
    estado = _estado((TIPOC, 2))
    assert estado.cart_items[0].unit_price is None

    revisao = await build_hydrated_order_review(
        estado, execute=_indice([TIPOC]), payment_condition_id="247134"
    )
    assert revisao.lines[0].unit_price == pytest.approx(14.91)
    assert revisao.total == pytest.approx(29.82)
    assert revisao.status == REVIEW_READY


@pytest.mark.asyncio
async def test_the_index_price_wins_over_a_stale_conversation_price():
    """Preco citado ha dez mensagens nao e verdade superior ao indice."""
    estado = _estado((TIPOC, 1))
    estado.cart_items[0].unit_price = "99.00"

    revisao = await build_hydrated_order_review(
        estado, execute=_indice([TIPOC]), payment_condition_id="247134"
    )
    assert revisao.lines[0].unit_price == pytest.approx(14.91)


@pytest.mark.asyncio
async def test_the_name_and_reference_are_refreshed_too():
    estado = _estado((TIPOC, 1))
    estado.cart_items[0].name = "nome velho"
    revisao = await build_hydrated_order_review(
        estado, execute=_indice([TIPOC]), payment_condition_id="247134"
    )
    assert revisao.lines[0].name == TIPOC["name"]
    assert revisao.lines[0].reference == "FKT-110C"


# === B. uma hidratacao por produto ========================================


@pytest.mark.asyncio
async def test_each_product_is_looked_up_once():
    estado = _estado((TIPOC, 2), (CABO, 1), (TIPOC, 3))
    indice = _indice([TIPOC, CABO])
    await build_hydrated_order_review(estado, execute=indice, payment_condition_id="1")

    consultas = [i for t, i in indice.chamadas if t == "get_product"]
    assert sorted(set(consultas)) == sorted(consultas), "produto consultado duas vezes"
    assert set(consultas) == {"1", "2"}


@pytest.mark.asyncio
async def test_the_review_never_reaches_the_adaptor():
    """Somente `get_product` e `check_inventory`, que leem o indice local."""
    estado = _estado((TIPOC, 1))
    indice = _indice([TIPOC])
    await build_hydrated_order_review(estado, execute=indice, payment_condition_id="1")
    assert {t for t, _ in indice.chamadas} <= {"get_product", "check_inventory"}


# === C/D. estoque =========================================================


@pytest.mark.asyncio
async def test_unknown_stock_stays_unknown():
    """`None` nao vira zero: negar venda de item que existe e o pior erro."""
    estado = _estado((TIPOC, 1))
    revisao = await build_hydrated_order_review(
        estado, execute=_indice([TIPOC], estoque=None), payment_condition_id="1"
    )
    assert revisao.lines[0].stock is None
    assert revisao.status != REVIEW_READY
    assert "estoque_nao_confirmado" in revisao.unconfirmed


@pytest.mark.asyncio
async def test_stock_below_the_quantity_blocks_the_review():
    estado = _estado((CABO, 10))  # indice tem 3
    revisao = await build_hydrated_order_review(
        estado, execute=_indice([CABO]), payment_condition_id="1"
    )
    assert revisao.status != REVIEW_READY
    assert any("estoque" in m for m in revisao.missing)


@pytest.mark.asyncio
async def test_unconfirmed_stock_is_reported_not_asserted():
    estado = _estado((TIPOC, 1))
    revisao = await build_hydrated_order_review(
        estado, execute=_indice([TIPOC], stock_confirmed=False), payment_condition_id="1"
    )
    assert "estoque_nao_confirmado" in revisao.unconfirmed


# === E. preco ausente =====================================================


@pytest.mark.asyncio
async def test_a_product_without_a_price_blocks_the_review():
    sem_preco = {**TIPOC, "price": None}
    estado = _estado((sem_preco, 1))
    revisao = await build_hydrated_order_review(
        estado, execute=_indice([sem_preco]), payment_condition_id="1"
    )
    assert revisao.status != REVIEW_READY
    assert "preco_unitario" in revisao.missing


@pytest.mark.asyncio
async def test_a_product_missing_from_the_index_blocks_the_review():
    estado = _estado((TIPOC, 1))
    revisao = await build_hydrated_order_review(
        estado, execute=_indice([]), payment_condition_id="1"
    )
    assert revisao.status != REVIEW_READY


# === I/J/K. fingerprint sobre o preco resolvido ===========================


@pytest.mark.asyncio
async def test_two_identical_reviews_share_a_fingerprint():
    a = await build_hydrated_order_review(
        _estado((TIPOC, 2)), execute=_indice([TIPOC]), payment_condition_id="1"
    )
    b = await build_hydrated_order_review(
        _estado((TIPOC, 2)), execute=_indice([TIPOC]), payment_condition_id="1"
    )
    assert a.fingerprint == b.fingerprint and a.fingerprint


@pytest.mark.asyncio
async def test_a_price_change_changes_the_fingerprint():
    """O snapshot e sobre o preco RESOLVIDO, nao sobre o que estava no carrinho."""
    antes = await build_hydrated_order_review(
        _estado((TIPOC, 1)), execute=_indice([TIPOC]), payment_condition_id="1"
    )
    depois = await build_hydrated_order_review(
        _estado((TIPOC, 1)),
        execute=_indice([{**TIPOC, "price": 19.9}]),
        payment_condition_id="1",
    )
    assert antes.fingerprint != depois.fingerprint


@pytest.mark.asyncio
async def test_a_payment_condition_change_changes_the_fingerprint():
    a = await build_hydrated_order_review(
        _estado((TIPOC, 1)), execute=_indice([TIPOC]), payment_condition_id="1"
    )
    b = await build_hydrated_order_review(
        _estado((TIPOC, 1)), execute=_indice([TIPOC]), payment_condition_id="2"
    )
    assert a.fingerprint != b.fingerprint


@pytest.mark.asyncio
async def test_a_quantity_change_changes_the_fingerprint():
    a = await build_hydrated_order_review(
        _estado((TIPOC, 1)), execute=_indice([TIPOC]), payment_condition_id="1"
    )
    b = await build_hydrated_order_review(
        _estado((TIPOC, 2)), execute=_indice([TIPOC]), payment_condition_id="1"
    )
    assert a.fingerprint != b.fingerprint


# === a hidratacao acontece na REVISAO, nao na confirmacao =================


@pytest.mark.asyncio
async def test_the_review_is_what_hydrates():
    """Este teste ja afirmou o contrario — e o desenho e que estava errado.

    Resolver os fatos no "confirmo" faria o cliente aprovar uma coisa e o
    sistema buscar outra logo depois. A revisao resolve; a confirmacao so
    carimba o que foi aprovado.
    """
    from app.commerce.order_review import run_order_review
    from app.commerce.payment_conditions import PaymentConditionCache

    async def condicoes():
        return [{"id": 247134, "nome": "à vista", "excluido": False}]

    estado = _estado((TIPOC, 2))
    indice = _indice([TIPOC])
    revisao = await run_order_review(
        estado, execute=indice, cache=PaymentConditionCache(),
        fetch_conditions=condicoes,
    )

    assert revisao.status == REVIEW_READY
    assert ("get_product", "2") in indice.chamadas
    assert ("check_inventory", "2") in indice.chamadas
    assert estado.order_confirmation_status == "pending"


@pytest.mark.asyncio
async def test_insufficient_stock_keeps_the_review_from_pending():
    """O bloqueio acontece na revisao, antes de existir o que confirmar."""
    from app.commerce.order_review import run_order_review
    from app.commerce.payment_conditions import PaymentConditionCache

    async def condicoes():
        return [{"id": 247134, "nome": "à vista", "excluido": False}]

    estado = _estado((CABO, 10))  # indice tem 3
    revisao = await run_order_review(
        estado, execute=_indice([CABO]), cache=PaymentConditionCache(),
        fetch_conditions=condicoes,
    )
    assert revisao.status != REVIEW_READY
    assert estado.order_confirmation_status == "not_ready"


def test_the_builder_does_not_require_an_order_type():
    """Auditado: `order_type` nao entra no payload, entao nao ha o que escolher
    — e escolher um dos nove arbitrariamente sairia errado no ERP."""
    import inspect

    from app.commerce.mercos import order_payload

    fonte = inspect.getsource(order_payload)
    assert "tipo_pedido" not in fonte
    assert "order_type" not in fonte
