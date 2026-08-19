import httpx
import pytest

from app.tray_adapter_client import TrayAdapterClient, TrayAdapterError


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"products": []}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    async def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_product_search_sends_bearer_params_and_limit():
    fake = FakeClient(FakeResponse(payload={"products": []}))
    client = TrayAdapterClient("https://tray.example/", "secret", fake)
    await client.search_products(name="Tissot", ean=None, brand=None, limit=50, page=None)
    args, kwargs = fake.calls[0]
    assert args == ("GET", "https://tray.example/internal/products")
    assert kwargs["headers"] == {"Authorization": "Bearer secret"}
    assert kwargs["params"] == {"name": "Tissot", "limit": 20}


@pytest.mark.asyncio
async def test_order_list_can_filter_by_confirmed_customer_id():
    fake = FakeClient(FakeResponse(payload={"orders": []}))
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    await client.list_orders(customer_id="customer-7")

    args, kwargs = fake.calls[0]
    assert args == ("GET", "https://tray.example/internal/orders")
    assert kwargs["params"] == {"customer_id": "customer-7"}


@pytest.mark.asyncio
async def test_categories_and_variants_use_new_read_only_routes():
    fake = FakeClient()
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    await client.list_categories(limit=500, page=2)
    await client.get_category("10")
    await client.get_category_tree("10")
    await client.list_product_variants("641")
    await client.get_product_variant("123")

    assert [call[0][1] for call in fake.calls] == [
        "https://tray.example/internal/categories",
        "https://tray.example/internal/categories/10",
        "https://tray.example/internal/categories/tree/10",
        "https://tray.example/internal/products/variants",
        "https://tray.example/internal/products/variants/123",
    ]
    assert fake.calls[0][1]["params"] == {"limit": 50, "page": 2}
    assert fake.calls[3][1]["params"] == {"product_id": "641"}


@pytest.mark.asyncio
async def test_cart_uses_exact_adapter_contract_and_omits_optional_fields():
    fake = FakeClient(FakeResponse(payload={
        "cart_id": "C1",
        "session_id": "S1",
        "cart_url": "https://loja.example/checkout/S1",
    }))
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    result = await client.create_cart(
        product_id="641",
        quantity=2,
        price="5439.99",
    )

    args, kwargs = fake.calls[0]
    assert args == ("POST", "https://tray.example/internal/carts")
    assert kwargs["json"] == {
        "product_id": "641",
        "quantity": 2,
        "price": "5439.99",
    }
    assert result["session_id"] == "S1"


@pytest.mark.asyncio
async def test_cart_post_is_never_retried_on_transient_error():
    fake = FakeClient(FakeResponse(status_code=503))
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    with pytest.raises(TrayAdapterError):
        await client.create_cart(
            product_id="641",
            variant_id="123",
            quantity=1,
            price="10.00",
            session_id="S1",
        )

    assert len(fake.calls) == 1
    assert fake.calls[0][1]["json"] == {
        "product_id": "641",
        "variant_id": "123",
        "quantity": 1,
        "price": "10.00",
        "session_id": "S1",
    }


@pytest.mark.asyncio
async def test_shipping_and_order_methods_use_only_internal_adapter_contracts():
    fake = FakeClient(FakeResponse(payload={"success": True, "order_id": 123}))
    client = TrayAdapterClient("https://tray.example", "secret", fake)
    products = [{
        "product_id": "803", "variant_id": None,
        "price": "4699.99", "quantity": 1,
    }]

    await client.quote_shipping(zipcode="19900000", products=products)
    await client.list_shipping_methods()
    await client.create_order({"session_id": "S1", "products": products})
    await client.list_orders(session_id="S1")
    await client.get_order("123")
    await client.get_order_complete("123")
    await client.get_order_payment("123")

    assert [call[0] for call in fake.calls] == [
        ("POST", "https://tray.example/internal/shippings/quote"),
        ("GET", "https://tray.example/internal/shippings/methods"),
        ("POST", "https://tray.example/internal/orders"),
        ("GET", "https://tray.example/internal/orders"),
        ("GET", "https://tray.example/internal/orders/123"),
        ("GET", "https://tray.example/internal/orders/123/complete"),
        ("GET", "https://tray.example/internal/orders/123/payment"),
    ]
    assert fake.calls[0][1]["json"] == {
        "zipcode": "19900000", "products": products,
    }
    assert fake.calls[3][1]["params"] == {"session_id": "S1"}


@pytest.mark.asyncio
async def test_get_cart_uses_adapter_session_route():
    fake = FakeClient(FakeResponse(payload={"session_id": "S1"}))
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    await client.get_cart("S1")

    assert fake.calls[0][0] == (
        "GET",
        "https://tray.example/internal/carts/S1",
    )


@pytest.mark.asyncio
async def test_complete_cart_and_payment_options_use_adapter_routes():
    fake = FakeClient(FakeResponse(payload={}))
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    await client.get_cart_complete("S1")
    await client.get_payment_options("S1")

    assert fake.calls[0][0] == (
        "GET",
        "https://tray.example/internal/carts/S1/complete",
    )
    assert fake.calls[1][0] == (
        "GET",
        "https://tray.example/internal/payments/options",
    )
    assert fake.calls[1][1]["params"] == {"cart_session_id": "S1"}


