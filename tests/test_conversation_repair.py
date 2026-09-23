from unittest.mock import AsyncMock

import pytest

from app.commerce.conversation_repair import recover_conversation, repair_request
from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.sales_agent import _render_commerce_turn


@pytest.mark.parametrize("text", ["Não foi isso que perguntei", "não perguntei o preço", "você não entendeu", "ta entendendo nada"])
def test_recognizes_corrections_without_treating_price_as_a_new_question(text):
    assert repair_request(text)[0]
    assert not repair_request("qual o preço desse cabo?")[0]


@pytest.mark.asyncio
async def test_repeated_misunderstanding_escalates_without_repeating_mutations():
    state = CommerceConversationState(pending_commerce_action="confirm_order", last_catalog_query="confirmo")
    execute = AsyncMock(side_effect=AssertionError("no commercial mutation"))
    first = await recover_conversation("não foi isso que perguntei", state=state, execute=execute, render=_render_commerce_turn)
    assert not first.handoff_required
    assert first.safety_reason == "conversation_repair_clarification"
    state = evolve_commerce_state(state, first)
    assert state.pending_commerce_action is None
    second = await recover_conversation("você não entendeu", state=state, execute=execute, render=_render_commerce_turn)
    assert second.handoff_required
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_context_asks_once_without_inventing_a_product():
    state = CommerceConversationState()
    execute = AsyncMock()
    result = await recover_conversation("não perguntei o preço", state=state, execute=execute, render=_render_commerce_turn)
    assert result.safety_reason == "conversation_repair_missing_context"
    assert not result.handoff_required
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_repair_reuses_original_catalog_query_and_preserves_preferences(monkeypatch):
    from app.commerce import turn_flow
    from app.models import AgentResult
    state = CommerceConversationState(last_catalog_query="cabo lightning", active_preferences={"budget_max": 80})
    run = AsyncMock(return_value=object())
    monkeypatch.setattr(turn_flow, "run_commerce_turn", run)
    response = await recover_conversation("não foi isso que perguntei", state=state,
        execute=AsyncMock(), render=lambda outcome, state: AgentResult(reply_text="Cabo consultado"))
    assert run.await_args.args[0] == "cabo lightning"
    assert run.await_args.kwargs["state"].active_preferences == {"budget_max": 80}
    assert response.response_metadata["conversation_repair"]["attempt"] == 1


@pytest.mark.asyncio
async def test_explicit_replacement_wins_over_previous_query(monkeypatch):
    from app.commerce import turn_flow
    from app.models import AgentResult
    run = AsyncMock(return_value=object())
    monkeypatch.setattr(turn_flow, "run_commerce_turn", run)
    state = CommerceConversationState(last_catalog_query="fone bluetooth")
    await recover_conversation("não foi isso que pedi, quero cabo usb c", state=state,
        execute=AsyncMock(), render=lambda outcome, state: AgentResult(reply_text="Cabo"))
    assert run.await_args.args[0] == "quero cabo usb c"
