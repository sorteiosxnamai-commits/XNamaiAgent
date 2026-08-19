from types import SimpleNamespace

import pytest

from app.models import AgentResult, BrevoSendResult, IncomingMessage


def _settings(**overrides):
    values = {
        "brevo_api_key": "test-key",
        "brevo_agent_id": "agent-1",
        "brevo_agent_email": "",
        "brevo_agent_name": "NewStoreAgent",
        "brevo_received_from": "NewStoreAgent",
        "brevo_sender_number": "5511000000000",
        "brevo_send_url": "",
        "brevo_reply_mode": "auto",
        "brevo_send_audio_as_attachment": True,
        "dry_run": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_facebook_reply_uses_conversations_visitor_id_payload(monkeypatch):
    import app.brevo_client as brevo

    captured = {}

    class Response:
        status_code = 201
        text = ""

        def json(self):
            return {"id": "outbound-1"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json, headers):
            captured.update({"url": url, "json": json, "headers": headers})
            return Response()

    monkeypatch.setattr(brevo, "get_settings", lambda: _settings())
    monkeypatch.setattr(brevo.httpx, "AsyncClient", Client)

    result = await brevo._send_conversations_reply(
        IncomingMessage(channel="facebook", visitor_id="brevo-visitor-fb"),
        "Segue o link oficial: https://loja.example/produto",
    )

    assert result.ok is True
    assert captured["url"] == brevo.BREVO_CONVERSATIONS_SEND_URL
    assert captured["json"] == {
        "text": "Segue o link oficial: https://loja.example/produto",
        "visitorId": "brevo-visitor-fb",
        "agentId": "agent-1",
    }


@pytest.mark.asyncio
async def test_social_channel_never_falls_into_whatsapp_transactional(monkeypatch):
    import app.brevo_client as brevo

    calls = []
    monkeypatch.setattr(brevo, "get_settings", lambda: _settings(brevo_reply_mode="whatsapp"))

    async def conversations(incoming, text, audio_file=None):
        calls.append(("conversations", incoming.visitor_id, text, audio_file))
        return BrevoSendResult(ok=True, dry_run=False)

    async def whatsapp(*_args):
        raise AssertionError("Facebook must not use the WhatsApp endpoint")

    monkeypatch.setattr(brevo, "_send_conversations_reply", conversations)
    monkeypatch.setattr(brevo, "_send_whatsapp_transactional_reply", whatsapp)

    sent = await brevo.send_brevo_reply(
        IncomingMessage(
            channel="facebook",
            visitor_id="visitor-fb",
            sender_phone="5511999999999",
        ),
        AgentResult(reply_text="Olá"),
    )

    assert sent.ok is True
    assert calls == [("conversations", "visitor-fb", "Olá", None)]


@pytest.mark.asyncio
async def test_auto_whatsapp_keeps_transactional_number_route(monkeypatch):
    import app.brevo_client as brevo

    calls = []
    monkeypatch.setattr(brevo, "get_settings", lambda: _settings())

    async def whatsapp(incoming, text):
        calls.append((incoming.sender_phone, text))
        return BrevoSendResult(ok=True, dry_run=False)

    monkeypatch.setattr(brevo, "_send_whatsapp_transactional_reply", whatsapp)

    sent = await brevo.send_brevo_reply(
        IncomingMessage(channel="whatsapp", sender_phone="+55 11 99999-9999"),
        "Resposta",
    )

    assert sent.ok is True
    assert calls == [("+55 11 99999-9999", "Resposta")]
