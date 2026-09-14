"""Cliente HTTP do MercosAdaptor. Transporte simulado — nunca a Mercos real.

Contrato coberto (lido em sorteiosxnamai-commits/MercosAdaptor @38f0bf4):
  GET  /health
  GET  /v1/resources
  GET  /v1/{resource}?alterado_apos=...  -> {resource,count,pageCursor,nextCursor,data}
  GET  /v1/{customers|products|orders}/{mercos_id}
  POST /v1/customers | PUT /v1/customers/{id}
  POST /v1/orders    | PUT /v1/orders/{id}

Auth: header ``X-API-Key``. Erro: ``{"error": ..., "details": ...}``; 429 com
``Retry-After`` e ``tempo_ate_permitir_novamente``.
"""

from __future__ import annotations

import httpx
import pytest

from app.commerce.mercos.client import MercosAdaptorClient, MercosAdaptorError

BASE = "https://adaptor.example.com"
KEY = "internal-key"


def _client(handler, **kwargs) -> MercosAdaptorClient:
    return MercosAdaptorClient(
        base_url=BASE,
        api_key=KEY,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


# --- auth e construcao de URL ----------------------------------------------


@pytest.mark.asyncio
async def test_sends_internal_api_key_header():
    seen = {}

    def handler(request: httpx.Request):
        seen["key"] = request.headers.get("X-API-Key")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"status": "ok"})

    await _client(handler).health()
    assert seen["key"] == KEY
    assert seen["url"] == BASE + "/health"


@pytest.mark.asyncio
async def test_never_sends_mercos_tokens():
    seen = {}

    def handler(request: httpx.Request):
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, json={"status": "ok"})

    await _client(handler).health()
    for forbidden in ("applicationtoken", "companytoken", "authorization"):
        assert forbidden not in seen["headers"], "token Mercos vazou: " + forbidden


