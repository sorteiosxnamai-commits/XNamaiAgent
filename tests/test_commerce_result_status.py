"""B8 — "o Mercos caiu" nunca pode virar "esse produto nao existe"."""

from __future__ import annotations

import pytest

from app.commerce.result_status import (
    RESULT_STATUS_KEY,
    CommerceResultStatus as S,
    classify_commerce_result,
)
from app.models import IncomingMessage

_FRESH = {"price_confirmed": True, "stock_confirmed": True}
_STALE = {"price_confirmed": True, "stock_confirmed": False}


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ({"ok": True, "products": [{"id": "1", "freshness": _FRESH}]}, S.FOUND),
        ({"ok": True, "product": {"id": "1", "freshness": _FRESH}}, S.FOUND),
        ({"ok": True, "products": []}, S.NOT_FOUND),
        ({"ok": False, "error": "product_not_found", "product_id": "9"}, S.NOT_FOUND),
        ({"ok": False, "error": "commerce_product_unavailable"}, S.NOT_FOUND),
        ({"ok": True, "products": [{"id": "1", "freshness": _STALE}]}, S.PARTIAL),
        ({"ok": True, "product_id": "1", "stock": 3, "stock_confirmed": False}, S.PARTIAL),
        ({"ok": False, "error": "commerce_provider_unavailable"}, S.PROVIDER_UNAVAILABLE),
        ({"ok": False, "error": "commerce_provider_error", "code": "rate_limited", "status_code": 429},
         S.PROVIDER_UNAVAILABLE),
        ({"error": "commerce_tool_error", "error_type": "ReadTimeout"}, S.TIMEOUT),
        ({"ok": False, "error": "commerce_provider_error", "code": "transport_error", "status_code": 504},
         S.TIMEOUT),
        ({"ok": False, "error": "commerce_provider_error", "code": "invalid_response"}, S.INVALID_RESPONSE),
        ({"ok": True, "products": "not-a-list"}, S.INVALID_RESPONSE),
        ("garbage", S.INVALID_RESPONSE),
        ({"ok": False, "error": "missing_argument", "argument": "product_id"}, S.INVALID_REQUEST),
    ],
)
def test_every_outcome_has_its_own_status(result, status):
    assert classify_commerce_result(result) is status


def test_only_an_answering_source_can_support_not_found():
    assert S.NOT_FOUND.source_answered and S.FOUND.source_answered and S.PARTIAL.source_answered
    for outage in (S.PROVIDER_UNAVAILABLE, S.TIMEOUT, S.INVALID_RESPONSE, S.INVALID_REQUEST):
        assert not outage.source_answered


class _Provider:
    name = "fake"
    available = True

    def __init__(self, result=None, raises=None):
        self._result, self._raises = result, raises

    async def execute(self, capability, arguments):
        if self._raises:
            raise self._raises
        return dict(self._result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "status"),
    [
        (_Provider({"ok": True, "products": [{"id": "1", "freshness": _FRESH}]}), "found"),
        (_Provider({"ok": True, "products": []}), "not_found"),
        (_Provider(raises=TimeoutError("slow")), "timeout"),
        (_Provider(raises=ConnectionError("down")), "provider_unavailable"),
    ],
)
async def test_execute_tool_stamps_the_status(monkeypatch, provider, status):
    import app.commerce.tools as tools

    monkeypatch.setattr(tools, "get_commerce_provider", lambda: provider)
    result = await tools.execute_tool("search_products", {"query": "cabo"})
    assert result[RESULT_STATUS_KEY] == status


@pytest.mark.asyncio
async def test_null_provider_is_reported_as_unavailable_not_empty(monkeypatch):
    import app.commerce.tools as tools
    from app.commerce.provider import NullCommerceProvider

    monkeypatch.setattr(tools, "get_commerce_provider", lambda: NullCommerceProvider())
    result = await tools.execute_tool("search_products", {"query": "cabo"})
    assert result[RESULT_STATUS_KEY] == "provider_unavailable"


_DENIALS = ("não encontrei", "nao encontrei", "não existe", "nao existe", "não temos")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        {"ok": False, "error": "commerce_provider_unavailable"},
        {"error": "commerce_tool_error", "error_type": "ReadTimeout"},
        {"ok": False, "error": "commerce_provider_error", "code": "transport_error", "status_code": 503},
    ],
)
async def test_outage_never_becomes_product_does_not_exist(monkeypatch, failure):
    import app.commerce_router as router

    async def tool(name, arguments):
        return dict(failure)

    monkeypatch.setattr(router, "execute_tool", tool)
    result = await router.handle_commerce_message(
        IncomingMessage(text="tem cabo USB-C FKT-110C?"), {}, {}, action="product_search", query="FKT-110C"
    )
    assert result.safety_reason == "commerce_provider_unavailable"
    assert not any(denial in result.reply_text.casefold() for denial in _DENIALS)


@pytest.mark.asyncio
async def test_source_answering_not_found_is_stated_as_not_found(monkeypatch):
    import app.commerce_router as router

    async def tool(name, arguments):
        return {"ok": True, "products": []}

    monkeypatch.setattr(router, "execute_tool", tool)
    result = await router.handle_commerce_message(
        IncomingMessage(text="tem FKT-999?"), {}, {}, action="product_search", query="FKT-999"
    )
    assert result.safety_reason == "product_not_found"


@pytest.mark.asyncio
async def test_remembered_product_gone_is_not_found_not_outage(monkeypatch):
    """Antes: produto removido do catalogo virava "nao consegui consultar"."""
    import app.commerce_router as router

    async def tool(name, arguments):
        return {"ok": False, "error": "product_not_found", "product_id": "7"}

    monkeypatch.setattr(router, "execute_tool", tool)
    monkeypatch.setattr(router, "_remembered_product", lambda _m: {"id": "7", "name": "Cabo"})
    monkeypatch.setattr(router, "_is_follow_up_without_product", lambda _q: True)
    result = await router.handle_commerce_message(
        IncomingMessage(text="e o preço dele?"), {}, {}, action="product_price", query="dele"
    )
    assert result.safety_reason == "product_not_found"
    assert result.response_metadata["commerce_result_status"] == "not_found"
