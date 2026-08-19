"""Honest offline replay harness (Etapa 11).

Runs the real agent entrypoint with fakes. Observations come only from the
AgentResult / tool stubs — never from ``expected``.
"""

from __future__ import annotations

from typing import Any

from app.models import IncomingMessage
from app.turn_runtime import LLMCallBudget, TurnRuntimeContext
from app.runtime_context import set_current_turn, reset_current_turn


def observations_from_result(
    result: Any,
    *,
    tools_called: list[str] | None = None,
    openai_calls: int = 0,
) -> dict[str, Any]:
    metadata = dict(getattr(result, "response_metadata", None) or {})
    domain = metadata.get("domain") or getattr(result, "intent", None)
    validation = metadata.get("factual_validation") or {}
    invented = bool(
        validation.get("fallback_applied")
        or getattr(result, "safety_reason", None) == "factual_validation_failed"
    )
    return {
        "observed_domain": domain,
        "observed_tools": list(tools_called or []),
        "reply_text": getattr(result, "reply_text", None) or "",
        "openai_calls": int(openai_calls),
        "factual_valid": validation.get("valid", True) is not False,
        "handoff_required": bool(getattr(result, "handoff_required", False)),
        "invented_claim": invented,
        "intent": getattr(result, "intent", None),
        "safety_reason": getattr(result, "safety_reason", None),
    }


async def run_replay_case(
    case: dict[str, Any],
    monkeypatch: Any,
) -> dict[str, Any]:
    """Execute one fixture case through ``generate_agent_reply_async``."""
    from app.config import get_settings
    from app.openai_agent import generate_agent_reply_async
    import app.openai_agent as openai_agent
    import app.sales_agent as sales_agent

    get_settings.cache_clear()
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("TRAY_ADAPTER_URL", "")
    monkeypatch.setenv("TRAY_ADAPTER_TOKEN", "")
    get_settings.cache_clear()

    monkeypatch.setattr(
        openai_agent,
        "load_recent_conversation_turns",
        lambda **_kwargs: list(case.get("history") or []),
    )

    tools_called: list[str] = []
    fixtures = case.get("tool_fixtures") or {}

    async def fake_execute(name: str, arguments: dict) -> dict:
        tools_called.append(str(name))
        payload = fixtures.get(name)
        if payload is None:
            return {
                "success": False,
                "error": "fixture_missing",
                "products": [],
            }
        if isinstance(payload, dict):
            return dict(payload)
        return {"success": True, "products": list(payload)}

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    try:
        import app.tray_tools as tray_tools

        monkeypatch.setattr(tray_tools, "execute_tool", fake_execute)
    except Exception:
        pass
    try:
        import app.product_retrieval as product_retrieval

        if hasattr(product_retrieval, "execute_tool"):
            monkeypatch.setattr(product_retrieval, "execute_tool", fake_execute)
    except Exception:
        pass

    incoming = IncomingMessage(
        channel=str(case.get("channel") or "whatsapp"),
        text=str(case.get("input") or ""),
        sender_phone=str(case.get("sender_phone") or "5511999999999"),
        sender_key=case.get("sender_key"),
        conversation_id=case.get("conversation_id"),
    )
    customer_context = {"found": False}
    initial_state = case.get("initial_state") or {}
    if initial_state:
        customer_context["_commerce_state"] = initial_state

    turn = TurnRuntimeContext(
        trace_id=f"eval-{case.get('id') or 'case'}",
        llm_budget=LLMCallBudget(max_calls=6, enforce=False),
    )
    token = set_current_turn(turn)
    try:
        result = await generate_agent_reply_async(incoming, customer_context)
    finally:
        reset_current_turn(token)

    obs = observations_from_result(
        result,
        tools_called=tools_called,
        openai_calls=turn.openai_call_count,
    )
    obs["case_id"] = case.get("id")
    return obs
