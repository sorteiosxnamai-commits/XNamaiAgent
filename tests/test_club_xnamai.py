import pytest

from app.club_xnamai import handle_club_turn, maybe_append_club_offer
from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.models import AgentResult
from app.site_knowledge import CLUB_URL, STORE_URL


def test_non_member_receives_official_club_offer():
    result = handle_club_turn(
        "ainda não sou membro do Club Xnamai",
        state=CommerceConversationState(),
    )
    assert result is not None
    assert CLUB_URL in result.reply_text
    assert "preços exclusivos" in result.reply_text
    assert result.response_metadata["club_membership_status"] == "non_member"
    assert result.response_metadata["used_openai_interpreter"] is False


def test_member_is_sent_to_account_and_catalog_without_another_pitch():
    result = handle_club_turn(
        "já sou membro do clube Xnamai",
        state=CommerceConversationState(),
    )
    assert result is not None
    assert CLUB_URL in result.reply_text
    assert STORE_URL in result.reply_text
    state = evolve_commerce_state(CommerceConversationState(), result)
    reply, offered = maybe_append_club_offer("Produto encontrado.", state=state)
    assert reply == "Produto encontrado."
    assert offered is False


def test_club_registration_is_not_confused_with_commercial_registration():
    result = handle_club_turn(
        "quero me cadastrar no Club",
        state=CommerceConversationState(),
    )
    assert result is not None
    assert CLUB_URL in result.reply_text
    assert "CPF/CNPJ" not in result.reply_text


def test_proactive_offer_is_appended_only_once():
    state = CommerceConversationState()
    first, offered = maybe_append_club_offer("Achei estes produtos.", state=state)
    assert offered is True
    assert CLUB_URL in first
    state = evolve_commerce_state(
        state,
        AgentResult(
            reply_text="",
            response_metadata={"domain": "commerce", "club_offer_shown": True},
        ),
    )
    second, offered_again = maybe_append_club_offer("Mais produtos.", state=state)
    assert second == "Mais produtos."
    assert offered_again is False


@pytest.mark.asyncio
@pytest.mark.offline_eval
async def test_club_route_skips_openai_interpretation(monkeypatch):
    import app.openai_agent as agent

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("OpenAI interpretation must not run for Club")

    monkeypatch.setattr(agent, "interpret_message", forbidden)
    monkeypatch.setattr(agent, "load_recent_conversation_turns", lambda *_a, **_k: [])
    result = await agent.generate_agent_reply_async(
        agent.IncomingMessage(text="quero conhecer o Club Xnamai"),
        {"_commerce_state": CommerceConversationState()},
    )
    assert CLUB_URL in result.reply_text
    assert result.response_metadata["used_openai_interpreter"] is False
    assert result.response_metadata["used_openai_responder"] is False


@pytest.mark.asyncio
async def test_unavailable_product_search_offers_official_catalog_and_club():
    from app.commerce.turn_flow import run_commerce_turn
    from app.sales_agent import _render_commerce_turn

    async def unavailable(_tool, _args):
        return {"ok": False, "error": "commerce_provider_unavailable"}

    state = CommerceConversationState()
    outcome = await run_commerce_turn(
        "vocês têm cabo USB-C?",
        state=state,
        execute=unavailable,
    )
    result = _render_commerce_turn(outcome, state)
    assert result is not None
    assert result.safety_reason == "commerce_provider_unavailable"
    assert STORE_URL in result.reply_text
    assert CLUB_URL in result.reply_text
    assert "não encontrei" not in result.reply_text.casefold()
    assert result.response_metadata["used_commerce_provider"] is False
