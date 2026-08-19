from app.channel_profiles import get_channel_profile
from app.models import AgentResult, IncomingMessage
from app.response_composer import compose_outbound_reply


def test_instagram_disables_audio_and_shortens_replies():
    profile = get_channel_profile("instagram")
    assert profile.allow_audio_reply is False
    assert profile.max_reply_chars == 700
    assert profile.assisted_chat is True

    result = compose_outbound_reply(
        IncomingMessage(channel="instagram", text="oi"),
        AgentResult(
            reply_text="a" * 800,
            reply_modality="audio",
            reply_audio_url="https://example.com/a.ogg",
        ),
    )
    assert result.reply_modality == "text"
    assert result.reply_audio_url is None
    assert len(result.reply_text) <= 700


def test_whatsapp_keeps_assisted_chat_profile():
    profile = get_channel_profile("whatsapp")
    assert profile.allow_audio_reply is True
    assert profile.assisted_chat is True
