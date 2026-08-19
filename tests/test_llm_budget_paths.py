from types import SimpleNamespace

import pytest

from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.openai_runtime import execute_openai_call_sync
from app.runtime_context import reset_current_turn, set_current_turn
from app.turn_runtime import (
    LLMCallBudget,
    LLMCallBudgetExceeded,
    TurnRuntimeContext,
)


def _response():
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)
    )


def _turn(*, max_calls: int, enforce: bool = True) -> TurnRuntimeContext:
    return TurnRuntimeContext(
        trace_id="budget-test",
        llm_budget=LLMCallBudget(max_calls=max_calls, enforce=enforce),
    )


def test_budget_zero_blocks_all_external_calls_and_records_avoided():
    context = _turn(max_calls=0)
    external = {"n": 0}

    def operation():
        external["n"] += 1
        return _response()

    token = set_current_turn(context)
    try:
        with pytest.raises(LLMCallBudgetExceeded):
            execute_openai_call_sync(
                call_type="decision",
                operation=operation,
                reason="interpret",
            )
    finally:
        reset_current_turn(token)

    assert external["n"] == 0
    assert context.openai_call_count == 0
    assert context.llm_calls_avoided == 1
    assert context.fallback_reasons == ["llm_budget_exceeded"]


def test_budget_one_allows_single_call():
    context = _turn(max_calls=1)
    token = set_current_turn(context)
    try:
        execute_openai_call_sync(
            call_type="decision",
            operation=_response,
            reason="interpret_only",
        )
        with pytest.raises(LLMCallBudgetExceeded):
            execute_openai_call_sync(
                call_type="response_composition",
                operation=_response,
                reason="compose",
            )
    finally:
        reset_current_turn(token)

    assert context.openai_call_count == 1
    assert context.llm_calls_by_type == {"decision": 1}
    assert context.llm_call_reasons[0]["reason"] == "interpret_only"
    assert context.llm_calls_avoided == 1


def test_budget_two_allows_interpret_and_compose():
    context = _turn(max_calls=2)
    token = set_current_turn(context)
    try:
        execute_openai_call_sync(call_type="decision", operation=_response)
        execute_openai_call_sync(
            call_type="response_composition",
            operation=_response,
        )
        with pytest.raises(LLMCallBudgetExceeded):
            execute_openai_call_sync(call_type="judge", operation=_response)
    finally:
        reset_current_turn(token)

    assert context.openai_call_count == 2
    assert context.execution_path == "complex"
    assert context.llm_calls_avoided == 1


def test_budget_three_allows_selective_validation_slot():
    context = _turn(max_calls=3)
    token = set_current_turn(context)
    try:
        for call_type in ("decision", "response_composition", "judge"):
            execute_openai_call_sync(call_type=call_type, operation=_response)
        with pytest.raises(LLMCallBudgetExceeded):
            execute_openai_call_sync(call_type="legacy", operation=_response)
    finally:
        reset_current_turn(token)

    assert context.openai_call_count == 3
    assert set(context.llm_calls_by_type) == {
        "decision",
        "response_composition",
        "judge",
    }


def test_budget_exceeded_does_not_count_as_openai_integration_failure():
    context = _turn(max_calls=0)
    token = set_current_turn(context)
    try:
        with pytest.raises(LLMCallBudgetExceeded):
            execute_openai_call_sync(call_type="decision", operation=_response)
    finally:
        reset_current_turn(token)

    assert context.integration_failures == {}


