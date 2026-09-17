"""Carrinho local, revisao de pedido e o portao que separa preparar de criar.

A Mercos nao expoe carrinho no contrato que o provider consome, entao o
carrinho E a conversa: `cart_items` do estado comercial. Adicionar, remover e
mudar quantidade sao operacoes locais — nenhuma toca o adaptador, que serializa
chamadas e dorme dois segundos depois de cada uma.

O que estes testes protegem, acima de tudo, e a fronteira entre PREPARAR um
pedido e CRIA-LO. Um pedido criado por engano nao tem desfazer: ele aparece no
ERP do cliente. Por isso a revisao e versionada, qualquer mudanca no carrinho a
invalida, e a criacao depende de um gate que vem desligado por padrao.
"""

from __future__ import annotations

import pytest

from app.commerce.order_review import (
    REVIEW_CUSTOMER_REQUIRED,
    REVIEW_INCOMPLETE,
    REVIEW_READY,
    build_order_review,
    order_fingerprint,
)
from app.commerce.turn_flow import run_commerce_turn
from app.commerce.turn_resolver import (
    ACTION_ADD_TO_CART,
    ACTION_CLEAR_CART,
    ACTION_REMOVE_FROM_CART,
    ACTION_SET_QUANTITY,
    ACTION_SHOW_CART,
    resolve_commerce_turn,
)
from app.commerce_context import (
    CommerceCartItem,
    CommerceConversationState,
    CommerceProductReference,
    PresentedCommerceProduct,
)

CABO = {"id": "1", "name": "CB702V - Cabo Usb 1 Metro", "reference": "CB702V", "price": 5.25, "stock": 69}
FONE = {"id": "2", "name": "EJ-25 - Fone De Ouvido Com Fio", "reference": "EJ-25", "price": 9.6, "stock": 86}
LISTA = [CABO, FONE]


def _executor(produtos=LISTA):
    chamadas: list[tuple[str, dict]] = []

    async def executar(tool, args):
        chamadas.append((tool, dict(args)))
        if tool == "search_products":
            return {"ok": True, "products": produtos}
        alvo = next((p for p in produtos if p["id"] == str(args.get("product_id"))), None)
        if alvo is None:
            return {"ok": False, "error": "product_not_found"}
        if tool == "get_product":
            return {"ok": True, "product": alvo}
        return {"ok": True, "stock": alvo.get("stock"), "stock_confirmed": True}

    executar.chamadas = chamadas
    return executar


def _estado_com_produto(produto=CABO) -> CommerceConversationState:
    return CommerceConversationState(
        active_product=CommerceProductReference(
            product_id=produto["id"], name=produto["name"], reference=produto.get("reference")
        ),
        last_presented_products=[
            PresentedCommerceProduct(
                product_id=p["id"], name=p["name"], reference=p.get("reference"), position=i
            )
            for i, p in enumerate(LISTA, start=1)
        ],
    )


# === 1. o carrinho e local: nenhuma chamada externa =======================


@pytest.mark.parametrize(
    "texto,acao",
    [
        ("coloca no carrinho", ACTION_ADD_TO_CART),
        ("adiciona esse", ACTION_ADD_TO_CART),
        ("coloca 3", ACTION_SET_QUANTITY),
        ("adiciona mais 2", ACTION_SET_QUANTITY),
        ("tira esse", ACTION_REMOVE_FROM_CART),
        ("remove o segundo", ACTION_REMOVE_FROM_CART),
        ("como ficou?", ACTION_SHOW_CART),
        ("o que tem no meu pedido?", ACTION_SHOW_CART),
        ("limpa o carrinho", ACTION_CLEAR_CART),
    ],
)
def test_cart_phrases_are_recognized(texto, acao):
    assert resolve_commerce_turn(texto, state=_estado_com_produto()).action == acao


