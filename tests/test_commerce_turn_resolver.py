"""Continuidade comercial: de qual produto estamos falando.

O sintoma que originou este modulo e uma conversa inteira que nao se sustenta::

    BOT     "Estes sao alguns produtos: 1. Ring Light  2. FKT-110C Tipo C Kit
             Carregador  3. FKT-1108 Kit Carregador  4. Cabo USB"
    CLIENTE "quero o carregador tipo c"
    BOT     "Nao encontrei esse produto"          <- estava na tela
    CLIENTE (cola o nome completo)
    BOT     "Encontrei"
    CLIENTE "tem foto?"
    BOT     "Nao encontrei esse produto"          <- acabou de encontrar

Cada turno era interpretado como busca nova. O que falta nao e catalogo nem
persona: e resolver a REFERENCIA — "esse", "o segundo", "o carregador tipo c" —
contra o que ja esta em jogo na conversa.

Este modulo e puro: recebe texto e estado, devolve decisao. Nao chama tool, nao
conhece fornecedor, nao toca persona. Quem verbaliza continua sendo a persona;
quem busca fato continua sendo a tool.
"""

from __future__ import annotations

import pytest

from app.commerce.turn_resolver import (
    ACTION_BROWSE_CATALOG,
    ACTION_CHECK_INVENTORY,
    ACTION_COMPARE,
    ACTION_CORRECT_REFERENCE,
    ACTION_GET_DETAILS,
    ACTION_GET_PRICE,
    ACTION_REJECT_PRODUCT,
    ACTION_SEARCH_PRODUCT,
    ACTION_SELECT_PRODUCT,
    ACTION_SHOW_MEDIA,
    ACTION_SHOW_MORE_MEDIA,
    FACT_INVENTORY,
    FACT_MEDIA,
    FACT_PRICE,
    REF_CURRENT,
    REF_EXPLICIT,
    REF_LIST_POSITION,
    REF_SEMANTIC,
    product_query_for,
    resolve_commerce_turn,
)
from app.commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    PresentedCommerceProduct,
)

RING = {"id": "1", "name": "Q508A - Ring Light Bastao Led RGB Profissional"}
TIPOC = {"id": "2", "name": "FKT-110C - Tipo C Kit Carregador USB 2.1A com Cabo"}
V8 = {"id": "3", "name": "FKT-1108 - V8 Kit Carregador USB 2.1A com Cabo"}
CABO = {"id": "4", "name": "CB702V - V8 Cabo Usb 1 Metro Premium"}
LISTA = [RING, TIPOC, V8, CABO]


def _apresentados(produtos=LISTA) -> list[PresentedCommerceProduct]:
    return [
        PresentedCommerceProduct(
            product_id=p["id"], name=p["name"], position=posicao
        )
        for posicao, p in enumerate(produtos, start=1)
    ]


def _estado(**kwargs) -> CommerceConversationState:
    base = {"last_presented_products": _apresentados()}
    base.update(kwargs)
    return CommerceConversationState(**base)


def _ativo(produto) -> CommerceProductReference:
    return CommerceProductReference(product_id=produto["id"], name=produto["name"])


# === 1. a query do produto nao carrega verbo conversacional ================


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("quero o carregador tipo c", "carregador tipo c"),
        ("me mostra um cabo lightning", "cabo lightning"),
        ("preciso de ring light", "ring light"),
        ("voces tem fone bluetooth?", "fone bluetooth"),
        ("estou procurando carregador", "carregador"),
        ("gostaria de ver um cabo usb", "cabo usb"),
    ],
)
def test_conversational_verbs_are_not_part_of_the_product_query(texto, esperado):
    """"quero" nao e caracteristica de produto: buscar por ele nao casa nada."""
    assert product_query_for(texto) == esperado


def test_a_meaningless_query_is_not_produced():
    """Token isolado curto demais nao vira busca."""
    assert product_query_for("quero") is None
    assert product_query_for("tem?") is None