@pytest.mark.asyncio
async def test_deterministic_greeting_records_zero_llm_avoided(monkeypatch):
    import app.openai_agent as openai_agent

    context = _turn(max_calls=3)
    token = set_current_turn(context)
    try:
        monkeypatch.setattr(
            openai_agent,
            "get_settings",
            lambda: SimpleNamespace(
                agent_history_limit=12,
                agent_history_hard_cap=80,
                openai_api_key="k",
                tray_adapter_url="https://tray.example",
                tray_adapter_token="t",
                agent_image_search_enabled=False,
                max_reply_chars=900,
            ),
        )
        monkeypatch.setattr(openai_agent, "detect_blocked_request", lambda _t: None)
        monkeypatch.setattr(
            openai_agent,
            "should_request_human_handoff",
            lambda _m: None,
        )
        monkeypatch.setattr(
            openai_agent,
            "load_recent_conversation_turns",
            lambda **_k: [],
        )
        monkeypatch.setattr(openai_agent, "extract_order_reference", lambda _t: None)
        monkeypatch.setattr(openai_agent, "extract_valid_tax_document", lambda _t: None)
        monkeypatch.setattr(
            openai_agent,
            "contains_tax_document_candidate",
            lambda _t: False,
        )
        monkeypatch.setattr(
            openai_agent,
            "extract_handles_from_conversation",
            lambda **_k: {},
        )
        monkeypatch.setattr(
            openai_agent,
            "hydrate_state_from_handles",
            lambda state, _h: state,
        )
        monkeypatch.setattr(openai_agent, "is_order_lookup_request", lambda _t: False)
        monkeypatch.setattr(openai_agent, "is_payment_link_request", lambda _t: False)
        monkeypatch.setattr(
            openai_agent,
            "is_unpaid_order_resume_request",
            lambda _t: False,
        )
        monkeypatch.setattr(openai_agent, "should_resume_pending_order", lambda *_a, **_k: False)
        monkeypatch.setattr(openai_agent, "has_resumable_commerce", lambda _s: True)
        monkeypatch.setattr(openai_agent, "_is_greeting", lambda _t: True)
        monkeypatch.setattr(openai_agent, "is_soft_greeting", lambda _t: True)
        monkeypatch.setattr(
            openai_agent,
            "build_contextual_greeting",
            lambda _s: AgentResult(
                reply_text="Oi! Posso ajudar com seu pedido.",
                intent="general",
                response_metadata={"domain": "greeting", "response_source": "context_resume_soft"},
            ),
        )

        async def boom(*_a, **_k):
            raise AssertionError("interpret must not run for soft greeting resume")

        monkeypatch.setattr(openai_agent, "interpret_message", boom)

        result = await openai_agent.generate_agent_reply_async(
            IncomingMessage(text="oi", conversation_id="c1"),
            {"_commerce_state": {}},
        )
    finally:
        reset_current_turn(token)

    assert "Oi" in result.reply_text
    assert result.response_metadata.get("used_openai_interpreter") is False
    assert result.response_metadata.get("used_openai_responder") is False
    assert context.openai_call_count == 0
    assert context.llm_calls_avoided >= 2
    assert any(
        item["reason"].startswith("path:") for item in context.llm_avoided_reasons
    )


@pytest.mark.asyncio
async def test_handoff_path_is_zero_llm(monkeypatch):
    import app.openai_agent as openai_agent

    context = _turn(max_calls=3)
    token = set_current_turn(context)
    try:
        monkeypatch.setattr(openai_agent, "detect_blocked_request", lambda _t: None)
        monkeypatch.setattr(
            openai_agent,
            "should_request_human_handoff",
            lambda _m: "customer_requested_human",
        )

        result = await openai_agent.generate_agent_reply_async(
            IncomingMessage(text="quero falar com atendente", conversation_id="c1"),
            {},
        )
    finally:
        reset_current_turn(token)

    assert result.handoff_required is True
    assert context.openai_call_count == 0
    assert context.llm_calls_avoided >= 2


@pytest.mark.asyncio
async def test_budget_exceeded_on_clarification_uses_deterministic_fallback(monkeypatch):
    import app.openai_gateway as openai_gateway
    import app.sales_agent as sales_agent

    context = _turn(max_calls=0)
    token = set_current_turn(context)
    try:
        monkeypatch.setattr(
            sales_agent,
            "get_settings",
            lambda: SimpleNamespace(
                openai_api_key="k",
                openai_model="gpt-4.1-mini",
                max_reply_chars=900,
            ),
        )

        async def fail_compose(*_a, **_k):
            raise LLMCallBudgetExceeded("llm_call_budget_exceeded:clarification")

        monkeypatch.setattr(openai_gateway, "generate_text_output", fail_compose)

        interpretation = SalesInterpretation(
            domain="commerce",
            goal="discover",
            subject={},
            preferences={},
            references_previous_context=False,
            needs_clarification=True,
            clarification_question="Qual modelo você procura?",
            confidence=0.5,
        )
        interpretation._source = "fallback"
        result = await sales_agent.generate_clarification_reply(
            message=IncomingMessage(text="relógio", conversation_id="c1"),
            interpretation=interpretation,
            recent_turns=[],
            used_tray=False,
        )
    finally:
        reset_current_turn(token)

    assert result.response_metadata.get("response_source") == "deterministic_fallback"
    assert "modelo" in result.reply_text.lower()
