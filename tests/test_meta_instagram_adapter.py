from app.channels.meta_instagram import (
    handle_meta_verify_challenge,
    instagram_event_skip_reason,
    messaging_event_shapes,
    parse_meta_instagram_messaging,
    payload_skeleton,
    verify_meta_signature,
)


def test_meta_signature_roundtrip():
    body = b'{"object":"instagram"}'
    secret = "test-app-secret"
    import hashlib
    import hmac

    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(
        app_secret=secret, body=body, signature_header=sig
    )
    assert not verify_meta_signature(
        app_secret=secret, body=body, signature_header="sha256=deadbeef"
    )
    upper = "SHA256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest().upper()
    assert verify_meta_signature(
        app_secret=secret, body=body, signature_header=upper
    )


def test_meta_signature_accepts_instagram_secret():
    from app.channels.meta_instagram import verify_meta_signatures
    import hashlib
    import hmac

    body = b'{"object":"instagram"}'
    ig_secret = "instagram-app-secret"
    sig = "sha256=" + hmac.new(ig_secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_meta_signatures(
        app_secrets=["facebook-app-secret", ig_secret],
        body=body,
        signature_header_sha256=sig,
    )
    assert not verify_meta_signatures(
        app_secrets=["facebook-app-secret"],
        body=body,
        signature_header_sha256=sig,
    )


def test_meta_verify_challenge():
    assert (
        handle_meta_verify_challenge(
            mode="subscribe",
            verify_token="tok",
            challenge="12345",
            expected_token="tok",
        )
        == "12345"
    )
    assert (
        handle_meta_verify_challenge(
            mode="subscribe",
            verify_token="wrong",
            challenge="12345",
            expected_token="tok",
        )
        is None
    )


def test_parse_meta_changes_field_messages():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "sender": {"id": "user-2"},
                            "recipient": {"id": "ig-biz"},
                            "message": {"mid": "m2", "text": "teste"},
                        },
                    }
                ],
            }
        ],
    }
    messages = parse_meta_instagram_messaging(payload)
    assert len(messages) == 1
    assert messages[0].text == "teste"
    assert messages[0].sender_external_id == "user-2"
    assert messages[0].provider == "meta"


def test_parse_instagram_login_from_and_string_message():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "from": {"id": "user-9", "username": "cliente"},
                            "id": "mid-9",
                            "message": "oi, quanto fica?",
                            "timestamp": 1710000000,
                        },
                    }
                ],
            }
        ],
    }
    messages = parse_meta_instagram_messaging(payload)
    assert len(messages) == 1
    assert messages[0].text == "oi, quanto fica?"
    assert messages[0].sender_external_id == "user-9"
    assert messages[0].message_id == "mid-9"
    assert messages[0].provider == "meta"


def test_parse_message_edit_with_nested_sender_and_text():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "messaging": [
                    {
                        "timestamp": 1,
                        "message_edit": {
                            "mid": "m-edit",
                            "text": "oi editado",
                            "from": {"id": "user-edit"},
                        },
                    }
                ],
            }
        ],
    }
    shapes = messaging_event_shapes(payload)
    assert shapes[0]["edit_text_len"] == 10
    assert shapes[0]["edit_has_sender"] is True
    assert parse_meta_instagram_messaging(payload) == []
    assert (
        instagram_event_skip_reason(payload["entry"][0]["messaging"][0])
        == "message_edit_unparsed"
    )


def test_messaging_event_shapes_marks_read_receipts():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "messaging": [
                    {
                        "sender": {"id": "user-1"},
                        "recipient": {"id": "ig-biz"},
                        "timestamp": 1,
                        "read": {"watermark": 1},
                    }
                ],
            }
        ],
    }
    shapes = messaging_event_shapes(payload)
    assert shapes[0]["has_read"] is True
    assert shapes[0]["text_len"] == 0
    assert parse_meta_instagram_messaging(payload) == []
    assert instagram_event_skip_reason(payload["entry"][0]["messaging"][0]) == "read_receipt"
    skeleton = payload_skeleton(payload)
    assert "messaging" in skeleton["entry"][0]


def test_parse_meta_story_reply_attachment():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "messaging": [
                    {
                        "sender": {"id": "user-1"},
                        "recipient": {"id": "ig-biz"},
                        "message": {
                            "mid": "m1",
                            "text": "valor",
                            "reply_to": {
                                "story": {
                                    "id": "story-99",
                                    "url": "https://cdn.example/story.jpg",
                                }
                            },
                        },
                    }
                ],
            }
        ],
    }
    messages = parse_meta_instagram_messaging(payload)
    assert len(messages) == 1
    msg = messages[0]
    assert msg.channel == "instagram"
    assert msg.provider == "meta"
    assert msg.text == "valor"
    assert msg.image_url == "https://cdn.example/story.jpg"
    assert msg.instagram_story is not None
    assert msg.instagram_story.replied_to_story is True
    assert msg.instagram_story.story_media_id == "story-99"


def test_story_rollout_allows_meta_live_media(monkeypatch):
    from app.config import get_settings
    from app.instagram_story_models import InstagramStoryContext
    from app.instagram_story_service import story_rollout_allows
    from app.models import IncomingMessage
    from pydantic import SecretStr

    get_settings.cache_clear()
    monkeypatch.setenv("META_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "false")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "off")
    get_settings.cache_clear()

    story = InstagramStoryContext(
        provider="meta",
        instagram_account_id="17841404241547355",
        story_media_id="s1",
        replied_to_story=True,
        story_media_url_private=SecretStr("https://cdn.example/s.jpg"),
    )
    incoming = IncomingMessage(
        provider="meta",
        channel="instagram",
        image_url="https://cdn.example/s.jpg",
        instagram_story=story,
    )
    allowed, reason = story_rollout_allows(
        tenant_id="newstore",
        story=story,
        incoming=incoming,
    )
    assert allowed is True
    assert reason == "meta_live_media"
    get_settings.cache_clear()
