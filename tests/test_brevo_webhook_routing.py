import pytest
from httpx import ASGITransport, AsyncClient

from app.models import AgentResult, BrevoSendResult
from app.webhook_parser import webhook_event_skip_reason


def _fragment_payload() -> dict:
    return {
        "eventName": "conversationFragment",
        "conversationId": "conversation-1",
        "messages": [
            {
                "type": "visitor",
                "id": "message-1",
                "text": "mensagem inbound",
            }
        ],
        "visitor": {
            "id": "visitor-1",
            "attributes": {"SMS": "5511999999999"},
        },
    }


async def _post_webhook(index, payload):
    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=index.app),
            base_url="http://test",
        ) as client:
            return await client.post(
                "/api/webhooks/brevo/whatsapp",
                json=payload,
            )
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)


@pytest.mark.asyncio
async def test_conversation_fragment_enters_agent_pipeline(monkeypatch, capsys):
    import api.index as index

    processed = []

    async def process(incoming, customer_context):
        processed.append((incoming, customer_context))
        return AgentResult(reply_text="ok", intent="commerce")

    async def send(*_args):
        return BrevoSendResult(
            ok=True,
            dry_run=True,
            provider_response={"accepted": True},
        )

    monkeypatch.setattr(index, "inbound_message_exists", lambda *_args: False)
    monkeypatch.setattr(index, "claim_inbound_message", lambda _message: (True, 101))
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *_args: True)
    monkeypatch.setattr(index, "find_customer_profile_by_phone", lambda _phone: {})
    monkeypatch.setattr(index, "process_incoming_message", process)
    monkeypatch.setattr(index, "send_brevo_reply", send)
    monkeypatch.setattr(index, "insert_agent_response", lambda _data: None)

    response = await _post_webhook(index, _fragment_payload())

    assert response.status_code == 200
    assert len(processed) == 1
    assert processed[0][0].event_type == "conversationFragment"
    output = capsys.readouterr().out
    assert "[agent.obs]" in output
    assert '"event": "brevo.webhook.routing"' in output
    assert '"event_name": "conversationFragment"' in output
    assert '"should_process": true' in output
    assert '"event": "brevo.webhook.processing"' in output


@pytest.mark.asyncio
async def test_conversation_transcript_is_explicitly_ignored(monkeypatch, capsys):
    import api.index as index

    payload = {
        **_fragment_payload(),
        "eventName": "conversationTranscript",
    }
    monkeypatch.setattr(
        index,
        "claim_inbound_message",
        lambda _message: (_ for _ in ()).throw(
            AssertionError("transcript must not be claimed")
        ),
    )
    monkeypatch.setattr(
        index,
        "process_incoming_message",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("transcript must not be processed")
        ),
    )

    response = await _post_webhook(index, payload)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "skipped": True,
        "reason": "non_inbound_event",
    }
    output = capsys.readouterr().out
    assert '"event_name": "conversationTranscript"' in output
    assert '"should_process": false' in output
    assert '"reason": "non_inbound_event"' in output
    assert '"event": "brevo.webhook.processing"' not in output


@pytest.mark.asyncio
async def test_conversation_started_without_message_preserves_no_text_skip(
    monkeypatch,
    capsys,
):
    import api.index as index

    monkeypatch.setattr(
        index,
        "process_incoming_message",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("empty conversationStarted must not be processed")
        ),
    )
    payload = {
        "eventName": "conversationStarted",
        "conversationId": "conversation-1",
        "visitor": {
            "id": "visitor-1",
            "attributes": {"SMS": "5511999999999"},
        },
    }

    response = await _post_webhook(index, payload)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "skipped": True,
        "reason": "no_text",
    }
    output = capsys.readouterr().out
    assert '"event_name": "conversationStarted"' in output
    assert '"should_process": false' in output
    assert '"reason": "no_text"' in output


@pytest.mark.asyncio
async def test_image_only_fragment_is_not_skipped_as_no_text(monkeypatch):
    import api.index as index

    processed = []

    async def process(incoming, customer_context):
        processed.append(incoming)
        return AgentResult(reply_text="ok", intent="commerce")

    async def send(*_args):
        return BrevoSendResult(
            ok=True,
            dry_run=True,
            provider_response={"accepted": True},
        )

    monkeypatch.setattr(index, "inbound_message_exists", lambda *_args: False)
    monkeypatch.setattr(index, "claim_inbound_message", lambda _message: (True, 202))
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *_args: True)
    monkeypatch.setattr(index, "find_customer_profile_by_phone", lambda _phone: {})
    monkeypatch.setattr(index, "process_incoming_message", process)
    monkeypatch.setattr(index, "send_brevo_reply", send)
    monkeypatch.setattr(index, "insert_agent_response", lambda _data: None)

    payload = {
        "eventName": "conversationFragment",
        "conversationId": "conversation-img",
        "messages": [
            {
                "type": "visitor",
                "id": "message-img-1",
                "file": {
                    "link": "https://cdn.example.com/watch.jpg",
                    "mimeType": "image/jpeg",
                    "type": "image",
                    "name": "watch.jpg",
                },
            }
        ],
        "visitor": {
            "id": "visitor-1",
            "attributes": {"WHATSAPP": "5511999999999"},
        },
    }

    response = await _post_webhook(index, payload)

    assert response.status_code == 200
    assert len(processed) == 1
    assert processed[0].image_url == "https://cdn.example.com/watch.jpg"
    assert processed[0].attachment_type == "image"


