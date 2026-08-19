from app.brevo_client import _build_brevo_audio_file
from app.models import AgentResult, IncomingMessage


def test_build_brevo_audio_file_uses_whatsapp_voice_mime():
    payload = _build_brevo_audio_file(
        url="https://example.supabase.co/storage/v1/object/public/agent-audio/agent-replies/abc.ogg",
        size=1234,
    )

    assert payload["name"] == "resposta.ogg"
    assert payload["mimeType"] == "audio/ogg; codecs=opus"
    assert payload["size"] == 1234
    assert payload["link"].endswith(".ogg")


def test_agent_result_keeps_audio_bytes_for_attachment_size():
    result = AgentResult(
        reply_text="Olá!",
        reply_modality="audio",
        reply_audio_bytes=b"x" * 512,
        reply_audio_mime_type="audio/ogg; codecs=opus",
        reply_audio_url="https://example.supabase.co/agent-replies/test.ogg",
    )

    assert result.reply_modality == "audio"
    assert len(result.reply_audio_bytes) == 512
