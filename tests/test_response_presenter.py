from app.config import Settings, get_settings
from app.models import AgentResult, IncomingMessage
from app.prompt_layers import PROMPT_LAYER_ORDER, STYLE_VOICE_RULES
from app.response_composer import compose_outbound_reply
from app.response_presenter import (
    present_agent_result,
    present_reply_text,
    present_reply_text_full,
    present_reply_text_thin,
)
from app.channel_profiles import channel_system_hint


def test_full_strips_generic_opener_and_limits_questions():
    text = present_reply_text(
        "Claro! O Seastar custa R$ 10,00. Quer que eu reserve? Posso preparar o frete também?",
        channel="whatsapp",
        intent="commerce",
        mode="full",
    )
    assert not text.lower().startswith("claro")
    assert text.count("?") <= 1


def test_thin_preserves_second_commerce_question():
    raw = (
        "O Seastar custa R$ 10,00. Quer que eu reserve? "
        "Posso preparar o frete também?"
    )
    thin = present_reply_text(
        raw,
        channel="whatsapp",
        intent="commerce",
        mode="thin",
    )
    full = present_reply_text(
        raw,
        channel="whatsapp",
        intent="commerce",
        mode="full",
    )
    assert thin.count("?") == 2
    assert full.count("?") <= 1
    assert "Claro" not in thin  # no opener in source


def test_thin_still_zero_questions_on_handoff():
    text = present_reply_text(
        "Vou te transferir. Qual horário prefere? Quer pix também?",
        channel="whatsapp",
        intent="handoff",
        mode="thin",
    )
    assert text.count("?") == 0


def test_marks_similar_product_and_preserves_url():
    text = present_reply_text(
        "Achei este modelo. https://www.sorteionewstore.com.br/produto/1",
        channel="whatsapp",
        intent="commerce",
        metadata={"match_kind": "similar"},
        mode="thin",
    )
    assert "semelhante" in text.casefold()
    assert "https://www.sorteionewstore.com.br/produto/1" in text


def test_compose_greeting_handoff_and_long_message(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_PRESENTER_MODE", "full")
    get_settings.cache_clear()
    try:
        greeting = compose_outbound_reply(
            IncomingMessage(channel="whatsapp", text="oi"),
            AgentResult(reply_text="Com certeza! Olá!", intent="greeting"),
        )
        assert "Com certeza" not in greeting.reply_text

        handoff = compose_outbound_reply(
            IncomingMessage(channel="whatsapp", text="atendente"),
            AgentResult(
                reply_text="Vou te transferir. Qual horário prefere? Quer pix também?",
                intent="handoff",
                handoff_required=True,
            ),
        )
        assert handoff.reply_text.count("?") == 0

        long = compose_outbound_reply(
            IncomingMessage(channel="instagram", text="busca"),
            AgentResult(
                reply_text="\n\n".join([f"Bloco {i}" for i in range(6)]),
                intent="commerce",
            ),
            max_reply_chars=700,
        )
        assert long.reply_text.count("\n\n") <= 1
        assert len(long.reply_text) <= 700
    finally:
        get_settings.cache_clear()


def test_shadow_outbound_is_full_with_thin_preview(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_PRESENTER_MODE", "shadow")
    get_settings.cache_clear()
    try:
        raw = (
            "Claro! O Seastar custa R$ 10,00. Quer que eu reserve? "
            "Posso preparar o frete também?"
        )
        result = present_agent_result(
            IncomingMessage(channel="whatsapp", text="preço"),
            AgentResult(reply_text=raw, intent="commerce"),
        )
        meta = result.response_metadata["presentation"]
        assert meta["mode"] == "shadow"
        assert meta["applied"] == "full"
        assert not result.reply_text.lower().startswith("claro")
        assert result.reply_text.count("?") <= 1
        assert meta["thin_preview"].count("?") == 2
        assert meta["diff"]["texts_differ"] is True
        assert meta["diff"]["questions_dropped_by_full"] >= 1
    finally:
        get_settings.cache_clear()


def test_thin_and_full_helpers_align_with_mode_dispatch():
    raw = "Bloco A\n\nBloco B\n\nBloco C\n\nBloco D"
    assert present_reply_text_thin(raw, channel="whatsapp") == present_reply_text(
        raw, channel="whatsapp", mode="thin"
    )
    assert present_reply_text_full(raw, channel="instagram") == present_reply_text(
        raw, channel="instagram", mode="full"
    )


def test_audio_disabled_on_instagram_profile():
    result = compose_outbound_reply(
        IncomingMessage(channel="instagram", text="oi"),
        AgentResult(
            reply_text="Olá",
            intent="greeting",
            reply_modality="audio",
            reply_audio_url="https://example.com/a.ogg",
        ),
    )
    assert result.reply_modality == "text"
    assert result.reply_audio_url is None


def test_style_voice_single_source_in_channel_hint():
    hint = channel_system_hint("whatsapp")
    assert STYLE_VOICE_RULES in hint
    assert "Claro" in hint


def test_prompt_layer_order_documents_compiler_stack():
    assert PROMPT_LAYER_ORDER[0] == "fixed_safety_policy"
    assert "channel_overlay" in PROMPT_LAYER_ORDER
    assert "conversation_summary" in PROMPT_LAYER_ORDER
    assert PROMPT_LAYER_ORDER[-1] == "operational_contract"


def test_presenter_mode_default_is_thin():
    assert Settings.model_fields["agent_presenter_mode"].default == "thin"