@pytest.mark.asyncio
async def test_payment_options_accepts_order_scope_and_rejects_ambiguous_scope():
    fake = FakeClient(FakeResponse(payload={}))
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    await client.get_payment_options(order_id="123")

    assert fake.calls[0][1]["params"] == {"order_id": "123"}
    with pytest.raises(ValueError, match="exactly one"):
        await client.get_payment_options("S1", "123")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 502, 503])
async def test_http_errors_are_typed(status):
    fake = FakeClient(FakeResponse(status_code=status))
    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient("https://tray.example", "secret", fake).get_product(1)
    assert error.value.status_code == status


@pytest.mark.asyncio
async def test_connection_failure_is_safe():
    class BrokenClient:
        async def request(self, *args, **kwargs):
            raise httpx.ConnectError("offline")

    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient("https://tray.example", "secret", BrokenClient()).get_product(1)
    assert error.value.status_code is None


@pytest.mark.asyncio
async def test_transient_503_is_retried_once_and_can_recover(monkeypatch):
    import app.tray_adapter_client as tray_client

    class SequenceClient:
        def __init__(self):
            self.responses = [
                FakeResponse(status_code=503),
                FakeResponse(status_code=200, payload={"products": [{"id": "ok"}]}),
            ]
            self.calls = []

        async def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return self.responses.pop(0)

    async def no_wait(_seconds):
        return None

    fake = SequenceClient()
    monkeypatch.setattr(tray_client.asyncio, "sleep", no_wait)

    result = await TrayAdapterClient(
        "https://tray.example",
        "secret",
        fake,
    ).search_products(brand="Doxa", limit=20, page=1)

    assert result["products"] == [{"id": "ok"}]
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_persistent_503_fails_after_single_retry(monkeypatch):
    import app.tray_adapter_client as tray_client

    class AlwaysUnavailableClient:
        def __init__(self):
            self.calls = []

        async def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return FakeResponse(status_code=503)

    async def no_wait(_seconds):
        return None

    fake = AlwaysUnavailableClient()
    monkeypatch.setattr(tray_client.asyncio, "sleep", no_wait)

    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient(
            "https://tray.example",
            "secret",
            fake,
        ).search_products(brand="Doxa", limit=20, page=1)

    assert error.value.status_code == 503
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_non_transient_422_is_not_retried():
    fake = FakeClient(FakeResponse(status_code=422))

    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient(
            "https://tray.example",
            "secret",
            fake,
        ).search_products(brand="Doxa", limit=20, page=1)

    assert error.value.status_code == 422
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_adapter_error_preserves_only_sanitized_contract_diagnostics():
    fake = FakeClient(FakeResponse(
        status_code=400,
        payload={
            "error": {
                "code": "invalid_cart",
                "type": "validation_error",
                "message": (
                    "Campo inválido Authorization: Bearer super-secret "
                    "para pessoa@example.com 5511999999999"
                ),
                "errors": [
                    {
                        "loc": ["body", "price"],
                        "msg": "Preço obrigatório",
                    }
                ],
            }
        },
    ))

    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient(
            "https://tray.example",
            "secret",
            fake,
        ).create_cart(
            product_id="641",
            quantity=1,
            price="0.00",
            session_id="S1",
        )

    diagnostics = error.value.diagnostics
    assert diagnostics["tray_error_code"] == "invalid_cart"
    assert diagnostics["tray_error_type"] == "validation_error"
    assert diagnostics["tray_error_field"] == "body.price"
    assert diagnostics["tray_error_fields"] == ["body.price"]
    assert "super-secret" not in diagnostics["tray_error_message"]
    assert "pessoa@example.com" not in diagnostics["tray_error_message"]
    assert "5511999999999" not in diagnostics["tray_error_message"]
    assert "***" in diagnostics["tray_error_message"]


@pytest.mark.asyncio
async def test_cart_http_400_preserves_adapter_contract_and_logs_only_safe_fields(capsys):
    fake = FakeClient(FakeResponse(
        status_code=400,
        payload={
            "success": False,
            "error": "tray_api_error",
            "tray_error_code": "invalid_cart",
            "tray_error_type": "validation_error",
            "tray_error_field": "Cart.variant_id",
            "tray_error_fields": ["Cart.variant_id"],
            "tray_error_message": "Campo invÃ¡lido Authorization: Bearer super-secret",
        },
    ))

    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient("https://tray.example", "secret", fake).create_cart(
            product_id="803", quantity=1, price="10.00", session_id="S1"
        )

    exc = error.value
    assert exc.status_code == 400
    assert exc.error == "tray_api_error"
    assert exc.tray_error_code == "invalid_cart"
    assert exc.tray_error_type == "validation_error"
    assert exc.tray_error_field == "Cart.variant_id"
    assert exc.tray_error_fields == ["Cart.variant_id"]
    assert "super-secret" not in (exc.tray_error_message or "")
    output = capsys.readouterr().out
    assert "[sales.cart.http.response]" in output
    assert "tray_api_error" in output
    assert "super-secret" not in output