@pytest.mark.asyncio
async def test_base_url_trailing_slash_is_normalized():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"resources": []})

    client = MercosAdaptorClient(
        base_url=BASE + "/",
        api_key=KEY,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    await client.resources()
    assert seen["url"] == BASE + "/v1/resources"


# --- envelope de listagem ---------------------------------------------------


@pytest.mark.asyncio
async def test_list_resource_returns_the_adaptor_envelope():
    def handler(request: httpx.Request):
        assert request.url.path == "/v1/products"
        assert request.url.params["alterado_apos"] == "2026-01-01T00:00:00"
        return httpx.Response(
            200,
            json={
                "resource": "products",
                "count": 2,
                "pageCursor": "2026-01-02T00:00:00",
                "nextCursor": "2026-01-02T00:00:00",
                "data": [
                    {"id": 1, "ultima_alteracao": "2026-01-01T00:00:00"},
                    {"id": 2, "ultima_alteracao": "2026-01-02T00:00:00"},
                ],
            },
        )

    page = await _client(handler).list_resource(
        "products", changed_after="2026-01-01T00:00:00"
    )
    assert page.resource == "products"
    assert page.count == 2
    assert page.next_cursor == "2026-01-02T00:00:00"
    assert page.page_cursor == "2026-01-02T00:00:00"
    assert [row["id"] for row in page.data] == [1, 2]
    assert page.has_more is True


@pytest.mark.asyncio
async def test_last_page_has_no_next_cursor():
    def handler(_):
        return httpx.Response(
            200,
            json={
                "resource": "products",
                "count": 1,
                "pageCursor": "2026-01-01T00:00:00",
                "nextCursor": None,
                "data": [{"id": 1, "ultima_alteracao": "2026-01-01T00:00:00"}],
            },
        )

    page = await _client(handler).list_resource("products")
    assert page.next_cursor is None
    assert page.has_more is False


@pytest.mark.asyncio
async def test_omits_cursor_param_when_not_given():
    def handler(request: httpx.Request):
        assert "alterado_apos" not in request.url.params
        return httpx.Response(
            200,
            json={
                "resource": "products",
                "count": 0,
                "pageCursor": None,
                "nextCursor": None,
                "data": [],
            },
        )

    await _client(handler).list_resource("products")


# --- detalhe ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_detail_builds_the_documented_path():
    def handler(request: httpx.Request):
        assert request.url.path == "/v1/orders/42"
        return httpx.Response(200, json={"id": 42, "itens": [{"id": 7, "produto_id": 3}]})

    detail = await _client(handler).get_detail("orders", "42")
    assert detail["id"] == 42


# --- erros ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_surfaces_retry_after_and_does_not_busy_retry():
    calls = {"n": 0}

    def handler(_):
        calls["n"] += 1
        return httpx.Response(
            429,
            json={"error": "Too Many Requests", "details": {"tempo_ate_permitir_novamente": 30}},
            headers={"Retry-After": "30"},
        )

    with pytest.raises(MercosAdaptorError) as exc:
        await _client(handler).list_resource("products")
    assert exc.value.status_code == 429
    assert exc.value.retry_after == 30
    assert calls["n"] == 1, "429 nao pode virar retry agressivo: o adaptor ja tenta"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
async def test_http_errors_become_structured_errors(status):
    def handler(_):
        return httpx.Response(status, json={"error": "falhou", "details": {"x": 1}})

    with pytest.raises(MercosAdaptorError) as exc:
        await _client(handler).list_resource("products")
    assert exc.value.status_code == status
    assert exc.value.code


@pytest.mark.asyncio
async def test_invalid_json_becomes_structured_error():
    def handler(_):
        return httpx.Response(200, content=b"<html>nao eh json</html>")

    with pytest.raises(MercosAdaptorError) as exc:
        await _client(handler).list_resource("products")
    assert exc.value.code == "invalid_response"


@pytest.mark.asyncio
async def test_incomplete_envelope_becomes_structured_error():
    def handler(_):
        return httpx.Response(200, json={"resource": "products"})

    with pytest.raises(MercosAdaptorError) as exc:
        await _client(handler).list_resource("products")
    assert exc.value.code == "invalid_response"


@pytest.mark.asyncio
async def test_transport_failure_retries_a_little_then_fails_closed():
    calls = {"n": 0}

    def handler(_):
        calls["n"] += 1
        raise httpx.ConnectError("sem rede")

    with pytest.raises(MercosAdaptorError) as exc:
        await _client(handler, max_transport_retries=2).list_resource("products")
    assert exc.value.code == "transport_error"
    assert calls["n"] == 2, "retry de transporte deve ser limitado"


# --- mutacoes: nunca repetir cegamente --------------------------------------


@pytest.mark.asyncio
async def test_mutations_are_never_retried_on_transport_failure():
    calls = {"n": 0}

    def handler(_):
        calls["n"] += 1
        raise httpx.ConnectError("sem rede")

    with pytest.raises(MercosAdaptorError):
        await _client(handler, max_transport_retries=3).create_order({"x": 1})
    assert calls["n"] == 1, "POST nao pode ser repetido sem idempotencia garantida"


@pytest.mark.asyncio
async def test_create_order_posts_to_the_documented_path():
    seen = {}

    def handler(request: httpx.Request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"id": 99})

    await _client(handler).create_order({"cliente_id": 1})
    assert (seen["method"], seen["path"]) == ("POST", "/v1/orders")


@pytest.mark.asyncio
async def test_update_customer_puts_to_the_documented_path():
    seen = {}

    def handler(request: httpx.Request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"id": 5})

    await _client(handler).update_customer("5", {"nome": "x"})
    assert (seen["method"], seen["path"]) == ("PUT", "/v1/customers/5")


# --- segredos ---------------------------------------------------------------


def test_error_repr_never_leaks_the_api_key():
    err = MercosAdaptorError("falhou", status_code=500, code="x", details={"api_key": KEY})
    assert KEY not in repr(err)
    assert KEY not in str(err)


@pytest.mark.asyncio
async def test_error_details_are_sanitized():
    def handler(_):
        return httpx.Response(500, json={"error": "falhou", "details": {"apiKey": KEY, "ok": 1}})

    with pytest.raises(MercosAdaptorError) as exc:
        await _client(handler).list_resource("products")
    assert KEY not in str(exc.value.details)
    assert exc.value.details.get("ok") == 1
