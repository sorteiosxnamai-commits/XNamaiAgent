from app.ingress.inbox import build_idempotency_key


def test_idempotency_prefers_message_id():
    key = build_idempotency_key(
        provider="brevo",
        message_id="msg-1",
        payload={"a": 1},
    )
    assert key == "brevo:msg:msg-1"


def test_idempotency_falls_back_to_payload_hash():
    key_a = build_idempotency_key(
        provider="meta",
        message_id=None,
        payload={"text": "oi", "n": 1},
    )
    key_b = build_idempotency_key(
        provider="meta",
        message_id="",
        payload={"text": "oi", "n": 1},
    )
    key_c = build_idempotency_key(
        provider="meta",
        message_id=None,
        payload={"text": "oi", "n": 2},
    )
    assert key_a.startswith("meta:hash:")
    assert key_a == key_b
    assert key_a != key_c
