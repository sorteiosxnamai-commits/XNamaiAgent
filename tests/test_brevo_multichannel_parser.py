import pytest

from app.webhook_parser import (
    build_sender_key,
    inbound_skip_reason,
    normalize_channel,
    parse_brevo_conversations_payload,
)


def _instagram_started() -> dict:
    return {
        "eventName": "conversationStarted",
        "conversationId": "conv-instagram-001",
        "message": {
            "id": "msg-instagram-001",
            "type": "visitor",
            "text": "Vocês têm relógio Tissot?",
            "createdAt": 1785700000000,
        },
        "visitor": {
            "id": "brevo-visitor-instagram-001",
            "source": "instagram",
            "sourceChannelRef": "instagram-account-001",
            "sourceChannelLink": "https://instagram.com/newstore",
            "sourceConversationRef": "instagram-user-999",
            "displayedName": "Cliente Instagram",
        },
    }


def test_instagram_conversation_started_is_normalized_without_phone():
    incoming = parse_brevo_conversations_payload(_instagram_started())

    assert incoming.channel == "instagram"
    assert incoming.sender_phone is None
    assert incoming.visitor_id == "brevo-visitor-instagram-001"
    assert incoming.conversation_id == "conv-instagram-001"
    assert incoming.sender_external_id == "instagram-user-999"
    assert incoming.sender_key == "instagram:instagram-user-999"
    assert incoming.text == "Vocês têm relógio Tissot?"
    assert incoming.message_id == "msg-instagram-001"


def test_fragment_selects_newest_visitor_from_out_of_order_history():
    payload = {
        "eventName": "conversationFragment",
        "messages": [
            {"id": "visitor-new", "type": "visitor", "text": "Me manda o link", "createdAt": 3000},
            {"id": "agent-old", "type": "agent", "text": "Resposta", "createdAt": 1000},
            {"id": "trigger-old", "type": "visitor", "isTrigger": True, "createdAt": 2000},
        ],
        "visitor": {
            "id": "visitor-ig",
            "source": "instagram_dm",
            "sourceConversationRef": "user-ig",
        },
    }

    incoming = parse_brevo_conversations_payload(payload)

    assert incoming.message_id == "visitor-new"
    assert incoming.text == "Me manda o link"
    assert inbound_skip_reason(payload) is None


@pytest.mark.parametrize("flag", ["isPushed", "isTrigger"])
def test_automatic_messages_are_ignored(flag):
    payload = {
        "eventName": "conversationFragment",
        "messages": [{"id": "auto", "type": "visitor", flag: True, "createdAt": 1}],
    }
    assert inbound_skip_reason(payload) == "agent_message"


def test_facebook_file_without_text_gets_safe_placeholder():
    incoming = parse_brevo_conversations_payload({
        "eventName": "conversationFragment",
        "conversationId": "conv-fb",
        "messages": [{
            "id": "file-fb",
            "type": "visitor",
            "file": {"name": "manual.pdf", "link": "https://private.example/file", "mimeType": "application/pdf"},
        }],
        "visitor": {
            "id": "visitor-fb",
            "source": "facebook_messenger",
            "sourceConversationRef": "123",
        },
    })

    assert incoming.channel == "facebook"
    assert incoming.sender_key == "facebook:123"
    assert incoming.text == "[Arquivo recebido via Facebook: manual.pdf]"
    assert incoming.attachment_type == "file"
    assert "private.example" not in incoming.text


def test_channel_aliases_and_sender_keys_are_collision_safe():
    assert normalize_channel("ig") == "instagram"
    assert normalize_channel("messenger") == "facebook"
    assert build_sender_key("instagram", None, "123", "visitor", None) == "instagram:123"
    assert build_sender_key("facebook", None, "123", "visitor", None) == "facebook:123"
    assert build_sender_key("wa", "+55 (43) 99999-9999", None, None, None) == "whatsapp:5543999999999"
