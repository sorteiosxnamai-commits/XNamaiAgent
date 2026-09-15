"""Identidade de produto para catalogo comum, sem a logica de relogios.

Dois erros distintos motivaram este modulo, e o segundo e o grave.

O primeiro: perguntas genericas ("o que voces vendem?") nunca chegavam ao
catalogo. O planejador as classificava como `goal="discover"`, o gate de
clarificacao disparava antes de qualquer busca, e o cliente recebia uma pergunta
de volta com 6.120 produtos indexados.

O segundo: perguntado por "Cabo Lightning iPhone Hmaston", o agente respondeu
"Sim, encontrei" e listou fone de ouvido e caixa de som — produtos de marca
igual e identidade completamente diferente. Marca coincidente nao prova
identidade de produto. Afirmar preco e estoque do item errado e pior que nao
responder, porque o cliente nao tem como perceber a troca.

A regra que este modulo aplica: marca AMPLIA e ORDENA candidatos; nunca os
promove a match. Um candidato so vence por evidencia de identidade —
referencia, EAN, id, nome exato ou sobreposicao forte dos termos do nome.
"""

from __future__ import annotations

import pytest

from app.commerce.generic_catalog import (
    GENERIC_BROWSE,
    BROWSE_SAMPLE_LIMIT,
    classify_catalog_request,
    match_generic_catalog_product,
    normalize_tokens,
)

CABO = {
    "id": "1", "name": "Cabo Lightning iPhone Hmaston",
    "reference": "CB827L", "price": 13.65, "stock": 180, "available": True,
}
FONE = {
    "id": "2", "name": "Fone Bluetooth Hmaston",
    "reference": "LY-11", "price": 28.80, "stock": 5, "available": True,
}
CAIXA = {
    "id": "3", "name": "Caixa de Som Bluetooth Hmaston",
    "reference": "R11", "price": 60.00, "stock": 1, "available": True,
}
TODOS = [CABO, FONE, CAIXA]


# === 1. normalizacao ========================================================


def test_tokens_are_case_and_accent_insensitive():
    assert normalize_tokens("Cabo LIGHTNING iPhône") == normalize_tokens(
        "cabo lightning iphone"
    )


def test_punctuation_does_not_create_tokens():
    assert normalize_tokens("cabo, lightning - iphone!") == ("cabo", "lightning", "iphone")


def test_extremely_generic_words_are_dropped():
    """"produto", "voces", "tem" nao distinguem nada."""
    tokens = normalize_tokens("voces tem algum produto de cabo lightning?")
    assert "cabo" in tokens and "lightning" in tokens
    for generico in ("voces", "tem", "algum", "produto", "de"):
        assert generico not in tokens


# === 2. o bug grave: marca nao prova identidade =============================


def test_the_right_product_wins_over_same_brand_siblings():
    resultado = match_generic_catalog_product("Cabo Lightning iPhone Hmaston", TODOS)
    assert resultado.matched is not None
    assert resultado.matched["id"] == "1"


@pytest.mark.parametrize("irmao", [FONE, CAIXA])
def test_a_same_brand_product_is_never_accepted_as_the_asked_one(irmao):
    """O erro exato de producao: "Sim, encontrei" com o produto errado."""
    resultado = match_generic_catalog_product("Cabo Lightning iPhone Hmaston", [irmao])
    assert resultado.matched is None
    assert resultado.candidates == []


def test_without_the_asked_product_the_answer_is_not_found():
    """Removido o cabo, os irmaos de marca NAO viram resposta afirmativa."""
    resultado = match_generic_catalog_product(
        "Cabo Lightning iPhone Hmaston", [FONE, CAIXA]
    )
    assert resultado.matched is None
    assert resultado.ambiguous is False


def test_brand_alone_is_never_enough_for_a_product_question():
    resultado = match_generic_catalog_product("cabo lightning Hmaston", [FONE])
    assert resultado.matched is None


# === 3. prioridade de identidade ============================================


def test_an_exact_reference_wins():
    resultado = match_generic_catalog_product("CB827L", TODOS)
    assert resultado.matched["id"] == "1"


def test_an_exact_reference_wins_even_against_name_overlap():
    outro = {**FONE, "reference": "CABO-LIGHTNING"}
    resultado = match_generic_catalog_product("CABO-LIGHTNING", [CABO, outro])
    assert resultado.matched["id"] == outro["id"]


