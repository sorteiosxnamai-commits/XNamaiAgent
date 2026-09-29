from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import chatbo_sync


WORKSPACE = "aa774d20-509f-4d54-865b-7a5de22b6d30"


def row():
    return {
        "id": 11,
        "workspace_id": WORKSPACE,
        "provider": "ycloud",
        "event_type": "whatsapp.inbound_message.received",
        "message_id": "wamid.in-11",
        "conversation_id": "wa:5511999999999",
        "channel": "whatsapp",
        "sender_key": "whatsapp:5511999999999",
        "sender_phone": "5511999999999",
        "sender_name": "Cliente",
        "text": "Olá",
        "channel_metadata": {"ycloud_to": "5511955575530"},
        "created_at": "2026-09-28T10:00:00+00:00",
        "response_id": 22,
        "reply_text": "Oi! Como posso ajudar?",
        "intent": "greeting",
        "handoff_required": False,
        "safety_reason": None,
        "provider_send_ok": True,
        "provider_response": {"provider": "ycloud"},
        "response_created_at": "2026-09-28T10:00:01+00:00",
    }


def test_payload_carries_server_workspace_metadata_without_raw_provider_body():
    payload = chatbo_sync._payload(row(), WORKSPACE)

    assert payload["inbound"]["provider"] == "ycloud"
    assert payload["inbound"]["messageId"] == "wamid.in-11"
    assert payload["inbound"]["channelMetadata"]["chatbo_workspace_id"] == WORKSPACE
    assert payload["response"]["providerSendOk"] is True
    assert "raw" not in payload["inbound"]


@pytest.mark.asyncio
async def test_sync_posts_internal_turn_and_marks_both_rows(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, *, json, headers):
            captured.update(url=url, json=json, headers=headers)
            return Response()

    monkeypatch.setattr(chatbo_sync, "get_settings", lambda: SimpleNamespace(
        chatbo_sync_enabled=True,
        chatbo_api_url="https://chatbo.example/api",
        chatbo_internal_token="internal-secret",
        chatbo_workspace_id=WORKSPACE,
    ))
    monkeypatch.setattr(chatbo_sync, "_load_turn", lambda *_a, **_k: row())
    marked = []
    monkeypatch.setattr(chatbo_sync, "_mark_synced", lambda *args: marked.append(args))
    monkeypatch.setattr(chatbo_sync.httpx, "AsyncClient", lambda **_k: Client())

    result = await chatbo_sync.sync_chatbo_turn(inbound_id=11, response_id=22)

    assert result["ok"] is True
    assert captured["url"].endswith(f"/internal/workspaces/{WORKSPACE}/conversation-turns")
    assert captured["headers"] == {"X-NITRUS-Internal-Token": "internal-secret"}
    assert marked == [(11, 22)]
