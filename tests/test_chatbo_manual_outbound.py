from types import SimpleNamespace

from fastapi.testclient import TestClient


def _settings():
    return SimpleNamespace(
        chatbo_internal_token="shared-token",
        chatbo_workspace_id="aa774d20-509f-4d54-865b-7a5de22b6d30",
    )


def test_chatbo_manual_outbound_uses_ycloud(monkeypatch):
    import api.index as index
    import app.security as security
    import app.channels.ycloud_whatsapp as ycloud

    monkeypatch.setattr(index, "get_settings", _settings)
    monkeypatch.setattr(security, "get_settings", _settings)
    calls = []

    async def fake_send(incoming, result):
        calls.append((incoming, result))
        return {"ok": True, "provider": "ycloud", "wamid": "wamid.1"}

    monkeypatch.setattr(ycloud, "send_ycloud_reply", fake_send)
    response = TestClient(index.app).post(
        "/api/internal/chatbo/outbound",
        headers={"Authorization": "Bearer shared-token"},
        json={
            "workspaceId": "aa774d20-509f-4d54-865b-7a5de22b6d30",
            "recipient": "whatsapp:558599498149",
            "content": "Ricardo: oi",
            "correlationId": "message-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["provider"] == "ycloud"
    assert calls[0][0].sender_phone == "whatsapp:558599498149"
    assert calls[0][1].reply_text == "Ricardo: oi"


def test_chatbo_manual_outbound_rejects_other_workspace(monkeypatch):
    import api.index as index
    import app.security as security

    monkeypatch.setattr(index, "get_settings", _settings)
    monkeypatch.setattr(security, "get_settings", _settings)
    response = TestClient(index.app).post(
        "/api/internal/chatbo/outbound",
        headers={"Authorization": "Bearer shared-token"},
        json={"workspaceId": "ns-workspace", "recipient": "+5511999999999", "content": "oi"},
    )
    assert response.status_code == 403
