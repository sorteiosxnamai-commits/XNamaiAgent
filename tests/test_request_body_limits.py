import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.request_body import read_limited_request_body


def request_for(chunks, headers=()):
    source = iter(chunks)
    async def receive():
        part = next(source, None)
        return {"type": "http.request", "body": part or b"", "more_body": part is not None}
    return Request({"type": "http", "method": "POST", "path": "/", "headers": list(headers)}, receive)


@pytest.mark.asyncio
async def test_signed_body_keeps_exact_bytes_and_can_be_read_again():
    body = b'{ "text": "hello" }\n'
    request = request_for([body[:5], body[5:]])
    assert await read_limited_request_body(request, max_bytes=len(body)) == body
    assert await request.body() == body


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [[], [(b"content-length", b"2")], [(b"content-length", b"invalid")]])
async def test_streamed_oversize_body_rejected_even_without_honest_length(headers):
    request = request_for([b"1234", b"5678"], headers)
    with pytest.raises(HTTPException) as error:
        await read_limited_request_body(request, max_bytes=7)
    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_declared_oversize_is_rejected_before_reading():
    request = request_for([], [(b"content-length", b"100")])
    with pytest.raises(HTTPException) as error:
        await read_limited_request_body(request, max_bytes=10)
    assert error.value.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/api/webhooks/meta", "/api/webhooks/brevo/whatsapp"])
async def test_webhook_routes_reject_oversized_payloads(monkeypatch, route):
    from httpx import ASGITransport, AsyncClient
    from api import index
    monkeypatch.setattr("app.channels.meta_instagram.meta_webhook_enabled", lambda: True)
    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=index.app), base_url="http://test") as client:
            response = await client.post(route, content=b"x" * (1024 * 1024 + 1))
        assert response.status_code == 413
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)


@pytest.mark.asyncio
async def test_meta_does_not_acknowledge_when_enqueue_failed(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from api import index
    from app.models import IncomingMessage
    monkeypatch.setattr("app.channels.meta_instagram.meta_webhook_enabled", lambda: True)
    monkeypatch.setattr("app.channels.meta_instagram.verify_meta_signatures", lambda **kwargs: True)
    monkeypatch.setattr("app.channels.meta_instagram.parse_meta_instagram_messaging", lambda _: [
        IncomingMessage(provider="meta", channel="instagram", message_id="m1", text="oi")])
    monkeypatch.setattr("app.ingress.inbox.enqueue_inbound", lambda **kwargs: (False, None))
    async with AsyncClient(transport=ASGITransport(app=index.app), base_url="http://test") as client:
        response = await client.post("/api/webhooks/meta", json={})
    assert response.status_code == 503
