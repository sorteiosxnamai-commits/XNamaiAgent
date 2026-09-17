"""Casos reais de producao em que o roteamento comercial ainda errava.

Todos tem a mesma raiz: o turno decidia SOBRE QUAL PRODUTO antes de decidir O
QUE o cliente queria fazer. Quando a mensagem nao era sobre um produto, a
palavra operacional virava nome de produto e a busca falhava.

    "quero fazer um pedido"   ->  "Nao encontrei esse produto no catalogo"
    "sim" (apos o bot propor) ->  "Nao encontrei esse produto no catalogo"
    "qual valor d AD-190 ..." ->  "A fonte oficial nao informou um link"

O terceiro e o mais revelador: a pergunta era de PRECO e foi parar num ramo de
link de produto. Fato comercial simples precisa ser decidido antes do fluxo
legado, nao depois.
"""

from __future__ import annotations

import pytest

from app.commerce.turn_resolver import (
    ACTION_BROWSE_CATALOG,
    ACTION_CONFIRM_PENDING,
    ACTION_GET_DETAILS,
    ACTION_GET_PRICE,
    ACTION_REJECT_PENDING,
    ACTION_START_PURCHASE,
    PENDING_BROWSE,
    REF_LIST_POSITION,
    product_query_for,
    resolve_commerce_turn,
)
from app.commerce.turn_flow import (
    OUTCOME_BROWSE,
    OUTCOME_PRICE,
    OUTCOME_PRODUCT_NOT_FOUND,
    OUTCOME_PURCHASE_INTENT,
    run_commerce_turn,
)
from app.commerce_context import CommerceConversationState, PresentedCommerceProduct

AD190 = {
    "id": "90", "reference": "AD-190",
    "name": "AD-190 - Caixa de Som Rádio Retrô Bluetooth 5w Recarregável Portátil AM FM SM Altomex",
    "price": 89.9, "stock": 12,
}
CA31 = {
    "id": "31", "reference": "CA31-2",
    "name": "CA31-2 - Iphone Kit Carregador Lightning PD 30W",
    "price": 21.5, "stock": 40,
}
FKT = {"id": "2", "reference": "FKT-110C", "name": "FKT-110C - Tipo C Kit Carregador USB", "price": 14.91, "stock": 490}
OUTROS = [{"id": str(n), "name": f"Produto {n}"} for n in range(3, 10)]


def _lista(produtos) -> list[PresentedCommerceProduct]:
    return [
        PresentedCommerceProduct(
            product_id=p["id"], name=p["name"], reference=p.get("reference"), position=i
        )
        for i, p in enumerate(produtos, start=1)
    ]


def _executor(produtos):
    chamadas: list[tuple[str, dict]] = []

    async def executar(tool, args):
        chamadas.append((tool, dict(args)))
        if tool == "search_products":
            consulta = str(args.get("query") or "").casefold()
            if not consulta:
                return {"ok": True, "products": produtos}
            achados = [
                p for p in produtos
                if consulta in p["name"].casefold()
                or consulta in str(p.get("reference") or "").casefold()
            ]
            return {"ok": True, "products": achados}
        alvo = next((p for p in produtos if p["id"] == str(args.get("product_id"))), None)
        if alvo is None:
            return {"ok": False, "error": "product_not_found"}
        if tool == "get_product":
            return {"ok": True, "product": alvo}
        return {"ok": True, "stock": alvo.get("stock"), "stock_confirmed": True}

    executar.chamadas = chamadas
    return executar


def _tools(executar) -> list[str]:
    return [t for t, _ in executar.chamadas]


# === A. intencao transacional nao e nome de produto ========================


@pytest.mark.parametrize(
    "texto",
    ["quero fazer um pedido", "quero fazer pedido", "quero comprar",
     "quero pedir", "como faço um pedido?", "quero fechar um pedido",
     "quero levar", "como compro?"],
)
def test_purchase_intent_is_not_a_product_search(texto):
    resolucao = resolve_commerce_turn(texto, state=CommerceConversationState())
    assert resolucao.action == ACTION_START_PURCHASE
    assert resolucao.requires_catalog_search is False


@pytest.mark.parametrize("texto", ["quero fazer um pedido", "quero comprar"])
def test_the_word_pedido_never_becomes_a_query(texto):
    consulta = product_query_for(texto)
    assert consulta is None or "pedido" not in consulta


@pytest.mark.asyncio
async def test_purchase_intent_does_not_hit_the_catalog():
    """O caso A de producao: "quero fazer um pedido" -> "nao encontrei"."""
    estado = CommerceConversationState()
    executar = _executor([AD190])
    r = await run_commerce_turn("quero fazer um pedido", state=estado, execute=executar)
    assert r.outcome == OUTCOME_PURCHASE_INTENT
    assert r.outcome != OUTCOME_PRODUCT_NOT_FOUND
    assert "search_products" not in _tools(executar)


# === B. "tem produto disponivel?" e browse, nao clarificacao ===============