@pytest.mark.asyncio
async def test_cart_operations_never_call_the_adaptor():
    """O adaptador serializa e dorme 2s por chamada: carrinho nao pode usa-lo."""
    estado = _estado_com_produto()
    executar = _executor()
    for texto in ("coloca no carrinho", "coloca 3", "como ficou?", "limpa o carrinho"):
        await run_commerce_turn(texto, state=estado, execute=executar)
    assert executar.chamadas == []


# === 2. adicionar, quantidade e remocao ===================================


@pytest.mark.asyncio
async def test_adding_the_active_product_starts_at_one():
    estado = _estado_com_produto()
    await run_commerce_turn("coloca no carrinho", state=estado, execute=_executor())
    assert [(i.product_id, i.quantity) for i in estado.cart_items] == [("1", 1)]


@pytest.mark.asyncio
async def test_setting_a_quantity_replaces_it():
    estado = _estado_com_produto()
    executar = _executor()
    await run_commerce_turn("coloca no carrinho", state=estado, execute=executar)
    await run_commerce_turn("coloca 3", state=estado, execute=executar)
    assert estado.cart_items[0].quantity == 3


@pytest.mark.asyncio
async def test_adding_more_increments_instead_of_replacing():
    """"coloca 3" e "adiciona mais 2" nao sao a mesma operacao."""
    estado = _estado_com_produto()
    executar = _executor()
    await run_commerce_turn("coloca no carrinho", state=estado, execute=executar)
    await run_commerce_turn("coloca 3", state=estado, execute=executar)
    await run_commerce_turn("adiciona mais 2", state=estado, execute=executar)
    assert estado.cart_items[0].quantity == 5


@pytest.mark.asyncio
async def test_adding_a_product_from_the_list_by_position():
    estado = _estado_com_produto()
    await run_commerce_turn("adiciona o segundo", state=estado, execute=_executor())
    assert [i.product_id for i in estado.cart_items] == ["2"]


@pytest.mark.asyncio
async def test_removing_takes_the_item_out():
    estado = _estado_com_produto()
    executar = _executor()
    await run_commerce_turn("coloca no carrinho", state=estado, execute=executar)
    await run_commerce_turn("tira esse", state=estado, execute=executar)
    assert estado.cart_items == []


@pytest.mark.asyncio
async def test_clearing_empties_the_cart():
    estado = _estado_com_produto()
    executar = _executor()
    await run_commerce_turn("coloca no carrinho", state=estado, execute=executar)
    await run_commerce_turn("limpa o carrinho", state=estado, execute=executar)
    assert estado.cart_items == []


@pytest.mark.asyncio
async def test_quantity_never_drops_below_one():
    estado = _estado_com_produto()
    executar = _executor()
    await run_commerce_turn("coloca no carrinho", state=estado, execute=executar)
    await run_commerce_turn("coloca 0", state=estado, execute=executar)
    assert not estado.cart_items or estado.cart_items[0].quantity >= 1


# === 3. revisao determinista ==============================================


def _carrinho(*itens) -> list[CommerceCartItem]:
    return [
        CommerceCartItem(
            product_id=p["id"], name=p["name"], quantity=q, unit_price=str(p["price"])
        )
        for p, q in itens
    ]


def test_a_review_without_a_customer_says_exactly_that():
    estado = CommerceConversationState(cart_items=_carrinho((CABO, 2)))
    revisao = build_order_review(estado, payment_condition_id="247134")
    assert revisao.status == REVIEW_CUSTOMER_REQUIRED
    assert "customer_id" in revisao.missing


def test_a_review_without_a_payment_condition_is_incomplete():
    estado = CommerceConversationState(cart_items=_carrinho((CABO, 2)))
    estado.mercos_customer_id = "555"
    revisao = build_order_review(estado, payment_condition_id=None)
    assert revisao.status == REVIEW_INCOMPLETE
    assert "payment_condition" in revisao.missing


def test_an_empty_cart_is_never_ready():
    estado = CommerceConversationState()
    estado.mercos_customer_id = "555"
    revisao = build_order_review(estado, payment_condition_id="247134")
    assert revisao.status != REVIEW_READY