def test_an_exact_ean_wins():
    com_ean = {**FONE, "ean": "7899999999999"}
    resultado = match_generic_catalog_product("7899999999999", [CABO, com_ean])
    assert resultado.matched["id"] == com_ean["id"]


def test_an_exact_id_wins():
    resultado = match_generic_catalog_product("3", TODOS)
    assert resultado.matched["id"] == "3"


def test_an_exact_normalized_name_wins():
    resultado = match_generic_catalog_product("cabo lightning iphone hmaston", TODOS)
    assert resultado.matched["id"] == "1"


def test_strong_token_overlap_is_enough():
    resultado = match_generic_catalog_product("cabo lightning iphone", TODOS)
    assert resultado.matched["id"] == "1"


def test_weak_overlap_is_not_enough():
    """Um termo em comum nao identifica produto."""
    resultado = match_generic_catalog_product("bluetooth", [CABO])
    assert resultado.matched is None


# === 4. ambiguidade =========================================================


def test_several_plausible_candidates_do_not_pick_one_arbitrarily():
    a = {"id": "10", "name": "Cabo Lightning 1m", "reference": "A1"}
    b = {"id": "11", "name": "Cabo Lightning 2m", "reference": "B2"}
    resultado = match_generic_catalog_product("cabo lightning", [a, b])
    assert resultado.matched is None
    assert resultado.ambiguous is True
    assert {c["id"] for c in resultado.candidates} == {"10", "11"}


def test_ambiguous_candidates_are_capped_at_three():
    muitos = [
        {"id": str(n), "name": f"Cabo Lightning modelo {n}", "reference": f"R{n}"}
        for n in range(1, 9)
    ]
    resultado = match_generic_catalog_product("cabo lightning", muitos)
    assert resultado.ambiguous is True
    assert len(resultado.candidates) <= 3


def test_an_empty_catalog_is_not_a_match():
    resultado = match_generic_catalog_product("cabo lightning", [])
    assert resultado.matched is None
    assert resultado.ambiguous is False


# === 5. classificacao do pedido =============================================


@pytest.mark.parametrize(
    "texto",
    [
        "o que voces vendem?",
        "o que vocês vendem",
        "me mostre alguns produtos",
        "me mostra uns produtos",
        "quais produtos voces tem?",
        "me recomende tres produtos",
        "me recomenda 3 produtos",
    ],
)
def test_generic_browse_questions_are_recognized(texto):
    assert classify_catalog_request(texto).kind == GENERIC_BROWSE


@pytest.mark.parametrize(
    "texto",
    [
        "voces tem cabo lightning?",
        "quanto custa o Cabo Lightning iPhone Hmaston?",
        "tem estoque do cabo lightning?",
    ],
)
def test_specific_questions_are_not_generic_browse(texto):
    assert classify_catalog_request(texto).kind != GENERIC_BROWSE


def test_a_recommendation_request_carries_how_many_were_asked():
    pedido = classify_catalog_request("me recomende tres produtos")
    assert pedido.kind == GENERIC_BROWSE
    assert pedido.wanted == 3


def test_a_plain_browse_uses_the_sample_limit():
    pedido = classify_catalog_request("o que voces vendem?")
    assert pedido.wanted == BROWSE_SAMPLE_LIMIT


# === 6. brand-only e intencao valida ========================================


@pytest.mark.parametrize(
    "texto,marca",
    [
        ("me mostre produtos Hmaston", "hmaston"),
        ("o que tem da Hmaston?", "hmaston"),
        ("quais produtos da marca Hmaston voces tem", "hmaston"),
    ],
)
def test_brand_only_browsing_is_a_valid_intent(texto, marca):
    pedido = classify_catalog_request(texto)
    assert pedido.brand == marca
    assert pedido.kind == GENERIC_BROWSE


def test_a_product_question_with_a_brand_is_not_brand_only():
    """"tem cabo lightning Hmaston?" precisa do produto, nao so da marca."""
    pedido = classify_catalog_request("tem cabo lightning Hmaston?")
    assert pedido.kind != GENERIC_BROWSE


# === 7. o matcher nunca inventa ============================================


def test_the_matcher_never_returns_a_product_absent_from_the_candidates():
    resultado = match_generic_catalog_product("cabo lightning iphone hmaston", TODOS)
    assert resultado.matched in TODOS


def test_a_candidate_without_a_name_is_ignored_not_crashed():
    resultado = match_generic_catalog_product("cabo", [{"id": "9"}, CABO])
    assert resultado.matched is None or resultado.matched["id"] == "1"