@pytest.mark.asyncio
async def test_conversation_busy_returns_200_skip_not_503(monkeypatch):
    import api.index as index
    from app.conversation_lock import ConversationLockUnavailable

    async def busy_lock(*_args, **_kwargs):
        raise ConversationLockUnavailable("database_lock_busy")

    monkeypatch.setattr(index, "inbound_message_exists", lambda *_args: False)
    monkeypatch.setattr(index, "acquire_conversation_lock", busy_lock)
    monkeypatch.setattr(
        index,
        "claim_inbound_message",
        lambda _message: (_ for _ in ()).throw(
            AssertionError("busy conversation must not claim")
        ),
    )
    monkeypatch.setattr(
        index,
        "process_incoming_message",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("busy conversation must not process")
        ),
    )

    response = await _post_webhook(index, _fragment_payload())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "skipped": True,
        "reason": "conversation_busy",
    }


@pytest.mark.asyncio
async def test_database_lock_unavailable_falls_back_to_local_lock(monkeypatch):
    import api.index as index
    from app.conversation_lock import ConversationLockUnavailable
    from types import SimpleNamespace

    calls: list[dict] = []
    processed: list = []

    async def lock_then_local(key, **kwargs):
        calls.append({"key": key, **kwargs})
        if kwargs.get("database_url"):
            raise ConversationLockUnavailable("database_lock_unavailable")
        return type("H", (), {"released": False})()

    async def process(incoming, customer_context):
        processed.append(incoming)
        return AgentResult(reply_text="ok", intent="commerce")

    async def send(*_args):
        return BrevoSendResult(
            ok=True,
            dry_run=True,
            provider_response={"accepted": True},
        )

    real_settings = index.get_settings()
    monkeypatch.setattr(
        index,
        "get_settings",
        lambda: SimpleNamespace(
            **{
                **real_settings.model_dump(),
                "database_url": "postgresql://example/db",
                "agent_conversation_lock_enabled": True,
            }
        ),
    )
    monkeypatch.setattr(index, "inbound_message_exists", lambda *_args: False)
    monkeypatch.setattr(index, "acquire_conversation_lock", lock_then_local)
    monkeypatch.setattr(
        index,
        "claim_inbound_message",
        lambda _message: (True, "inbound-db-fallback"),
    )
    monkeypatch.setattr(index, "find_customer_profile_by_phone", lambda *_a, **_k: {})
    monkeypatch.setattr(index, "process_incoming_message", process)
    monkeypatch.setattr(index, "send_brevo_reply", send)
    monkeypatch.setattr(index, "insert_agent_response", lambda *_a, **_k: None)
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *_a, **_k: True)
    monkeypatch.setattr(index, "has_successful_agent_response", lambda *_a, **_k: False)

    async def release_noop(*_a, **_k):
        return None

    monkeypatch.setattr(index, "release_conversation_lock", release_noop)

    response = await _post_webhook(index, _fragment_payload())

    assert response.status_code == 200
    assert len(calls) >= 2
    assert bool(calls[0].get("database_url"))
    assert calls[1].get("database_url") == ""
    assert len(processed) == 1


def test_event_skip_reason_only_blocks_transcript_event():
    assert webhook_event_skip_reason({
        "eventName": "conversationTranscript",
    }) == "non_inbound_event"
    assert webhook_event_skip_reason({
        "eventName": "conversationFragment",
    }) is None
    assert webhook_event_skip_reason({
        "eventName": "conversationStarted",
    }) is None


@pytest.mark.asyncio
async def test_caption_echo_is_skipped_before_claim(monkeypatch):
    import api.index as index

    monkeypatch.setattr(index, "inbound_message_exists", lambda *_args: False)
    monkeypatch.setattr(index, "is_caption_echo_of_recent_image", lambda _incoming: True)
    monkeypatch.setattr(
        index,
        "claim_inbound_message",
        lambda _message: (_ for _ in ()).throw(
            AssertionError("caption echo must not be claimed")
        ),
    )
    monkeypatch.setattr(
        index,
        "process_incoming_message",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("caption echo must not be processed")
        ),
    )

    response = await _post_webhook(index, _fragment_payload())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "skipped": True,
        "reason": "caption_echo",
    }


