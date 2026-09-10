import pytest

from app.commerce.errors import COMMERCE_UNAVAILABLE_CODE, CommerceUnavailableError


def test_unavailable_code_is_stable():
    assert COMMERCE_UNAVAILABLE_CODE == "commerce_provider_unavailable"


def test_unavailable_error_carries_code_and_capability():
    err = CommerceUnavailableError("search_products")
    assert err.code == COMMERCE_UNAVAILABLE_CODE
    assert err.capability == "search_products"
    assert "commerce_provider_unavailable" in str(err)


from app.commerce.provider import (
    NullCommerceProvider,
    get_commerce_provider,
    reset_commerce_provider,
    set_commerce_provider,
)


@pytest.mark.asyncio
async def test_null_provider_always_raises_unavailable():
    provider = NullCommerceProvider()
    with pytest.raises(CommerceUnavailableError) as exc:
        await provider.execute("search_products", {"query": "x"})
    assert exc.value.code == COMMERCE_UNAVAILABLE_CODE


def test_default_provider_is_null():
    reset_commerce_provider()
    assert isinstance(get_commerce_provider(), NullCommerceProvider)


def test_provider_is_replaceable():
    class FakeProvider:
        name = "fake"

        async def execute(self, capability, arguments):
            return {"ok": True, "capability": capability}

    try:
        set_commerce_provider(FakeProvider())
        assert get_commerce_provider().name == "fake"
    finally:
        reset_commerce_provider()