def test_a_short_token_survives_inside_a_meaningful_expression():
    """"c" sozinho nao, "tipo c" sim."""
    assert product_query_for("quero o tipo c") == "tipo c"
    assert product_query_for("tem usb c?") == "usb c"


# === 2. selecao a partir da lista apresentada ==============================


def test_selecting_by_words_resolves_against_the_presented_list():
    """O caso de producao: "quero o carregador tipo c" com a lista na tela."""
    resolucao = resolve_commerce_turn("quero o carregador tipo c", state=_estado())
    assert resolucao.action == ACTION_SELECT_PRODUCT
    assert resolucao.reference_type == REF_SEMANTIC
    assert resolucao.resolved_product.product_id == "2"
    assert resolucao.requires_catalog_search is False


def test_the_presented_list_is_tried_before_the_global_catalog():
    """6.120 produtos nao precisam ser varridos quando a lista ja resolve."""
    resolucao = resolve_commerce_turn("quero o ring light", state=_estado())
    assert resolucao.resolved_product.product_id == "1"
    assert resolucao.requires_catalog_search is False


def test_two_plausible_items_ask_instead_of_guessing():
    """Dois carregadores na lista: escolher um seria afirmar por sorteio."""
    resolucao = resolve_commerce_turn("quero o carregador", state=_estado())
    assert resolucao.requires_clarification is True
    assert resolucao.resolved_product is None
    assert {c.product_id for c in resolucao.candidates} == {"2", "3"}


def test_a_follow_up_narrows_the_ambiguity():
    estado = _estado()
    resolucao = resolve_commerce_turn("o tipo c", state=estado)
    assert resolucao.resolved_product.product_id == "2"


# === 3. posicao na lista ===================================================


@pytest.mark.parametrize(
    "texto,posicao",
    [
        ("o primeiro", 1), ("primeiro", 1), ("o 1", 1), ("1", 1),
        ("o segundo", 2), ("segundo", 2), ("2", 2),
        ("o terceiro", 3), ("o ultimo", 4), ("o último", 4),
    ],
)
def test_position_references_resolve_against_the_list(texto, posicao):
    resolucao = resolve_commerce_turn(texto, state=_estado())
    assert resolucao.reference_type == REF_LIST_POSITION
    assert resolucao.resolved_product.product_id == str(posicao)


def test_a_position_beyond_the_list_does_not_invent_a_product():
    resolucao = resolve_commerce_turn("o decimo", state=_estado())
    assert resolucao.resolved_product is None


def test_a_position_without_a_list_asks_instead_of_guessing():
    resolucao = resolve_commerce_turn("o segundo", state=CommerceConversationState())
    assert resolucao.resolved_product is None
    assert resolucao.requires_clarification is True


# === 4. referencia ao produto atual ========================================


@pytest.mark.parametrize(
    "texto",
    ["esse", "esse ai", "este", "quero ele", "fico com esse", "esse mesmo",
     "gostei desse", "pode ser esse", "esse produto"],
)
def test_current_product_references_use_the_active_product(texto):
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn(texto, state=estado)
    assert resolucao.reference_type == REF_CURRENT
    assert resolucao.resolved_product.product_id == "2"


def test_a_current_reference_falls_back_to_a_single_item_list():
    estado = CommerceConversationState(last_presented_products=_apresentados([TIPOC]))
    resolucao = resolve_commerce_turn("quero esse", state=estado)
    assert resolucao.resolved_product.product_id == "2"


def test_a_current_reference_with_an_ambiguous_list_does_not_guess():
    resolucao = resolve_commerce_turn("quero esse", state=_estado())
    assert resolucao.resolved_product is None
    assert resolucao.requires_clarification is True


# === 5. acoes sobre o produto ativo ========================================


