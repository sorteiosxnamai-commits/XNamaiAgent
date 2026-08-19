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