# === 8. o fluxo completo: pedido -> tools -> resposta ======================


def _executor(respostas):
    """Duplo de `execute_tool` que registra as chamadas."""
    chamadas: list[tuple[str, dict]] = []

    async def executar(tool, args):
        chamadas.append((tool, dict(args)))
        valor = respostas.get(tool)
        return valor(args) if callable(valor) else (valor or {"ok": False})

    executar.chamadas = chamadas
    return executar


@pytest.mark.asyncio
async def test_generic_browse_queries_the_catalog_instead_of_asking_back():
    from app.commerce.generic_catalog import ANSWER_BROWSE, resolve_catalog_request

    executar = _executor({"search_products": {"ok": True, "products": TODOS}})
    resposta = await resolve_catalog_request("o que voces vendem?", execute=executar)

    assert resposta.kind == ANSWER_BROWSE
    assert len(resposta.products) >= 1
    tool, args = executar.chamadas[0]
    assert tool == "search_products"
    assert args["page"] == 1 and args["available"] is True


@pytest.mark.asyncio
async def test_a_recommendation_returns_at_most_the_number_asked():
    from app.commerce.generic_catalog import resolve_catalog_request

    executar = _executor({"search_products": {"ok": True, "products": TODOS}})
    resposta = await resolve_catalog_request("me recomende tres produtos", execute=executar)
    assert len(resposta.products) <= 3


@pytest.mark.asyncio
async def test_a_specific_product_question_confirms_identity_before_answering():
    from app.commerce.generic_catalog import ANSWER_PRODUCT, resolve_catalog_request

    executar = _executor({
        "search_products": {"ok": True, "products": TODOS},
        "get_product": {"ok": True, "product": {**CABO, "price": 13.65}},
        "check_inventory": {"ok": True, "stock": 180, "stock_confirmed": True},
    })
    resposta = await resolve_catalog_request(
        "quanto custa o Cabo Lightning iPhone Hmaston?", execute=executar
    )
    assert resposta.kind == ANSWER_PRODUCT
    assert resposta.product["id"] == "1"
    # O id consultado em get_product/check_inventory e o do produto certo.
    ids = [a.get("product_id") for t, a in executar.chamadas if t != "search_products"]
    assert ids == ["1", "1"]


@pytest.mark.asyncio
async def test_a_same_brand_result_never_becomes_an_affirmative_answer():
    """O bug de producao, agora no fluxo inteiro."""
    from app.commerce.generic_catalog import ANSWER_NOT_FOUND, resolve_catalog_request

    executar = _executor({"search_products": {"ok": True, "products": [FONE, CAIXA]}})
    resposta = await resolve_catalog_request(
        "quanto custa o Cabo Lightning iPhone Hmaston?", execute=executar
    )
    assert resposta.kind == ANSWER_NOT_FOUND
    assert resposta.product is None
    # Nenhuma consulta de preco/estoque foi feita sobre produto errado.
    assert [t for t, _ in executar.chamadas] == ["search_products"]


@pytest.mark.asyncio
async def test_a_failed_search_is_not_a_missing_product():
    """"Nao consegui olhar" e diferente de "nao temos"."""
    from app.commerce.generic_catalog import resolve_catalog_request

    executar = _executor({"search_products": {"ok": False, "error": "boom"}})
    assert await resolve_catalog_request("cabo lightning", execute=executar) is None


@pytest.mark.asyncio
async def test_brand_only_browsing_passes_the_brand_to_the_search():
    from app.commerce.generic_catalog import ANSWER_BROWSE, resolve_catalog_request

    executar = _executor({"search_products": {"ok": True, "products": TODOS}})
    resposta = await resolve_catalog_request("me mostre produtos Hmaston", execute=executar)
    assert resposta.kind == ANSWER_BROWSE
    assert executar.chamadas[0][1]["brand"] == "hmaston"


@pytest.mark.asyncio
async def test_ambiguity_offers_options_instead_of_guessing():
    from app.commerce.generic_catalog import ANSWER_AMBIGUOUS, resolve_catalog_request

    a = {"id": "10", "name": "Cabo Lightning 1m"}
    b = {"id": "11", "name": "Cabo Lightning 2m"}
    executar = _executor({"search_products": {"ok": True, "products": [a, b]}})
    resposta = await resolve_catalog_request("voces tem cabo lightning?", execute=executar)
    assert resposta.kind == ANSWER_AMBIGUOUS
    assert len(resposta.products) == 2