@pytest.mark.parametrize(
    "texto",
    ["quanto custa?", "qual o preco?", "preco?", "valor?", "e quanto?",
     "sai por quanto?", "quanto fica?", "e o preco?"],
)
def test_price_questions_use_the_active_product(texto):
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn(texto, state=estado)
    assert resolucao.action == ACTION_GET_PRICE
    assert resolucao.requested_fact == FACT_PRICE
    assert resolucao.resolved_product.product_id == "2"


@pytest.mark.parametrize(
    "texto",
    ["tem estoque?", "tem?", "ainda tem?", "esta disponivel?",
     "tem disponivel?", "acabou?", "quantos tem?", "e o estoque?"],
)
def test_inventory_questions_use_the_active_product(texto):
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn(texto, state=estado)
    assert resolucao.action == ACTION_CHECK_INVENTORY
    assert resolucao.requested_fact == FACT_INVENTORY
    assert resolucao.resolved_product.product_id == "2"


@pytest.mark.parametrize(
    "texto",
    ["tem foto?", "tem fotos?", "tem imagem?", "manda foto", "manda a foto",
     "mostra foto", "mostra imagem", "posso ver?", "me mostra esse"],
)
def test_media_questions_use_the_active_product(texto):
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn(texto, state=estado)
    assert resolucao.action == ACTION_SHOW_MEDIA
    assert resolucao.requested_fact == FACT_MEDIA
    assert resolucao.resolved_product.product_id == "2"


@pytest.mark.parametrize(
    "texto", ["me fala mais", "me explica", "quais detalhes?", "quais especificacoes?"]
)
def test_detail_questions_use_the_active_product(texto):
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn(texto, state=estado)
    assert resolucao.action == ACTION_GET_DETAILS
    assert resolucao.resolved_product.product_id == "2"


# === 6. o estado desambigua mensagem curta =================================


def test_more_after_media_means_another_image():
    """"tem outra?" depois de foto e outra foto, nao outro produto."""
    estado = _estado(
        active_product=_ativo(TIPOC),
        last_commerce_action=ACTION_SHOW_MEDIA,
        last_requested_fact=FACT_MEDIA,
    )
    resolucao = resolve_commerce_turn("manda outra", state=estado)
    assert resolucao.action == ACTION_SHOW_MORE_MEDIA
    assert resolucao.resolved_product.product_id == "2"


@pytest.mark.parametrize("texto", ["tem outra?", "mais fotos", "outra foto", "mais"])
def test_more_media_variants_after_media(texto):
    estado = _estado(
        active_product=_ativo(TIPOC),
        last_commerce_action=ACTION_SHOW_MEDIA,
        last_requested_fact=FACT_MEDIA,
    )
    assert resolve_commerce_turn(texto, state=estado).action == ACTION_SHOW_MORE_MEDIA


def test_the_same_word_means_another_product_outside_a_media_context():
    """"outro" depois de preco nao pede outra imagem."""
    estado = _estado(
        active_product=_ativo(TIPOC),
        last_commerce_action=ACTION_GET_PRICE,
        last_requested_fact=FACT_PRICE,
    )
    resolucao = resolve_commerce_turn("tem outro?", state=estado)
    assert resolucao.action != ACTION_SHOW_MORE_MEDIA


def test_a_bare_have_it_after_details_means_availability():
    estado = _estado(
        active_product=_ativo(TIPOC), last_commerce_action=ACTION_GET_DETAILS
    )
    assert resolve_commerce_turn("tem?", state=estado).action == ACTION_CHECK_INVENTORY


# === 7. continuidade com "e ..." ===========================================


def test_the_leading_and_signals_context_dependency():
    estado = _estado(active_product=_ativo(TIPOC))
    for texto in ("e o preco?", "e ele?", "e a foto?"):
        resolucao = resolve_commerce_turn(texto, state=estado)
        assert resolucao.resolved_product is not None
        assert resolucao.requires_catalog_search is False