def test_a_complete_review_is_ready_and_totals_up():
    estado = CommerceConversationState(cart_items=_carrinho((CABO, 2), (FONE, 1)))
    estado.mercos_customer_id = "555"
    revisao = build_order_review(estado, payment_condition_id="247134")
    assert revisao.status == REVIEW_READY
    assert revisao.total == pytest.approx(5.25 * 2 + 9.6)
    assert len(revisao.lines) == 2


def test_a_missing_price_keeps_the_review_from_being_ready():
    estado = CommerceConversationState(
        cart_items=[CommerceCartItem(product_id="1", name="X", quantity=1)]
    )
    estado.mercos_customer_id = "555"
    revisao = build_order_review(estado, payment_condition_id="247134")
    assert revisao.status != REVIEW_READY
    assert any("preco" in m or "price" in m for m in revisao.missing)


# === 4. fingerprint =======================================================


def test_the_same_order_yields_the_same_fingerprint():
    estado = CommerceConversationState(cart_items=_carrinho((CABO, 2), (FONE, 1)))
    estado.mercos_customer_id = "555"
    a = order_fingerprint(estado, payment_condition_id="247134")
    b = order_fingerprint(estado, payment_condition_id="247134")
    assert a == b and a


def test_item_order_does_not_change_the_fingerprint():
    """Ordenacao deterministica: o mesmo pedido e o mesmo pedido."""
    um = CommerceConversationState(cart_items=_carrinho((CABO, 2), (FONE, 1)))
    outro = CommerceConversationState(cart_items=_carrinho((FONE, 1), (CABO, 2)))
    um.mercos_customer_id = outro.mercos_customer_id = "555"
    assert order_fingerprint(um, payment_condition_id="1") == order_fingerprint(
        outro, payment_condition_id="1"
    )


def test_changing_a_quantity_changes_the_fingerprint():
    antes = CommerceConversationState(cart_items=_carrinho((CABO, 2)))
    depois = CommerceConversationState(cart_items=_carrinho((CABO, 3)))
    antes.mercos_customer_id = depois.mercos_customer_id = "555"
    assert order_fingerprint(antes, payment_condition_id="1") != order_fingerprint(
        depois, payment_condition_id="1"
    )


def test_the_fingerprint_carries_no_timestamp():
    """Com produto dentro, o mesmo pedido teria digital nova a cada segundo —
    e a idempotencia que ela existe para sustentar iria junto."""
    import inspect

    from app.commerce import order_review

    fonte = inspect.getsource(order_review.order_fingerprint)
    for proibido in ("now(", "utcnow", "time()", "timestamp("):
        assert proibido not in fonte


# === 5. mudanca no carrinho invalida a revisao ============================


@pytest.mark.asyncio
async def test_touching_the_cart_after_a_review_invalidates_it():
    """Confirmar uma revisao que nao corresponde mais ao carrinho cria o pedido
    errado — e pedido criado nao tem desfazer."""
    estado = _estado_com_produto()
    executar = _executor()
    await run_commerce_turn("coloca no carrinho", state=estado, execute=executar)
    estado.mercos_customer_id = "555"
    estado.order_review_version = "v1"
    estado.order_confirmation_status = "pending"

    await run_commerce_turn("adiciona mais 2", state=estado, execute=executar)
    assert estado.order_confirmation_status == "not_ready"
    assert estado.order_review_version != "v1"


# === 6. o portao da mutation ==============================================


def test_order_mutations_are_disabled_by_default():
    from app.config import Settings

    definicoes = Settings.model_fields
    assert definicoes["mercos_order_mutations_enabled"].default is False
    assert definicoes["mercos_customer_mutations_enabled"].default is False


