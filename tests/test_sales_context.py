from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.models import IncomingMessage, SalesInterpretation
from openai_test_utils import install_fake_openai_client


def _settings(*, api_key: str = "test-key") -> SimpleNamespace:
    return SimpleNamespace(
        openai_api_key=api_key,
        openai_model="gpt-test",
        openai_main_model="gpt-test",
        openai_fast_model="gpt-test",
        database_url="postgresql://test",
        agent_turn_understanding_enabled=False,
    )


def _fake_openai(monkeypatch, interpretation: SalesInterpretation, captured: dict) -> None:
    class FakeCompletions:
        async def parse(self, **kwargs):
            captured.update(kwargs)
            message = SimpleNamespace(parsed=interpretation, refusal=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    install_fake_openai_client(monkeypatch, FakeClient)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_text", "history", "interpretation", "expected_style", "expected_budget"),
    [
        (
            "esportivo",
            [
                {"role": "user", "content": "quero comprar um relógio"},
                {"role": "assistant", "content": "Você procura algo esportivo, social ou casual?"},
            ],
            SalesInterpretation(
                domain="commerce",
                goal="discover",
                subject={"product_type": "relógio"},
                preferences={"style": "esportivo"},
                references_previous_context=True,
                needs_clarification=False,
                confidence=0.98,
            ),
            "esportivo",
            None,
        ),
        (
            "menos de 5 mil reais",
            [
                {"role": "user", "content": "quero um relógio esportivo"},
                {"role": "assistant", "content": "Qual faixa de preço você prefere?"},
            ],
            SalesInterpretation(
                domain="commerce",
                goal="recommend",
                subject={"product_type": "relógio"},
                preferences={"style": "esportivo", "budget_max": 5000},
                references_previous_context=True,
                needs_clarification=False,
                confidence=0.98,
            ),
            "esportivo",
            5000,
        ),
        (
            "social",
            [
                {"role": "user", "content": "me recomende relógios"},
                {"role": "assistant", "content": "Qual estilo você prefere?"},
            ],
            SalesInterpretation(
                domain="commerce",
                goal="recommend",
                subject={"product_type": "relógio"},
                preferences={"style": "social"},
                references_previous_context=True,
                needs_clarification=False,
                confidence=0.97,
            ),
            "social",
            None,
        ),
    ],
)
async def test_interpreter_uses_recent_turns_for_short_followups(
    monkeypatch,
    current_text,
    history,
    interpretation,
    expected_style,
    expected_budget,
):
    import app.sales_agent as sales_agent

    captured = {}
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())
    _fake_openai(monkeypatch, interpretation, captured)

    result = await sales_agent.interpret_message(
        IncomingMessage(text=current_text),
        recent_turns=history,
    )

    assert result.domain == "commerce"
    assert result.subject.product_type == "relógio"
    assert result.preferences.style == expected_style
    assert result.preferences.budget_max == expected_budget
    assert result.references_previous_context is True
    assert captured["messages"][2:-1] == history
    assert captured["messages"][1]["content"].startswith("COMMERCE_STATE:")
    assert captured["messages"][-1] == {"role": "user", "content": current_text}
    assert captured["response_format"] is SalesInterpretation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "interpretation", "expected_domain"),
    [
        ("quem ganhou o jogo ontem?", SalesInterpretation(domain="out_of_scope", references_previous_context=False, needs_clarification=False, confidence=0.99), "out_of_scope"),
        ("como funciona o sorteio?", SalesInterpretation(domain="raffle", references_previous_context=False, needs_clarification=False, confidence=0.99), "raffle"),
        (
            "preciso de um relógio para dar de presente, não queria gastar muito",
            SalesInterpretation(
                domain="commerce",
                goal="discover",
                subject={"product_type": "relógio"},
                preferences={"occasion": "presente"},
                references_previous_context=False,
                needs_clarification=True,
                clarification_question="Qual faixa de preço você tem em mente?",
                confidence=0.96,
            ),
            "commerce",
        ),
    ],
)
async def test_interpreter_returns_validated_domains(monkeypatch, text, interpretation, expected_domain):
    import app.sales_agent as sales_agent

    captured = {}
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())
    _fake_openai(monkeypatch, interpretation, captured)

    result = await sales_agent.interpret_message(IncomingMessage(text=text))

    assert result.domain == expected_domain
    if result.needs_clarification:
        assert result.clarification_question


@pytest.mark.asyncio
async def test_greeting_uses_fast_local_interpretation_without_openai(monkeypatch):
    import app.sales_agent as sales_agent

    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())

    async def forbid_parse(**kwargs):
        raise AssertionError("greeting must not call OpenAI")

    monkeypatch.setattr(
        "app.openai_gateway.parse_structured_output",
        forbid_parse,
    )

    result = await sales_agent.interpret_message(IncomingMessage(text="oi"))

    assert result.domain == "greeting"