# === 9. a consulta enviada ao indice ======================================


def test_the_whole_sentence_is_never_sent_as_the_query():
    """`LIKE '%trecho%'` e contiguo: a frase inteira nunca casa um nome."""
    from app.commerce.generic_catalog import search_queries_for

    consultas = search_queries_for("quanto custa o Cabo Lightning iPhone Hmaston?")
    assert consultas[0] == "cabo lightning iphone hmaston"
    assert all("quanto" not in c and "custa" not in c for c in consultas)


def test_queries_go_from_most_specific_to_broadest():
    from app.commerce.generic_catalog import search_queries_for

    consultas = search_queries_for("cabo lightning iphone hmaston")
    assert consultas == [
        "cabo lightning iphone hmaston",
        "cabo lightning iphone",
        "cabo lightning",
        "cabo",
    ]


@pytest.mark.asyncio
async def test_the_search_widens_only_until_it_finds_candidates():
    """Encurtar amplia candidatos; quem decide identidade segue sendo o matcher."""
    from app.commerce.generic_catalog import ANSWER_PRODUCT, resolve_catalog_request

    def busca(args):
        # So a consulta de dois termos devolve algo.
        if args["query"] == "cabo lightning":
            return {"ok": True, "products": [CABO]}
        return {"ok": True, "products": []}

    executar = _executor({
        "search_products": busca,
        "get_product": {"ok": True, "product": CABO},
        "check_inventory": {"ok": True, "stock": 180},
    })
    resposta = await resolve_catalog_request(
        "quanto custa o Cabo Lightning iPhone Hmaston?", execute=executar
    )
    assert resposta.kind == ANSWER_PRODUCT
    assert resposta.product["id"] == "1"
    consultas = [a["query"] for t, a in executar.chamadas if t == "search_products"]
    assert consultas == ["cabo lightning iphone hmaston", "cabo lightning iphone", "cabo lightning"]


@pytest.mark.asyncio
async def test_widening_never_turns_a_sibling_into_an_answer():
    """Mesmo com a consulta mais larga, marca igual nao vira match."""
    from app.commerce.generic_catalog import ANSWER_NOT_FOUND, resolve_catalog_request

    executar = _executor({"search_products": {"ok": True, "products": [FONE, CAIXA]}})
    resposta = await resolve_catalog_request(
        "quanto custa o Cabo Lightning iPhone Hmaston?", execute=executar
    )
    assert resposta.kind == ANSWER_NOT_FOUND


# === 10. ordem das chamadas no sales_agent ================================


def test_the_early_call_is_restricted_to_browse():
    """Cedo demais para pedido especifico: carrinho e checkout vem depois."""
    import inspect

    from app import sales_agent

    fonte = inspect.getsource(sales_agent._handle_sales_message_inner)
    precoce = fonte[: fonte.index("deterministic_confirmation = ")]
    assert "only_browse=True" in precoce


def test_the_full_fast_path_runs_before_the_clarification_gates():
    import inspect

    from app import sales_agent

    fonte = inspect.getsource(sales_agent._handle_sales_message_inner)
    assert fonte.index("resposta_catalogo = await _generic_catalog_fast_path(") < fonte.index(
        "_needs_clarification_before_retrieval(interpretation, plan, discovery_state)"
    )


@pytest.mark.asyncio
async def test_a_checkout_message_is_not_hijacked_by_the_early_call(monkeypatch):
    """"quero pagar" nao pode virar consulta de catalogo."""
    from app.commerce.generic_catalog import GENERIC_BROWSE, classify_catalog_request

    for texto in ("quero pagar", "confirma o pedido", "qual o link de pagamento"):
        assert classify_catalog_request(texto).kind != GENERIC_BROWSE


def test_the_openai_route_also_answers_generic_browse_before_the_tool_loop():
    """Nem toda pergunta de catalogo e roteada como escopo comercial.

    "o que voces vendem?" chegava ao tool loop sem passar pelo fluxo de vendas e
    voltava `tools_request_failed`. Uma amostra do catalogo e deterministica: nao
    precisa de tool loop nenhum.
    """
    import inspect

    from app import openai_agent

    fonte = inspect.getsource(openai_agent.generate_agent_reply_async)
    assert "_generic_catalog_fast_path(message, only_browse=True)" in fonte
    assert fonte.index("_generic_catalog_fast_path") < fonte.index(
        "generate_openai_reply_async(message, customer_context, facts)"
    )
