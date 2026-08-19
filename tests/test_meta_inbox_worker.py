from app.ingress.reconstruct import incoming_from_inbox_payload
from app.inbound_coalesce import attach_recent_image_for_followup
from app.models import IncomingMessage


def test_reconstruct_meta_story_message():
    payload = {
        "normalized": {
            "provider": "meta",
            "channel": "instagram",
            "message_id": "m1",
            "text": "valor",
            "image_url": "https://cdn.example/story.jpg",
            "attachment_type": "image",
            "input_modality": "text_with_image",
            "sender_key": "instagram:user-1",
            "sender_external_id": "user-1",
            "visitor_id": "user-1",
            "conversation_id": "ig:user-1",
            "instagram_story": {
                "provider": "meta",
                "instagram_account_id": "17841404241547355",
                "story_media_id": "story-1",
                "replied_to_story": True,
                "mentioned_in_story": False,
                "media_type": "image",
            },
            "raw": {},
        }
    }
    incoming = incoming_from_inbox_payload(payload)
    assert incoming is not None
    assert incoming.provider == "meta"
    assert incoming.image_url == "https://cdn.example/story.jpg"
    assert incoming.instagram_story is not None
    assert incoming.instagram_story.replied_to_story is True
    assert incoming.instagram_story.operational_media_url() == (
        "https://cdn.example/story.jpg"
    )


def test_attach_recent_image_for_valor(monkeypatch):
    from app import inbound_coalesce as module

    monkeypatch.setattr(
        module,
        "recent_image_inbound_for_echo",
        lambda **_kwargs: {
            "id": 42,
            "text": "[Imagem recebida via Instagram]",
            "channel_metadata": {
                "image_url": "https://cdn.example/watch.jpg",
                "image_url_present": True,
            },
        },
    )
    incoming = IncomingMessage(
        channel="instagram",
        provider="meta",
        text="valor",
        conversation_id="ig:user-1",
        sender_key="instagram:user-1",
    )
    updated = attach_recent_image_for_followup(incoming)
    assert updated.image_url == "https://cdn.example/watch.jpg"
    assert updated.attachment_type == "image"
    assert updated.input_modality == "text_with_image"


def test_meta_provider_skips_human_takeover(monkeypatch):
    import asyncio

    from app.ingress import worker as worker_mod
    from app.models import AgentResult

    monkeypatch.setattr(
        worker_mod,
        "incoming_from_inbox_payload",
        lambda *_args, **_kwargs: IncomingMessage(
            provider="meta",
            channel="instagram",
            text="teste",
            sender_key="instagram:user-1",
            sender_external_id="user-1",
            visitor_id="user-1",
            conversation_id="ig:user-1",
        ),
    )
    monkeypatch.setattr(worker_mod, "attach_recent_image_for_followup", lambda incoming: incoming)
    monkeypatch.setattr(
        "app.human_takeover.human_takeover_active",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(worker_mod, "claim_inbound_message", lambda *_args, **_kwargs: (True, 9))
    monkeypatch.setattr(worker_mod, "has_successful_agent_response", lambda *_args, **_kwargs: False)

    async def fake_process(*_args, **_kwargs):
        return AgentResult(reply_text="ok", intent="greeting")

    async def fake_send(*_args, **_kwargs):
        return {"ok": True}

    monkeypatch.setattr("app.message_pipeline.process_incoming_message", fake_process)
    monkeypatch.setattr(worker_mod, "_send_reply", fake_send)
    monkeypatch.setattr(worker_mod, "insert_agent_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_mod, "mark_inbox_processed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_mod, "mark_inbox_failed", lambda *_args, **_kwargs: None)

    result = asyncio.run(
        worker_mod.process_inbox_row({"id": 1, "payload_json": {}, "attempts": 1})
    )
    assert result["ok"] is True
    assert result.get("skipped") != "human_takeover"
    assert result.get("inbound_id") == 9
