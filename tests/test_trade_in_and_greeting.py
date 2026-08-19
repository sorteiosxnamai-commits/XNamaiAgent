"""Trade-in / appraisal handoff and greeting dedupe."""

from app.greeting_policy import (
    already_said,
    choose_greeting_reply,
    is_generic_greeting_reply,
)
from app.guardrails import detect_trade_in_or_appraisal_request
from app.handoff_service import build_human_handoff_result, should_request_human_handoff
from app.models import AgentResult, IncomingMessage
from app.response_critique import apply_fast_deterministic_critique
from app.site_knowledge import TRADE_IN_HANDOFF_MESSAGE


def test_detect_trade_in_seminovo_certina():
    text = "Bom dia vcs estão comprando Certina ds action seminovo comprado com vcs?"
    assert detect_trade_in_or_appraisal_request(text) is True


def test_trade_in_triggers_handoff_with_policy_message():
    incoming = IncomingMessage(
        channel="instagram",
        text="vocês estão comprando relógio seminovo?",
    )
    assert should_request_human_handoff(incoming) == "trade_in_or_appraisal"
    result = build_human_handoff_result(reason="trade_in_or_appraisal")
    assert result.handoff_required is True
    assert "avalia" in result.reply_text.lower()
    assert "troca" in result.reply_text.lower()
    assert TRADE_IN_HANDOFF_MESSAGE.split(".")[0] in result.reply_text


def test_fast_critique_rewrites_trade_in_denial():
    incoming = IncomingMessage(
        channel="instagram",
        text="estão comprando Certina seminovo?",
    )
    bad = AgentResult(
        reply_text=(
            "Bom dia! Não compramos relógios seminovos, apenas vendemos produtos novos."
        ),
        intent="commerce",
        handoff_required=False,
    )
    fixed, verdict, skip = apply_fast_deterministic_critique(
        incoming=incoming,
        result=bad,
        recent_turns=[],
    )
    assert skip == "fast_trade_in_handoff"
    assert fixed.handoff_required is True
    assert "avalia" in fixed.reply_text.lower()
    assert verdict is not None
    assert "trade_in_policy_violation" in verdict.issues


def test_greeting_avoids_repeating_same_phrase_to_same_person():
    assert is_generic_greeting_reply("Olá! Como posso ajudar?")
    history = [{"role": "assistant", "content": "Olá! Como posso ajudar?"}]
    reply = choose_greeting_reply(history)
    assert reply != "Olá! Como posso ajudar?"
    assert not already_said(reply, history)
    history2 = history + [{"role": "assistant", "content": reply}]
    reply2 = choose_greeting_reply(history2)
    assert reply2 not in {"Olá! Como posso ajudar?", reply}


def test_fast_critique_dedupes_identical_greeting():
    incoming = IncomingMessage(channel="whatsapp", text="Boa tarde")
    result = AgentResult(
        reply_text="Olá! Como posso ajudar?",
        intent="general",
        handoff_required=False,
    )
    fixed, verdict, skip = apply_fast_deterministic_critique(
        incoming=incoming,
        result=result,
        recent_turns=[
            {"role": "assistant", "content": "Olá! Como posso ajudar?"},
        ],
    )
    assert skip == "fast_greeting_dedupe"
    assert fixed.reply_text != "Olá! Como posso ajudar?"
    assert verdict is not None