@pytest.mark.parametrize(
    "texto",
    ["tem produto disponivel pronta entrega?", "tem produtos disponiveis?",
     "tem pronta entrega?", "o que tem pronta entrega?",
     "mostra o que tem em estoque", "o que voces tem disponivel?",
     "tem coisa disponivel agora?"],
)
def test_availability_questions_without_a_product_are_browse(texto):
    resolucao = resolve_commerce_turn(texto, state=CommerceConversationState())
    assert resolucao.action == ACTION_BROWSE_CATALOG


@pytest.mark.asyncio
async def test_availability_browse_returns_products_without_asking_first():
    estado = CommerceConversationState()
    executar = _executor([AD190, CA31, FKT])
    r = await run_commerce_turn(
        "tem produto disponivel pronta entrega?", state=estado, execute=executar
    )
    assert r.outcome == OUTCOME_BROWSE
    assert r.products
    argumentos = [a for t, a in executar.chamadas if t == "search_products"]
    assert argumentos[0]["available"] is True


# === C/D/E. pending action: sim / si m / nao ===============================


def test_a_bare_yes_without_a_pending_action_invents_nothing():
    resolucao = resolve_commerce_turn("sim", state=CommerceConversationState())
    assert resolucao.action != ACTION_CONFIRM_PENDING


@pytest.mark.parametrize(
    "texto", ["sim", "s", "ss", "ssim", "si m", "claro", "pode", "pode ser",
              "ok", "beleza", "vamos", "manda", "mostra"],
)
def test_confirmations_answer_the_pending_action(texto):
    estado = CommerceConversationState(pending_commerce_action=PENDING_BROWSE)
    assert resolve_commerce_turn(texto, state=estado).action == ACTION_CONFIRM_PENDING


@pytest.mark.parametrize(
    "texto", ["não", "nao", "na o", "n", "não quero", "agora não", "deixa", "não precisa"],
)
def test_rejections_answer_the_pending_action(texto):
    estado = CommerceConversationState(pending_commerce_action=PENDING_BROWSE)
    assert resolve_commerce_turn(texto, state=estado).action == ACTION_REJECT_PENDING


@pytest.mark.asyncio
async def test_confirming_runs_the_pending_browse_and_clears_it():
    """O caso B: "sim" respondia "nao encontrei esse produto"."""
    estado = CommerceConversationState(pending_commerce_action=PENDING_BROWSE)
    executar = _executor([AD190, CA31])
    r = await run_commerce_turn("sim", state=estado, execute=executar)
    assert r.outcome == OUTCOME_BROWSE
    assert r.products
    assert estado.pending_commerce_action is None


@pytest.mark.asyncio
async def test_a_typo_confirmation_behaves_the_same():
    estado = CommerceConversationState(pending_commerce_action=PENDING_BROWSE)
    executar = _executor([AD190])
    r = await run_commerce_turn("si m", state=estado, execute=executar)
    assert r.outcome == OUTCOME_BROWSE
    assert estado.pending_commerce_action is None


@pytest.mark.asyncio
async def test_rejecting_clears_the_pending_without_searching():
    estado = CommerceConversationState(pending_commerce_action=PENDING_BROWSE)
    executar = _executor([AD190])
    await run_commerce_turn("não", state=estado, execute=executar)
    assert estado.pending_commerce_action is None
    assert "search_products" not in _tools(executar)


# === F. preco com produto explicito ========================================


def test_a_price_question_with_a_reference_is_a_price_question():
    """O caso C: virava consulta de LINK do produto."""
    texto = (
        "qual valor d AD-190 - Caixa de Som Rádio Retrô Bluetooth 5w "
        "Recarregável Portátil AM FM SM Altomex"
    )
    resolucao = resolve_commerce_turn(texto, state=CommerceConversationState())
    assert resolucao.action == ACTION_GET_PRICE
    assert resolucao.explicit_reference == "AD-190"


@pytest.mark.asyncio
async def test_the_price_of_an_explicit_reference_is_answered():
    estado = CommerceConversationState()
    executar = _executor([AD190, CA31, FKT])
    r = await run_commerce_turn(
        "qual valor d AD-190 - Caixa de Som Rádio Retrô Bluetooth 5w Recarregável",
        state=estado, execute=executar,
    )
    assert r.outcome == OUTCOME_PRICE
    assert r.product["id"] == "90"
    assert "get_product_link" not in _tools(executar)


@pytest.mark.parametrize(
    "texto",
    ["quanto custa", "quanto é", "quanto fica", "quanto sai", "valor",
     "qual valor", "preço", "qual preço", "é quanto", "sai quanto",
     "por quanto", "custa quanto", "qual valor d", "valor desse",
     "preço desse", "e o valor?", "vlr"],
)
def test_price_synonyms(texto):
    estado = CommerceConversationState(last_presented_products=_lista([AD190]))
    assert resolve_commerce_turn(texto, state=estado).action == ACTION_GET_PRICE


# === G/H. posicao colada ao texto ==========================================


