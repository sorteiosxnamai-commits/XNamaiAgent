import json
from types import SimpleNamespace

import pytest

from app.models import AgentResult, IncomingMessage, SalesInterpretation
from openai_test_utils import install_fake_openai_client


def _settings(*, api_key: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        openai_api_key=api_key,
        openai_model="gpt-4.1-mini",
        openai_main_model="gpt-4.1-mini",
        openai_fast_model="gpt-4.1-nano",
        agent_turn_understanding_enabled=False,
    )


def _interpretation(
    *,
    product_type: str = "acessório",
    preferences: dict | None = None,
    goal: str = "discover",
    needs_clarification: bool = True,
    enough: bool = False,
    ready: bool = False,
    stop: bool = False,
    clarification_question: str | None = "Qual preferência é mais importante para você?",
) -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal=goal,
        subject={"product_type": product_type},
        preferences=preferences or {},
        information_needed=["catalog"],
        references_previous_context=True,
        enough_information_to_search=enough,
        ready_for_retrieval=ready,
        stop_clarification=stop,
        needs_clarification=needs_clarification,
        clarification_question=clarification_question,
        confidence=0.98,
    )


def _clarification_turn(content: str) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "metadata": {"safety_reason": "commerce_clarification"},
    }


async def _run_sales(monkeypatch, interpretation, recent_turns):
    import app.sales_agent as sales_agent

    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories":
            return {"categories": []}
        if name == "get_product":
            return {
                "id": arguments["product_id"],
                "name": f"{interpretation.subject.product_type} recomendado",
                "current_price": 1000,
            }
        return {
            "products": [{"id": "1", "name": f"{interpretation.subject.product_type} recomendado", "current_price": 1000}]
        }

    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())
    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="continuação comercial"),
        {"primary_intent": "commerce"},
        {},
        interpretation,
        recent_turns=recent_turns,
    )
    return result, calls


@pytest.mark.asyncio
async def test_python_does_not_force_retrieval_after_a_fixed_question_count(monkeypatch):
    interpretation = _interpretation()

    first, first_calls = await _run_sales(monkeypatch, interpretation, [])
    assert first.safety_reason == "commerce_clarification"
    assert first_calls == []

    one_turn = [
        {"role": "user", "content": "quero uma opção"},
        _clarification_turn("Tem alguma faixa de preço em mente?"),
        {"role": "user", "content": "não sei"},
    ]
    second, second_calls = await _run_sales(monkeypatch, interpretation, one_turn)
    assert second.safety_reason == "commerce_clarification"
    assert second_calls == []

    two_turns = [
        *one_turn,
        _clarification_turn("Existe alguma característica essencial?"),
        {"role": "user", "content": "tanto faz"},
    ]
    third, third_calls = await _run_sales(monkeypatch, interpretation, two_turns)
    assert third.safety_reason == "commerce_clarification"
    assert third_calls == []


@pytest.mark.asyncio
async def test_interpreter_can_mark_context_with_budget_as_enough_to_search(monkeypatch):
    interpretation = _interpretation(
        preferences={"budget_max": 10000},
        enough=True,
    )

    result, calls = await _run_sales(monkeypatch, interpretation, [])

    assert [call for call in calls if call[0] == "search_products"] == [("search_products", {"name": "acessório", "available": True, "available_in_store": True, "limit": 20, "page": 1})]
    assert result.response_metadata["used_tray"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("ready,stop", [(True, False), (False, True)])
async def test_action_or_friction_stops_clarification_when_subject_is_known(monkeypatch, ready, stop):
    interpretation = _interpretation(ready=ready, stop=stop)

    result, calls = await _run_sales(monkeypatch, interpretation, [])

    assert [call for call in calls if call[0] == "search_products"] == [("search_products", {"name": "acessório", "available": True, "available_in_store": True, "limit": 20, "page": 1})]
    assert result.safety_reason != "commerce_clarification"


def test_explicit_no_preference_is_not_an_unknown_question_candidate():
    import app.sales_agent as sales_agent

    interpretation = _interpretation(
        preferences={"explicit_no_preferences": ["color"]},
    )
    state = sales_agent._discovery_state(interpretation, [])

    assert state["explicit_no_preferences"] == ["color"]
    assert "color" not in state["unknown_preferences"]
    assert state["enough_information_to_search"] is False


@pytest.mark.asyncio
async def test_clarification_receives_known_preferences_and_recent_questions(monkeypatch):
    import app.sales_agent as sales_agent

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Tem uma faixa de preço em mente?"))]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    recent_turns = [
        _clarification_turn("Tem preferência de material?"),
        {"role": "user", "content": "material natural"},
    ]
    interpretation = _interpretation(
        preferences={"material": "material natural"},
        clarification_question=None,
    )
    state = sales_agent._discovery_state(interpretation, recent_turns)
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings(api_key="test-key"))
    install_fake_openai_client(monkeypatch, FakeClient)

    await sales_agent.generate_clarification_reply(
        message=IncomingMessage(text="material natural"),
        interpretation=interpretation,
        recent_turns=recent_turns,
        discovery_state=state,
    )

    request_payload = json.loads(captured["messages"][-1]["content"])
    discovery = request_payload["DISCOVERY_STATE"]
    assert discovery["known_preferences"]["material"] == "material natural"
    assert "material" not in discovery["unknown_preferences"]
    assert discovery["recent_questions"] == ["Tem preferência de material?"]


@pytest.mark.asyncio
async def test_structured_clarification_question_avoids_second_openai_call(monkeypatch):
    import app.sales_agent as sales_agent

    interpretation = _interpretation(
        clarification_question="Tem uma faixa de preço e um estilo em mente?",
    )
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings(api_key="test-key"))

    async def forbid_openai(**kwargs):
        raise AssertionError("structured question must be reused")

    monkeypatch.setattr(
        "app.openai_gateway.parse_structured_output",
        forbid_openai,
    )
    monkeypatch.setattr(
        "app.openai_gateway.generate_text_output",
        forbid_openai,
    )

    result = await sales_agent.generate_clarification_reply(
        message=IncomingMessage(text="quero uma opção"),
        interpretation=interpretation,
    )

    assert result.reply_text == "Tem uma faixa de preço e um estilo em mente?"
    assert result.response_metadata["response_source"] == "openai"
    assert result.response_metadata["used_openai_responder"] is False


@pytest.mark.asyncio
async def test_latest_explicit_preference_from_interpreter_is_preserved(monkeypatch):
    import app.sales_agent as sales_agent

    interpretation = _interpretation(
        preferences={"style": "digital", "attributes": []},
        goal="recommend",
        needs_clarification=False,
        enough=True,
    )

    class FakeCompletions:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=interpretation, refusal=None))]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings(api_key="test-key"))
    install_fake_openai_client(monkeypatch, FakeClient)

    result = await sales_agent.interpret_message(
        IncomingMessage(text="prefiro digital"),
        recent_turns=[
            {"role": "user", "content": "prefiro analógico"},
            _clarification_turn("Quer manter essa preferência?"),
        ],
    )

    assert result.preferences.style == "digital"
    assert "analógico" not in result.preferences.attributes


@pytest.mark.asyncio
async def test_catalog_request_interpretation_reaches_retrieval_without_clarification(monkeypatch):
    interpretation = _interpretation(ready=True, needs_clarification=False, goal="recommend")

    result, calls = await _run_sales(monkeypatch, interpretation, [])

    assert [call for call in calls if call[0] == "search_products"] == [("search_products", {"name": "acessório", "available": True, "available_in_store": True, "limit": 20, "page": 1})]
    assert result.safety_reason != "commerce_clarification"
