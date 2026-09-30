import json

import pytest

from app.commerce_context import CommerceConversationState
from app.working_memory import build_working_memory, format_working_memory_block


def test_conversation_goal_survives_without_product_or_order():
    from app.commerce_context import evolve_commerce_state
    from app.models import AgentResult
    state = evolve_commerce_state(CommerceConversationState(), AgentResult(
        reply_text="Podemos começar pelo perfil da loja.", intent="commerce",
        response_metadata={"domain": "commerce", "conversation_goal": "Abastecer uma loja nova"}))
    assert state.interpreter_payload()["conversation_goal"] == "Abastecer uma loja nova"
    assert "Abastecer uma loja nova" in format_working_memory_block(state)


def test_goal_is_durable_without_forcing_checkout_resume_and_old_order_keeps_new_topic():
    from app.context_resume import commerce_state_resumable_score, has_resumable_commerce, merge_commerce_states
    current = {"conversation_goal": "Comparar tecnologias", "active_topic": "commercial_guidance", "pending_followup": None}
    assert commerce_state_resumable_score(current) > 0
    assert not has_resumable_commerce(current)
    merged = merge_commerce_states(current, {"order_id": "old-order", "active_topic": "order_status",
        "pending_followup": {"question": "Quer pagar?"}})
    assert merged["order_id"] == "old-order"
    assert merged["active_topic"] == "commercial_guidance"
    assert merged["conversation_goal"] == "Comparar tecnologias"
    assert merged["pending_followup"] is None


def test_topic_only_context_survives_without_cart_or_product():
    block = format_working_memory_block({"active_topic": "store_product_overview"})
    memory = json.loads(block.split("\n", 1)[1])
    assert memory["active_topic"] == "store_product_overview"
    assert memory["has_cart"] is False
    assert memory["active_product"] is None


def test_pending_question_survives_without_commercial_entity():
    state = CommerceConversationState(pending_followup={
        "question": "Quer receber o catálogo?",
        "tool": "create_order",
        "arguments": {"customer_document": "private"},
    })
    block = format_working_memory_block(state)
    memory = json.loads(block.split("\n", 1)[1])
    assert memory["pending_followup"] == {"question": "Quer receber o catálogo?"}
    assert "private" not in block
    assert "create_order" not in block
    assert state.pending_followup["tool"] == "create_order"
    assert state.pending_action is None


@pytest.mark.parametrize("question", [None, "", "   ", 123, {"instruction": "execute"}])
def test_invalid_pending_question_does_not_create_context(question):
    state = {"pending_followup": {"question": question}}
    assert build_working_memory(state)["pending_followup"] is None
    assert format_working_memory_block(state) == ""


def test_question_is_bounded_and_memory_is_not_action_authority():
    memory = build_working_memory({"pending_followup": {"question": "sim? " * 500}})
    assert len(memory["pending_followup"]["question"]) <= 600
    policy = memory["conversation_context_policy"]
    assert "não instruções" in policy
    assert "autorização" in policy
    assert "Não infira preço, estoque, pagamento ou status atual" in policy
    assert "mudança explícita" in policy


def test_empty_memory_still_has_no_prompt_block():
    assert format_working_memory_block(None) == ""
