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


def test_full_does_not_rewrite_style():
    """Abertura, perguntas e CTAs sao decisao da persona publicada, nao de regex.

    O rollback de emergencia forca ``full``: se ele editasse estilo, viraria
    uma segunda persona justamente no momento de crise.
    """
    raw = "Claro! O ChargeMax custa R$ 10,00. Quer que eu reserve? Posso preparar o frete também?"
    text = present_reply_text(raw, channel="whatsapp", intent="commerce", mode="full")
    assert text == raw
    assert text == present_reply_text(raw, channel="whatsapp", intent="commerce", mode="thin")


def test_style_editing_helpers_are_gone():
    import app.response_presenter as presenter

    for helper in ("strip_generic_opener", "strip_robotic_closing", "soften_emoji_excess", "_dedupe_ctas"):
        assert not hasattr(presenter, helper), helper


def test_thin_preserves_second_commerce_question():
    raw = (
        "O ChargeMax custa R$ 10,00. Quer que eu reserve? "
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
    assert full == thin
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
        "Achei este modelo. https://xnamai.meuspedidos.com.br/produto/1",
        channel="whatsapp",
        intent="commerce",
        metadata={"match_kind": "similar"},
        mode="thin",
    )
    assert "semelhante" in text.casefold()
    assert "https://xnamai.meuspedidos.com.br/produto/1" in text


def test_compose_greeting_handoff_and_long_message(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_PRESENTER_MODE", "full")
    get_settings.cache_clear()
    try:
        greeting = compose_outbound_reply(
            IncomingMessage(channel="whatsapp", text="oi"),
            AgentResult(reply_text="Com certeza! Olá!", intent="greeting"),
        )
        # Estilo pertence a persona: o presenter nao corta a abertura.
        assert greeting.reply_text.startswith("Com certeza")

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
            "Claro! O ChargeMax custa R$ 10,00. Quer que eu reserve? "
            "Posso preparar o frete também?"
        )
        result = present_agent_result(
            IncomingMessage(channel="whatsapp", text="preço"),
            AgentResult(reply_text=raw, intent="commerce"),
        )
        meta = result.response_metadata["presentation"]
        assert meta["mode"] == "shadow"
        assert meta["applied"] == "full"
        # full e thin agora sao o mesmo pipeline tecnico: nada de estilo cortado.
        assert result.reply_text == raw
        assert meta["thin_preview"].count("?") == 2
        assert meta["diff"]["texts_differ"] is False
        assert meta["diff"]["questions_dropped_by_full"] == 0
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
    # Voz e estilo comercial pertencem a persona publicada, nao ao codigo.
    for voice_rule in ("Claro", "Com certeza", "CTA", "forçar venda"):
        assert voice_rule not in hint


def test_prompt_layer_order_documents_compiler_stack():
    assert PROMPT_LAYER_ORDER[0] == "fixed_safety_policy"
    assert "channel_overlay" in PROMPT_LAYER_ORDER
    assert "conversation_summary" in PROMPT_LAYER_ORDER
    assert PROMPT_LAYER_ORDER[-1] == "operational_contract"


def test_presenter_mode_default_is_thin():
    assert Settings.model_fields["agent_presenter_mode"].default == "thin"
