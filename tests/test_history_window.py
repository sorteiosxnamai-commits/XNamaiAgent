from types import SimpleNamespace

import pytest

from app.history_window import count_user_assistant_turns, select_model_history_turns
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def test_select_model_history_turns_keeps_newest_only():
    turns = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    selected = select_model_history_turns(turns, limit=12)
    assert len(selected) == 12
    assert selected[0]["content"] == "m8"
    assert selected[-1]["content"] == "m19"


def test_select_model_history_turns_empty_and_zero():
    assert select_model_history_turns(None, limit=12) == []
    assert select_model_history_turns([{"role": "user", "content": "a"}], limit=0) == []


def test_count_user_assistant_turns():
    counts = count_user_assistant_turns(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
    )
    assert counts == {"total": 3, "user": 2, "assistant": 1}


@pytest.mark.asyncio
async def test_agent_loads_hard_cap_but_sends_model_window(monkeypatch):
    import app.openai_agent as openai_agent

    captured = {}

    def fake_load(**kwargs):
        captured["load"] = kwargs
        return [{"role": "user", "content": f"t{i}"} for i in range(40)]

    async def fake_interpret(message, recent_turns=None, commerce_state=None):
        captured["interpret_turns"] = len(recent_turns or [])
        return SalesInterpretation(
            domain="commerce",
            goal="find",
            subject={"brand": "Tissot", "model": "Seastar"},
            preferences={},
            references_previous_context=False,
            needs_clarification=False,
            clarification_question=None,
            confidence=0.9,
        )

    async def fake_sales(
        message,
        facts,
        customer_context,
        interpretation,
        recent_turns=None,
        commerce_state=None,
    ):
        captured["sales_turns"] = len(recent_turns or [])
        captured["model_ctx"] = len(customer_context.get("_model_conversation_turns") or [])
        captured["ops_ctx"] = len(customer_context.get("_conversation_turns") or [])
        return AgentResult(reply_text="produto", intent="commerce")

    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", fake_load)
    monkeypatch.setattr(openai_agent, "interpret_message", fake_interpret)
    monkeypatch.setattr(openai_agent, "handle_sales_message", fake_sales)
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
    monkeypatch.setattr(openai_agent, "_is_greeting", lambda _t: False)
    monkeypatch.setattr(openai_agent, "is_soft_greeting", lambda _t: False)
    monkeypatch.setattr(openai_agent, "is_order_lookup_request", lambda _t: False)
    monkeypatch.setattr(openai_agent, "is_payment_link_request", lambda _t: False)
    monkeypatch.setattr(openai_agent, "is_unpaid_order_resume_request", lambda _t: False)
    monkeypatch.setattr(openai_agent, "extract_order_reference", lambda _t: None)
    monkeypatch.setattr(openai_agent, "extract_valid_tax_document", lambda _t: None)
    monkeypatch.setattr(openai_agent, "contains_tax_document_candidate", lambda _t: False)
    monkeypatch.setattr(
        openai_agent,
        "extract_handles_from_conversation",
        lambda **_k: {},
    )
    monkeypatch.setattr(
        openai_agent,
        "hydrate_state_from_handles",
        lambda state, _handles: state,
    )
    monkeypatch.setattr(openai_agent, "should_resume_pending_order", lambda *_a, **_k: False)

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(
            text="Tem Tissot Seastar?",
            conversation_id="c1",
            raw={"inbound_id": 9},
        ),
        {"_commerce_state": {}},
    )

    assert captured["load"]["limit"] == 80
    assert captured["load"]["hard_cap"] == 80
    assert captured["interpret_turns"] == 12
    assert captured["sales_turns"] == 12
    assert captured["model_ctx"] == 12
    assert captured["ops_ctx"] == 40
    assert result.reply_text == "produto"