@pytest.mark.asyncio
async def test_creating_an_order_is_refused_while_the_gate_is_closed():
    import httpx

    from app.commerce.mercos.client import MercosAdaptorClient, MercosMutationDisabled

    chamou = []

    def handler(request):
        chamou.append(str(request.url))
        return httpx.Response(200, json={"id": 1})

    cliente = MercosAdaptorClient(
        base_url="https://a.example.com", api_key="k",
        transport=httpx.MockTransport(handler), mutations_enabled=False,
    )
    with pytest.raises(MercosMutationDisabled):
        await cliente.create_order({"cliente_id": "1"})
    assert chamou == [], "POST saiu com o portao fechado"


@pytest.mark.asyncio
async def test_creating_a_customer_is_refused_while_the_gate_is_closed():
    import httpx

    from app.commerce.mercos.client import MercosAdaptorClient, MercosMutationDisabled

    chamou = []
    cliente = MercosAdaptorClient(
        base_url="https://a.example.com", api_key="k",
        transport=httpx.MockTransport(lambda r: chamou.append(1) or httpx.Response(200, json={})),
        customer_mutations_enabled=False,
    )
    with pytest.raises(MercosMutationDisabled):
        await cliente.create_customer({"razao_social": "X"})
    assert chamou == []


@pytest.mark.asyncio
async def test_with_the_gate_open_the_post_reaches_the_adaptor_route():
    import httpx

    from app.commerce.mercos.client import MercosAdaptorClient

    vistos: list[tuple[str, str]] = []

    def handler(request):
        vistos.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": 77, "numero": 12})

    cliente = MercosAdaptorClient(
        base_url="https://a.example.com", api_key="k",
        transport=httpx.MockTransport(handler), mutations_enabled=True,
    )
    resultado = await cliente.create_order({"cliente_id": "1", "itens": []})
    assert vistos == [("POST", "/v1/orders")]
    assert resultado["id"] == 77


# === 7. confirmacao so com revisao pendente ===============================


@pytest.mark.asyncio
async def test_confirming_without_a_pending_review_creates_nothing():
    from app.commerce.turn_flow import OUTCOME_PRODUCT_NOT_FOUND

    estado = CommerceConversationState()
    executar = _executor()
    resultado = await run_commerce_turn("confirmo", state=estado, execute=executar)
    assert estado.order_confirmation_status == "not_ready"
    assert resultado.outcome != OUTCOME_PRODUCT_NOT_FOUND
    assert "create_order" not in [t for t, _ in executar.chamadas]


@pytest.mark.asyncio
async def test_confirming_a_pending_review_stops_at_ready_for_submission():
    """Nesta fase o fluxo prepara o pedido e para: o portao segue fechado."""
    from app.commerce.turn_flow import OUTCOME_ORDER_READY

    estado = _estado_com_produto()
    executar = _executor()
    await run_commerce_turn("coloca no carrinho", state=estado, execute=executar)
    # O item precisa de preco: revisao sem preco e incompleta de proposito, e
    # este teste e sobre o PORTAO, nao sobre precificacao.
    estado.cart_items[0].unit_price = "5.25"
    estado.mercos_customer_id = "555"
    # Condicao de pagamento tambem e obrigatoria: sem ela a revisao fica
    # incompleta, e e isso que o teste do caso incompleto cobre.
    estado.selected_payment_option_id = "247134"
    estado.pending_commerce_action = "confirm_order"
    estado.order_confirmation_status = "pending"

    resultado = await run_commerce_turn("confirmo", state=estado, execute=executar)
    assert resultado.outcome == OUTCOME_ORDER_READY
    assert "create_order" not in [t for t, _ in executar.chamadas]


# === 8. o id do catalogo E o id Mercos ====================================


def test_the_catalog_product_id_is_the_mercos_product_id():
    """Prova por codigo: o indice guarda `id` do payload Mercos, sem traducao.

    Se fossem numeracoes diferentes, o POST criaria pedido de outro produto — e
    o erro so apareceria no ERP do cliente.
    """
    from app.commerce.mercos.normalizer import normalize_product

    bruto = {"id": 261436207, "nome": "X", "ativo": True, "excluido": False}
    produto = normalize_product(bruto)
    assert produto.external_id == "261436207"
    assert produto.for_model()["id"] == "261436207"