@pytest.mark.asyncio
async def test_openai_is_attempted_before_deterministic_fallback(monkeypatch):
    import app.sales_agent as sales_agent

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"product_type": "relógio"},
        preferences={"style": "esportivo"},
        references_previous_context=True,
        needs_clarification=False,
        confidence=0.95,
    )
    captured = {}
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())
    _fake_openai(monkeypatch, interpretation, captured)

    result = await sales_agent.interpret_message(
        IncomingMessage(text="esportivo"),
        recent_turns=[{"role": "user", "content": "quero um relógio"}],
    )

    assert captured
    assert result.domain == "commerce"
    assert result._source == "openai"


def test_load_recent_conversation_turns_prefers_conversation_and_delivered_replies(monkeypatch):
    import app.db as db

    captured = {}
    rows = [
        {"id": 12, "text": "menos de 5 mil", "reply_text": None, "safety_reason": None},
        {
            "id": 10,
            "text": "quero comprar um relógio",
            "reply_text": "Qual estilo você prefere?",
            "safety_reason": "commerce_clarification",
        },
    ]

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, query, params):
            captured.setdefault("queries", []).append(query)
            captured.setdefault("params_list", []).append(params)
            captured["query"] = query
            captured["params"] = params

        def fetchall(self):
            if "conversation_id" in captured.get("params", {}):
                return rows
            return []

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    @contextmanager
    def fake_get_conn():
        yield FakeConnection()

    monkeypatch.setattr(db, "get_settings", lambda: _settings())
    monkeypatch.setattr(db, "get_conn", fake_get_conn)
    monkeypatch.setattr(
        "app.customer_identity.resolve_linked_identity_candidates",
        lambda **_kwargs: [],
    )

    turns = db.load_recent_conversation_turns(
        conversation_id="conversation-1",
        sender_phone="5511999999999",
        before_inbound_id=20,
        limit=8,
        sender_key="whatsapp:5511999999999",
    )

    assert turns == [
        {"role": "user", "content": "quero comprar um relógio"},
        {
            "role": "assistant",
            "content": "Qual estilo você prefere?",
            "metadata": {"safety_reason": "commerce_clarification"},
        },
        {"role": "user", "content": "menos de 5 mil"},
    ]
    assert any(
        params.get("conversation_id") == "conversation-1"
        for params in captured["params_list"]
    )
    assert any(
        params.get("before_inbound_id") == 20 for params in captured["params_list"]
    )
    assert any("provider_send_ok = true" in query for query in captured["queries"])
    assert any("response.safety_reason" in query for query in captured["queries"])
    assert any(
        "inbound.id < %(before_inbound_id)s" in query for query in captured["queries"]
    )


@pytest.mark.asyncio
async def test_async_agent_passes_loaded_history_to_interpreter(monkeypatch):
    import app.openai_agent as openai_agent
    from app.models import AgentResult

    history = [
        {"role": "user", "content": "quero comprar um relógio"},
        {"role": "assistant", "content": "Você prefere esportivo ou social?"},
    ]
    captured = {}
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"product_type": "relógio"},
        preferences={"style": "esportivo"},
        references_previous_context=True,
        needs_clarification=True,
        clarification_question="Qual faixa de preço você prefere?",
        confidence=0.98,
    )

    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **kwargs: history)

    async def fake_interpret(message, *, recent_turns=None, commerce_state=None):
        captured["history"] = recent_turns
        captured["commerce_state"] = commerce_state
        return interpretation

    async def fake_handle(message, facts, customer_context, semantic_plan, recent_turns=None, commerce_state=None):
        captured["plan"] = semantic_plan
        captured["sales_history"] = recent_turns
        captured["sales_state"] = commerce_state
        return AgentResult(reply_text="Qual faixa de preço você prefere?", intent="commerce")

    monkeypatch.setattr(openai_agent, "interpret_message", fake_interpret)
    monkeypatch.setattr(openai_agent, "handle_sales_message", fake_handle)
    monkeypatch.setattr(
        openai_agent,
        "deterministic_scope",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("valid OpenAI interpretation must not be reclassified")),
    )

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(
            text="esportivo",
            conversation_id="conversation-1",
            sender_phone="5511999999999",
            raw={"inbound_id": 30},
        ),
        {},
    )

    assert result.intent == "commerce"
    assert result.reply_text != openai_agent.OUT_OF_SCOPE_REPLY
    assert captured["history"] == history
    assert captured["sales_history"] == history
    assert captured["plan"] is interpretation
