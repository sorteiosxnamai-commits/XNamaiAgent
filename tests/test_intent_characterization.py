"""Characterization: comportamento de intencao ANTES da extracao do Intent Router.

Valores gravados do codigo existente (nao inventados). Qualquer diferenca apos a
extracao e regressao ou mudanca intencional — e mudanca intencional precisa ser
explicitada aqui, nao escondida como refactor.

Quirks preservados de proposito (caminho SEM LLM): "tem cupom de desconto?" cai
em product_search; "compara X e Y" cai em out_of_scope; "quero falar com um
atendente" e commerce. Com LLM essas mensagens vao pelo interpretador.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models import AgentResult, IncomingMessage, SalesInterpretation

# --- camada deterministica (sem LLM): dominio, plan intent, goal de fallback ----

DETERMINISTIC = [
    # (caso, texto, dominio, plan_intent, fallback_goal, primary_intent)
    ("buscar_produto", "tem carregador USB-C?", "commerce", "product_search", "find", "commerce"),
    ("preco", "quanto custa o carregador FKT-110C?", "commerce", "price", "inspect", "commerce"),
    ("disponibilidade", "tem estoque do cabo CB702V?", "commerce", "inventory", "inspect", "commerce"),
    ("detalhes", "quais as especificações do fone FKT-WS02?", "commerce", "product_search", "find", "commerce"),
    ("comparacao", "compara FKT-110C e FKT-1108", "out_of_scope", None, None, "general"),
    ("recomendacao", "me recomende um fone bluetooth até 100 reais", "commerce", "purchase_intent", "recommend", "commerce"),
    ("nao_encontrado", "tem o produto XYZ-999?", "commerce", "product_search", "find", "commerce"),
    ("continuar", "e o segundo?", "out_of_scope", None, None, "general"),
    ("corrigir", "não foi isso que eu perguntei, quero o preto", "commerce", None, None, "general"),
    ("ambigua", "esse aí", "out_of_scope", None, None, "general"),
    ("greeting", "oi", "greeting", None, None, "general"),
    ("suporte", "cadê meu pedido 12345", "store_general", None, None, "general"),
    ("out_of_scope", "quem ganhou o jogo ontem?", "out_of_scope", None, None, "general"),
    ("lead", "sou lojista e quero revender os produtos de vocês", "commerce", "product_search", "find", "commerce"),
    ("checkout", "quero finalizar a compra", "commerce", None, None, "general"),
    ("interesse_compra", "quero comprar um fone", "commerce", "purchase_intent", "buy", "commerce"),
    ("handoff", "quero falar com um atendente", "commerce", None, None, "human_support"),
    ("cupom", "tem cupom de desconto?", "commerce", "product_search", "find", "general"),
    ("loja", "qual o horário da loja?", "store_general", None, None, "general"),
]


@pytest.mark.parametrize(("case", "text", "domain", "plan_intent", "goal", "primary"), DETERMINISTIC,
                         ids=[row[0] for row in DETERMINISTIC])
def test_deterministic_intent_is_preserved(case, text, domain, plan_intent, goal, primary):
    from app.context_builder import detect_primary_intent
    from app.sales_agent import _fallback_interpretation, deterministic_scope

    scope = deterministic_scope(text)
    assert scope["domain"] == domain
    assert scope.get("intent") == plan_intent
    fallback = _fallback_interpretation(text)
    assert fallback.goal == goal
    assert fallback.domain == domain
    assert detect_primary_intent(text) == primary


# --- interpretador -> plan intent ----------------------------------------------


def _interp(**kw) -> SalesInterpretation:
    base = dict(domain="commerce", references_previous_context=False, needs_clarification=False, confidence=0.9)
    base.update(kw)
    return SalesInterpretation(**base)


INTERPRETER = [
    ("find", dict(goal="find", subject={"product_type": "carregador"}), "product_search"),
    ("recommend_budget", dict(goal="recommend", subject={"product_type": "fone"}, preferences={"budget_max": 100}), "recommendation"),
    ("recommend_no_signal", dict(goal="recommend", subject={"product_type": "fone"}), "recommendation"),
    ("compare", dict(goal="compare", subject={"model": "FKT-110C"}), "product_comparison"),
    ("inspect_price", dict(goal="inspect", subject={"model": "FKT-110C"}, information_needed=["price"]), "price"),
    ("inspect_payment", dict(goal="inspect", subject={"model": "FKT-110C"}, information_needed=["payment"]), "price"),
    ("inspect_inventory", dict(goal="inspect", subject={"model": "FKT-110C"}, information_needed=["inventory"]), "inventory"),
    ("inspect_coupon", dict(goal="inspect", information_needed=["coupons"]), "coupon"),
    ("inspect_catalog", dict(goal="inspect", subject={"model": "FKT-110C"}, information_needed=["catalog"]), "product_search"),
    ("buy", dict(goal="buy", subject={"model": "FKT-110C"}), "clarification"),
    ("buy_ready", dict(goal="buy", subject={"model": "FKT-110C"}, ready_for_retrieval=True), "recommendation"),
    ("discover", dict(goal="discover", subject={"product_type": "capa"}), "clarification"),
    ("discover_enough", dict(goal="discover", subject={"product_type": "capa"}, enough_information_to_search=True), "recommendation"),
    ("discover_stop", dict(goal="discover", stop_clarification=True), "recommendation"),
    ("after_sales", dict(goal="after_sales"), "clarification"),
    ("find_needs_clarification", dict(goal="find", needs_clarification=True), "clarification"),
    ("no_goal", dict(goal=None), "clarification"),
]


@pytest.mark.parametrize(("case", "fields", "intent"), INTERPRETER, ids=[row[0] for row in INTERPRETER])
def test_interpreter_output_maps_to_the_same_sales_intent(case, fields, intent):
    from app.sales_agent import interpretation_to_plan

    assert interpretation_to_plan(_interp(**fields), "x")["intent"] == intent


# --- plan intent -> acao comercial / esclarecimento / erros ---------------------


@pytest.fixture
def commerce_spy(monkeypatch):
    import app.sales_agent as sales_agent

    monkeypatch.setattr(sales_agent, "get_settings", lambda: SimpleNamespace(
        openai_api_key="", openai_model="m", agent_db_persona_enabled=False))
    calls: list[str] = []
    reply = {"result": AgentResult(reply_text="ok", intent="commerce",
                                   commercial_data={"products": [{"id": "1", "name": "Cabo"}]})}

    async def fake_commerce(message, facts, customer_context, *, action=None, query=None):
        calls.append(action)
        return reply["result"]

    async def no_responder(*_a, **_k):
        return None

    monkeypatch.setattr(sales_agent, "handle_commerce_message", fake_commerce)
    monkeypatch.setattr(sales_agent, "_sales_response_with_openai", no_responder)
    return calls, reply


async def _run(plan: dict):
    import app.sales_agent as sales_agent

    return await sales_agent.handle_sales_message(
        IncomingMessage(text="mensagem"), {}, {}, semantic_plan={"domain": "commerce", "query": "cabo usb", **plan},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_intent", "action"),
    [
        ("product_search", "product_search"),
        ("recommendation", "product_search"),
        ("product_comparison", "product_search"),
        ("price", "product_price"),
        ("inventory", "product_inventory"),
        ("coupon", "coupon_search"),
    ],
)
async def test_sales_intent_maps_to_the_same_commerce_action(commerce_spy, plan_intent, action):
    calls, _ = commerce_spy
    await _run({"intent": plan_intent})
    assert calls[0] == action


@pytest.mark.asyncio
async def test_clarification_intent_asks_without_touching_commerce(commerce_spy):
    calls, _ = commerce_spy
    result = await _run({"intent": "clarification", "clarification_question": "Qual o modelo?"})
    assert calls == []
    assert result.safety_reason == "commerce_clarification"
    assert result.reply_text == "Qual o modelo?"


@pytest.mark.asyncio
async def test_intent_without_commerce_action_is_not_routed(commerce_spy):
    """purchase_intent sem interpretacao nao tem acao comercial: o turno segue adiante (None)."""
    calls, _ = commerce_spy
    assert await _run({"intent": "purchase_intent"}) is None
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "safety_reason",
    ["commerce_provider_unavailable", "commerce_timeout", "partial_catalog"],
    ids=["provider_indisponivel", "timeout", "parcial"],
)
async def test_provider_errors_are_passed_through_not_reclassified(commerce_spy, safety_reason):
    """Erro do provider nao vira outra intencao nem 'nao encontrado'."""
    calls, reply = commerce_spy
    reply["result"] = AgentResult(reply_text="Não consegui consultar agora.", intent="commerce",
                                  safety_reason=safety_reason)
    result = await _run({"intent": "product_search"})
    assert result.safety_reason == safety_reason
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_forced_retrieval_turns_clarification_into_recommendation(monkeypatch, commerce_spy):
    """Cliente pediu para agir: a intencao de esclarecer vira busca."""
    import app.sales_agent as sales_agent

    calls, _ = commerce_spy
    # O planner entrega "clarification" (needs_clarification, sem sinal de busca);
    # so o override por force_retrieval pode transforma-lo em busca.
    interpretation = _interp(goal="discover", subject={"product_type": "capa"}, needs_clarification=True)
    assert sales_agent.interpretation_to_plan(interpretation, "x")["intent"] == "clarification"
    monkeypatch.setattr(sales_agent, "_discovery_state",
                        lambda *_a, **_k: {"force_retrieval": True, "clarification_count": 0,
                                           "enough_information_to_search": True, "ready_for_retrieval": True,
                                           "stop_clarification": True, "known_preferences_count": 0,
                                           "subject_identifiable": True})

    async def compiled(_interpretation):
        calls.append("compiled_retrieval")
        return AgentResult(reply_text="ok", intent="commerce", commercial_data={"products": [{"id": "1"}]})

    monkeypatch.setattr(sales_agent, "_execute_compiled_product_retrieval", compiled)
    await sales_agent.handle_sales_message(IncomingMessage(text="me mostra as opções"), {}, {},
                                           semantic_plan=interpretation)
    assert "compiled_retrieval" in calls
