from types import SimpleNamespace

from fastapi.testclient import TestClient


def _settings():
    return SimpleNamespace(
        chatbo_internal_token="shared-token",
        chatbo_workspace_id="aa774d20-509f-4d54-865b-7a5de22b6d30",
    )


def test_chatbo_manual_outbound_uses_ycloud(monkeypatch):
    import api.index as index
    import app.chatbo_manual_outbound as outbound
    import app.channels.ycloud_whatsapp as ycloud

    monkeypatch.setattr(index, "get_settings", _settings)
    calls = []
    finished = []

    monkeypatch.setattr(outbound, "claim_message", lambda *_: {
        "id": "3d4f35c0-d138-4e73-9833-7df24aa091b6",
        "contact_phone": "whatsapp:558599498149",
        "content": "Ricardo: oi",
    })
    monkeypatch.setattr(outbound, "finish_message", lambda *args: finished.append(args))

    async def fake_send(incoming, result):
        calls.append((incoming, result))
        return {"ok": True, "provider": "ycloud", "wamid": "wamid.1"}

    monkeypatch.setattr(ycloud, "send_ycloud_reply", fake_send)
    response = TestClient(index.app).post(
        "/api/internal/chatbo/outbound",
        json={"messageId": "3d4f35c0-d138-4e73-9833-7df24aa091b6"},
    )
    assert response.status_code == 200
    assert response.json()["provider"] == "ycloud"
    assert calls[0][0].sender_phone == "whatsapp:558599498149"
    assert calls[0][1].reply_text == "Ricardo: oi"
    assert finished


def test_chatbo_manual_outbound_rejects_unknown_message(monkeypatch):
    import api.index as index
    import app.chatbo_manual_outbound as outbound

    monkeypatch.setattr(index, "get_settings", _settings)
    monkeypatch.setattr(outbound, "claim_message", lambda *_: None)
    response = TestClient(index.app).post(
        "/api/internal/chatbo/outbound",
        json={"messageId": "3d4f35c0-d138-4e73-9833-7df24aa091b6"},
    )
    assert response.status_code == 409