@pytest.mark.asyncio
async def test_photo_without_caption_still_processed(monkeypatch):
    """Image-only inbound must not be treated as caption_echo."""
    import api.index as index

    processed = []

    async def process(incoming, customer_context):
        processed.append(incoming)
        return AgentResult(reply_text="ok", intent="commerce")

    async def send(*_args):
        return BrevoSendResult(
            ok=True,
            dry_run=True,
            provider_response={"accepted": True},
        )

    def fail_if_called(_incoming):
        raise AssertionError("image inbound must short-circuit caption echo check")

    monkeypatch.setattr(index, "inbound_message_exists", lambda *_args: False)
    monkeypatch.setattr(index, "is_caption_echo_of_recent_image", fail_if_called)
    monkeypatch.setattr(index, "claim_inbound_message", lambda _message: (True, 303))
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *_args: True)
    monkeypatch.setattr(index, "find_customer_profile_by_phone", lambda _phone: {})
    monkeypatch.setattr(index, "process_incoming_message", process)
    monkeypatch.setattr(index, "send_brevo_reply", send)
    monkeypatch.setattr(index, "insert_agent_response", lambda _data: None)

    payload = {
        "eventName": "conversationFragment",
        "conversationId": "conversation-img-only",
        "messages": [
            {
                "type": "visitor",
                "id": "message-img-only",
                "file": {
                    "link": "https://cdn.example.com/solo.jpg",
                    "mimeType": "image/jpeg",
                    "type": "image",
                },
            }
        ],
        "visitor": {
            "id": "visitor-1",
            "attributes": {"WHATSAPP": "5511999999999"},
        },
    }

    response = await _post_webhook(index, payload)

    assert response.status_code == 200
    assert len(processed) == 1
    assert processed[0].image_url == "https://cdn.example.com/solo.jpg"


@pytest.mark.asyncio
async def test_instagram_unviewable_media_guides_resend(monkeypatch):
    """Brevo Story placeholder must guide resend, not touch human takeover."""
    import api.index as index
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("BREVO_ALLOWED_CHANNELS", "whatsapp,instagram,facebook")
    get_settings.cache_clear()

    sent = []
    takeover_touched = []

    async def send(incoming, agent_result):
        sent.append((incoming, agent_result))
        return BrevoSendResult(
            ok=True,
            dry_run=True,
            provider_response={"accepted": True},
        )

    monkeypatch.setattr(index, "inbound_message_exists", lambda *_args: False)
    monkeypatch.setattr(index, "claim_inbound_message", lambda _message: (True, 404))
    monkeypatch.setattr(index, "insert_agent_response", lambda _data: None)
    monkeypatch.setattr(index, "send_brevo_reply", send)
    monkeypatch.setattr(
        "app.human_takeover.touch_human_activity",
        lambda incoming: takeover_touched.append(incoming),
    )

    payload = {
        "eventName": "conversationFragment",
        "conversationId": "conversation-ig-story",
        "messages": [
            {
                "type": "agent",
                "id": "message-unviewable",
                "text": (
                    "⚠️ *This message cannot be viewed in Brevo.* "
                    "Please go to Instagram app to view it."
                ),
            }
        ],
        "visitor": {
            "id": "visitor-ig-1",
            "source": "instagram",
            "attributes": {"USERNAME": "cliente_teste"},
        },
    }

    response = await _post_webhook(index, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["reason"] == "instagram_media_unviewable_guided"
    assert len(sent) == 1
    assert "reenviar" in sent[0][1].reply_text.casefold()
    assert takeover_touched == []
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_instagram_unviewable_media_skipped_when_channel_disabled(monkeypatch):
    import api.index as index
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.delenv("BREVO_ALLOWED_CHANNELS", raising=False)
    get_settings.cache_clear()

    monkeypatch.setattr(
        index,
        "send_brevo_reply",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not reply")),
    )
    monkeypatch.setattr(
        index,
        "claim_inbound_message",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not claim")),
    )

    payload = {
        "eventName": "conversationFragment",
        "conversationId": "conversation-ig-off",
        "messages": [
            {
                "type": "agent",
                "id": "message-unviewable-off",
                "text": (
                    "⚠️ *This message cannot be viewed in Brevo.* "
                    "Please go to Instagram app to view it."
                ),
            }
        ],
        "visitor": {
            "id": "visitor-ig-off",
            "source": "instagram",
        },
    }

    response = await _post_webhook(index, payload)
    assert response.status_code == 200
    assert response.json()["reason"] == "agent_message"
    get_settings.cache_clear()
