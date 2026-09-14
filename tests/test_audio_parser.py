from app.audio_service import extract_audio_attachment, is_audio_attachment
from app.webhook_parser import parse_brevo_whatsapp_payload


def test_parse_audio_message_from_brevo_webhook():
    payload = {
        "eventName": "conversationFragment",
        "visitor": {"id": "visitor-1", "attributes": {"SMS": "5585999999999"}},
        "messages": [
            {
                "id": "msg-1",
                "type": "visitor",
                "file": {
                    "name": "audio.ogg",
                    "link": "https://cdn.example.com/audio.ogg",
                    "mimeType": "audio/ogg",
                },
            }
        ],
    }
    incoming = parse_brevo_whatsapp_payload(payload)
    assert incoming.input_modality == "audio"
    assert incoming.audio_url == "https://cdn.example.com/audio.ogg"
    assert incoming.audio_filename == "audio.ogg"
    assert is_audio_attachment(payload["messages"][0]["file"]) is True


def test_extract_audio_attachment():
    payload = {
        "messages": [
            {
                "type": "visitor",
                "file": {"name": "voice.opus", "link": "https://x/voice.opus", "mimeType": "audio/ogg"},
            }
        ]
    }
    audio = extract_audio_attachment(payload)
    assert audio is not None
    assert audio["link"] == "https://x/voice.opus"


# Migrados de tests/test_available_numbers.py, que testava a grade de
# numeros do sorteio: estes dois sao de audio e nao pertencem aquela feature.
from app.audio_service import is_placeholder_audio_text, should_transcribe_incoming


def test_should_transcribe_when_text_is_filename():
    assert should_transcribe_incoming("audio.ogg", "https://x/audio.ogg", "audio.ogg") is True


def test_is_placeholder_audio_text():
    assert is_placeholder_audio_text("audio.ogg", "audio.ogg") is True
    assert is_placeholder_audio_text("qual meu saldo", None) is False