def test_and_the_second_applies_the_last_fact_to_another_position():
    """"tem estoque?" e depois "e o segundo?" = estoque do segundo."""
    estado = _estado(
        active_product=_ativo(RING),
        last_commerce_action=ACTION_CHECK_INVENTORY,
        last_requested_fact=FACT_INVENTORY,
    )
    resolucao = resolve_commerce_turn("e o segundo?", state=estado)
    assert resolucao.resolved_product.product_id == "2"
    assert resolucao.action == ACTION_CHECK_INVENTORY


# === 8. correcao de referencia =============================================


def test_a_correction_moves_to_the_named_position():
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn("nao, o terceiro", state=estado)
    assert resolucao.action == ACTION_CORRECT_REFERENCE
    assert resolucao.resolved_product.product_id == "3"


@pytest.mark.parametrize("texto", ["nao esse", "esse nao", "quis dizer o outro"])
def test_a_rejection_does_not_keep_the_active_product(texto):
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn(texto, state=estado)
    assert resolucao.action in {ACTION_REJECT_PRODUCT, ACTION_CORRECT_REFERENCE}
    assert resolucao.rejected_product_id == "2"


def test_a_rejection_asking_for_an_alternative_searches_again():
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn(
        "nao quero esse, tem outro parecido?", state=estado
    )
    assert resolucao.rejected_product_id == "2"
    assert resolucao.resolved_product is None


# === 9. troca de assunto ===================================================


def test_naming_a_different_product_starts_a_new_search():
    """"e tem ring light?" com carregador ativo nao e sobre o carregador."""
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn("e tem ring light?", state=estado)
    assert resolucao.product_query == "ring light"
    assert resolucao.resolved_product.product_id == "1"


def test_an_unknown_product_requires_a_catalog_search():
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn("tem cabo lightning?", state=estado)
    assert resolucao.action == ACTION_SEARCH_PRODUCT
    assert resolucao.requires_catalog_search is True
    assert resolucao.product_query == "cabo lightning"


def test_a_search_without_any_state_is_still_a_search():
    resolucao = resolve_commerce_turn(
        "preciso de cabo lightning", state=CommerceConversationState()
    )
    assert resolucao.requires_catalog_search is True
    assert resolucao.product_query == "cabo lightning"


# === 10. browse e comparacao ===============================================


@pytest.mark.parametrize(
    "texto",
    ["o que voces vendem?", "me mostre alguns produtos", "quero ver o catalogo",
     "me mostre outros", "tem mais?"],
)
def test_browse_stays_browse(texto):
    assert resolve_commerce_turn(texto, state=_estado()).action == ACTION_BROWSE_CATALOG


@pytest.mark.parametrize(
    "texto",
    ["qual e melhor?", "qual desses e melhor?", "o primeiro ou o segundo?",
     "qual vale mais a pena?"],
)
def test_comparison_is_recognized(texto):
    assert resolve_commerce_turn(texto, state=_estado()).action == ACTION_COMPARE


# === 11. referencia explicita vence tudo ===================================


def test_an_explicit_reference_beats_the_active_product():
    estado = _estado(active_product=_ativo(TIPOC))
    resolucao = resolve_commerce_turn("quanto custa a CB702V?", state=estado)
    assert resolucao.reference_type == REF_EXPLICIT
    assert resolucao.explicit_reference == "CB702V"


def test_an_explicit_ean_is_captured():
    resolucao = resolve_commerce_turn(
        "tem o 7899999999999?", state=CommerceConversationState()
    )
    assert resolucao.explicit_ean == "7899999999999"


# === 12. o resolver nao decide sozinho =====================================


def test_the_resolver_never_calls_a_tool():
    """Puro por construcao: so texto e estado entram."""
    import inspect

    from app.commerce import turn_resolver

    fonte = inspect.getsource(turn_resolver)
    for proibido in ("execute_tool", "httpx", "get_commerce_provider", "requests"):
        assert proibido not in fonte


def test_the_resolver_knows_no_vendor():
    import inspect

    from app.commerce import turn_resolver

    fonte = inspect.getsource(turn_resolver).casefold()
    for marca in ("mercos", "tray", "newstore"):
        assert marca not in fonte
