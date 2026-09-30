from unittest.mock import AsyncMock

import pytest

from app.commerce.mercos.order_status import normalize_status, status_result
from app.commerce_context import CommerceConversationState
from app.order_queries import handle_order_query


def order(status=2):
    return status_result([normalize_status({"id": 166999001, "numero": 95805,
        "status": status, "status_faturamento": 0})], "95805")


@pytest.mark.asyncio
async def test_customer_next_step_after_order_lookup_never_searches_catalog(monkeypatch):
    from app import openai_agent, sales_agent
    from tests.test_chatbo_pipeline_replay import Replay
    replay = Replay(monkeypatch)
    async def execute(tool, arguments):
        replay.calls.append((tool, arguments))
        assert tool == "get_order_complete", "An order follow-up must not search products"
        return order()
    monkeypatch.setattr(openai_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    await replay.say("finalizei o pedido")
    await replay.say("#95805")
    result, row = await replay.say("Okay e o que faço agora?")
    assert row["active_topic"] == "order_next_steps"
    assert "95805" in result.reply_text and "pagamento" in result.reply_text and "entrega" in result.reply_text
    assert "não encontrei" not in result.reply_text.casefold()
    assert not result.handoff_required and not result.safety_reason
    assert result.response_metadata["factual_validation"]["valid"]
    assert row["tools"] == ["get_order_complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["E agora?", "Certo, qual o próximo passo?", "Entendi, como devo prosseguir?", "O que eu preciso fazer agora?"])
async def test_next_step_variations_stay_in_existing_order_context(text):
    from app.commerce.turn_resolver import product_query_for
    assert product_query_for(text) is None
    result = await handle_order_query(text, state=CommerceConversationState(order_id="166999001", active_topic="order_status"),
                                     execute=AsyncMock(return_value=order()))
    assert result is not None and "95805" in result.reply_text
    assert "pagamento" in result.reply_text and "entrega" in result.reply_text


@pytest.mark.asyncio
async def test_cancelled_order_has_no_payment_instruction_and_failure_has_no_success_guidance():
    state = CommerceConversationState(order_id="166999001", active_topic="order_status")
    cancelled = await handle_order_query("e agora?", state=state, execute=AsyncMock(return_value=order(0)))
    assert "cancelamento" in cancelled.reply_text and "condições de pagamento" not in cancelled.reply_text
    failed = await handle_order_query("e agora?", state=state, execute=AsyncMock(return_value={"ok": False,"error":"order_index_unavailable"}))
    assert not failed.commercial_data.get("success")
    assert "condições de pagamento" not in failed.reply_text


@pytest.mark.asyncio
async def test_next_steps_do_not_reuse_an_order_after_switching_to_products():
    result = await handle_order_query("E agora?", state=CommerceConversationState(order_id="166999001", active_topic="product_catalog"),
                                     execute=AsyncMock(side_effect=AssertionError("Unrelated order consulted")))
    assert result is None