def test_a_trailing_number_is_a_position_when_the_text_matches_that_item():
    """O caso D: "…lightning7" com o item 7 sendo justamente esse carregador."""
    lista = _lista(OUTROS[:6] + [CA31])
    estado = CommerceConversationState(last_presented_products=lista)
    resolucao = resolve_commerce_turn(
        "me fale mais sobre o iphone kit carregador lightning7", state=estado
    )
    assert resolucao.action == ACTION_GET_DETAILS
    assert resolucao.reference_type == REF_LIST_POSITION
    assert resolucao.reference_position == 7
    assert resolucao.resolved_product.product_id == "31"


def test_a_trailing_number_is_not_a_position_when_the_text_does_not_match():
    """"iphone7" nao pode virar item 7 so pelo sufixo — 7 pode ser o modelo."""
    lista = _lista(OUTROS[:6] + [FKT])
    estado = CommerceConversationState(last_presented_products=lista)
    resolucao = resolve_commerce_turn("quero iphone7", state=estado)
    assert resolucao.reference_position != 7
    assert (resolucao.resolved_product is None
            or resolucao.resolved_product.product_id != FKT["id"])


@pytest.mark.parametrize("texto", ["quero iphone7", "tem s23?", "tem a54?", "phone5"])
def test_model_numbers_are_not_read_as_positions(texto):
    lista = _lista(OUTROS)
    estado = CommerceConversationState(last_presented_products=lista)
    resolucao = resolve_commerce_turn(texto, state=estado)
    assert resolucao.reference_type != REF_LIST_POSITION


# === detalhes: flexoes verbais =============================================


@pytest.mark.parametrize(
    "texto",
    ["me fale mais", "fala mais", "fale mais", "me conta mais", "conta mais",
     "me explica", "explica melhor", "quero saber mais", "quais detalhes",
     "quais especificações", "ficha técnica", "como ele é"],
)
def test_detail_flexions(texto):
    estado = CommerceConversationState(last_presented_products=_lista([AD190]))
    assert resolve_commerce_turn(texto, state=estado).action == ACTION_GET_DETAILS


# === I/J. produto explicito e produto ativo ================================


@pytest.mark.asyncio
async def test_follow_ups_on_the_active_product():
    estado = CommerceConversationState()
    executar = _executor([AD190])
    await run_commerce_turn("qual preço do AD-190?", state=estado, execute=executar)
    assert estado.active_product.product_id == "90"

    await run_commerce_turn("tem estoque?", state=estado, execute=executar)
    await run_commerce_turn("tem foto?", state=estado, execute=executar)
    ids = [str(a.get("product_id")) for t, a in executar.chamadas if "product_id" in a]
    assert set(ids) == {"90"}


@pytest.mark.asyncio
async def test_an_explicit_product_works_without_any_previous_turn():
    estado = CommerceConversationState()
    executar = _executor([FKT, AD190])
    r = await run_commerce_turn("qual preço do FKT-110C?", state=estado, execute=executar)
    assert r.outcome == OUTCOME_PRICE
    assert r.product["id"] == "2"


# === precedencia do router =================================================


def test_read_only_commerce_actions_precede_the_legacy_flow():
    """Fato comercial simples nao pode passar antes por link/carrinho/checkout."""
    import inspect

    from app import sales_agent

    fonte = inspect.getsource(sales_agent._generic_catalog_fast_path)
    for acao in (
        "ACTION_GET_PRICE",
        "ACTION_CHECK_INVENTORY",
        "ACTION_SHOW_MEDIA",
        "ACTION_GET_DETAILS",
        "ACTION_SEARCH_PRODUCT",
        "ACTION_COMPARE",
        "ACTION_START_PURCHASE",
        "ACTION_CONFIRM_PENDING",
    ):
        assert acao in fonte, f"{acao} nao tem precedencia sobre o fluxo legado"


@pytest.mark.parametrize(
    "texto", ["quero pagar", "qual forma de pagamento?", "finalizar compra", "meu carrinho"],
)
def test_transaction_messages_are_left_to_their_own_flows(texto):
    """Fato de produto e transacao sao coisas diferentes."""
    from app.commerce.turn_resolver import READ_ONLY_ACTIONS

    estado = CommerceConversationState(last_presented_products=_lista([AD190]))
    resolucao = resolve_commerce_turn(texto, state=estado)
    assert resolucao.action not in READ_ONLY_ACTIONS or resolucao.action == ACTION_START_PURCHASE


@pytest.mark.asyncio
async def test_the_product_code_is_searched_before_the_long_description():
    """Numa fonte sem coluna de referencia, o codigo vive dentro do nome.

    "AD-190" acha o produto; a descricao inteira nao acha nenhum, porque a
    leitura do indice e um LIKE contiguo e o nome nao contem a frase do cliente.
    """
    estado = CommerceConversationState()
    executar = _executor([AD190, CA31, FKT])
    r = await run_commerce_turn(
        "qual valor d AD-190 - Caixa de Som Rádio Retrô Bluetooth 5w Recarregável "
        "Portátil AM FM SM Altomex",
        state=estado, execute=executar,
    )
    assert r.outcome == OUTCOME_PRICE
    assert r.product["id"] == "90"
    primeira = [a["query"] for t, a in executar.chamadas if t == "search_products"][0]
    # O hifen sobrevive: sem ele o LIKE contiguo nao casa o nome do produto.
    assert primeira == "AD-190"
