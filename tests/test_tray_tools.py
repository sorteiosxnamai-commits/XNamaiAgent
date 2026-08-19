import pytest

from app.tray_tools import TOOL_SCHEMAS, _reduce, execute_tool, search_products
from app.tray_adapter_client import TrayAdapterError


class FakeTray:
    def __init__(self):
        self.calls = []

    async def search_products(self, **kwargs):
        self.calls.append(("search_products", kwargs))
        return {"products": [{"id": "641", "name": "Tissot Seastar", "price": 6399.99, "huge": "omit"}]}

    async def get_product(self, product_id):
        self.calls.append(("get_product", product_id))
        return {"id": product_id, "name": "Produto", "current_price": 10, "secret_blob": "omit"}

    async def get_product_stock(self, product_id):
        self.calls.append(("get_product_stock", product_id))
        return {"product_id": product_id, "stock": 0, "available": "0", "upon_request": True, "availability": "sob consulta"}

    async def list_categories(self, **kwargs):
        self.calls.append(("list_categories", kwargs))
        return {
            "categories": [{"id": 10, "name": "Relógios", "secret": "omit"}],
            "paging": {"total": 1, "page": 1, "limit": 50},
        }

    async def get_category_tree(self, category_id):
        self.calls.append(("get_category_tree", category_id))
        return {"id": category_id, "name": "Relógios", "children": [{"id": 11, "name": "Masculinos"}]}

    async def list_product_variants(self, product_id):
        self.calls.append(("list_product_variants", product_id))
        return {"variants": [{"id": "123", "product_id": product_id, "color": "Preto", "stock": 2, "secret": "omit"}]}

    async def create_cart(self, **kwargs):
        self.calls.append(("create_cart", kwargs))
        return {
            "cart_id": "C1",
            "session_id": "S1",
            "cart_url": "https://loja.example/checkout/S1",
            "secret": "omit",
        }

    async def get_cart(self, session_id):
        self.calls.append(("get_cart", session_id))
        return {
            "cart_id": "C1",
            "session_id": session_id,
            "cart_url": "https://loja.example/checkout/S1",
            "secret": "omit",
        }

    async def get_cart_complete(self, session_id):
        self.calls.append(("get_cart_complete", session_id))
        return {
            "data": {
                "cart": {
                    "cart_id": "C1",
                    "session_id": session_id,
                    "total": "199.80",
                    "items": [{
                        "product_id": "641",
                        "variant_id": "123",
                        "quantity": 2,
                        "unit_price": "99.90",
                        "payment_methods": [{
                            "payment_method_id": "CARD",
                            "blocked": "0",
                            "max_plots": "6",
                        }],
                    }],
                }
            }
        }

    async def get_payment_options(self, cart_session_id):
        self.calls.append(("get_payment_options", cart_session_id))
        return {
            "payment_options": [
                {
                    "id": "PIX",
                    "name": "Pix",
                    "text": "Pagamento via Pix",
                    "card": 0,
                    "discount_value": "9.99",
                    "total_base": "189.81",
                    "plots": [{
                        "installments": 1,
                        "value": "189.81",
                        "interest": 0,
                        "order_total": "189.81",
                    }],
                },
                {
                    "id": "CARD",
                    "name": "Cartão",
                    "text": "Cartão de crédito",
                    "card": 1,
                    "plots": [{
                        "installments": 10,
                        "value": "19.98",
                        "interest": 0,
                        "order_total": "199.80",
                    }],
                },
            ]
        }


@pytest.mark.asyncio
async def test_search_products_reduces_payload_and_uses_name():
    client = FakeTray()
    result = await execute_tool("search_products", {"query": "Tissot Seastar", "limit": 5}, client)
    assert result == {"products": [{"id": "641", "name": "Tissot Seastar", "price": 6399.99}]}
    assert client.calls[0][1]["name"] == "Tissot Seastar"


@pytest.mark.asyncio
async def test_search_products_merges_query_with_explicit_brand():
    client = FakeTray()
    await execute_tool(
        "search_products",
        {"query": "C63 Sealander", "brand": "Christopher Ward", "limit": 5},
        client,
    )
    # Multi-word queries search by name; brand must stay attached.
    assert client.calls[0][1]["brand"] == "Christopher Ward"
    assert client.calls[0][1]["name"] == "C63 Sealander"


@pytest.mark.asyncio
async def test_search_products_compact_model_code_prefers_name():
    client = FakeTray()
    await execute_tool(
        "search_products",
        {"query": "PH2000M", "brand": "Certina", "limit": 5},
        client,
    )
    assert client.calls[0][1] == {
        "name": "PH2000M",
        "brand": "Certina",
        "limit": 5,
    }


@pytest.mark.asyncio
async def test_search_products_preserves_compact_paging_metadata():
    class PagedTray(FakeTray):
        async def search_products(self, **kwargs):
            self.calls.append(("search_products", kwargs))
            return {
                "products": [{"id": "1", "name": "Produto"}],
                "paging": {"total": 41, "page": 2, "limit": 20},
            }

    result = await execute_tool(
        "search_products",
        {"brand": "Marca", "limit": 20, "page": 2},
        PagedTray(),
    )

    assert result["paging"] == {"total": 41, "page": 2, "limit": 20}


@pytest.mark.asyncio
async def test_product_and_inventory_tools_call_expected_methods():
    client = FakeTray()
    assert (await execute_tool("get_product", {"product_id": "641"}, client))["current_price"] == 10
    inventory = await execute_tool("check_inventory", {"product_id": "641"}, client)
    assert inventory["stock"] == 0
    assert inventory["upon_request"] is True
    assert [call[0] for call in client.calls] == ["get_product", "get_product_stock"]


def test_tray_text_and_payment_options_are_normalized():
    result = _reduce(
        {
            "name": "Relógio &agrave; vista",
            "payment_option_details": [
                {
                    "id": "PIX",
                    "name": "Pix - Vindi",
                    "text": "Pagamento instantâneo",
                    "card": 0,
                    "total_base": "5439.99",
                    "plots": [{
                        "installments": 1,
                        "value": "5439.99",
                        "interest": 0,
                    }],
                },
                {
                    "id": "CARD",
                    "name": "Cartão",
                    "text": "Crédito",
                    "card": 1,
                    "plots": [{
                        "installments": 12,
                        "value": "533.33",
                        "interest": 0,
                    }],
                },
            ],
        },
        ("name", "payment_option_details"),
    )
    assert result["name"] == "Relógio à vista"
    options = result["payment_option_details"]
    assert options["pix"]["name"] == "Pix - Vindi"
    assert options["card"]["name"] == "Cartão"
    assert options["installments"] == [{
        "count": 12,
        "value": 533.33,
        "interest": False,
    }]
    assert "display_name" not in str(result)


@pytest.mark.asyncio
async def test_category_and_variant_tools_reduce_payloads():
    client = FakeTray()

    categories = await execute_tool("list_categories", {"limit": 50, "page": 1}, client)
    tree = await execute_tool("get_category_tree", {"category_id": "10"}, client)
    variants = await execute_tool("list_product_variants", {"product_id": "641"}, client)

    assert categories == {
        "categories": [{"id": 10, "name": "Relógios"}],
        "paging": {"total": 1, "page": 1, "limit": 50},
    }
    assert tree["tree"]["children"] == [{"id": 11, "name": "Masculinos"}]
    assert variants == {"variants": [{
        "id": "123",
        "product_id": "641",
        "color": "Preto",
        "stock": 2,
        "variant_id": "123",
    }]}


@pytest.mark.asyncio
async def test_category_http_422_is_preserved_as_invalid_request():
    class InvalidCategoryTray:
        async def list_categories(self, **kwargs):
            raise TrayAdapterError("tray_adapter_http_422", status_code=422)

    result = await execute_tool(
        "list_categories",
        {"limit": 50, "page": 1},
        InvalidCategoryTray(),
    )

    assert result["error_reason"] == "category_invalid_request"


@pytest.mark.asyncio
async def test_cart_adapter_diagnostics_are_preserved_as_structured_tool_facts():
    class FailingCartTray:
        async def create_cart(self, **_kwargs):
            raise TrayAdapterError(
                "tray_adapter_http_400",
                status_code=400,
                diagnostics={
                    "error": "tray_api_error",
                    "tray_error_code": "invalid_cart",
                    "tray_error_type": "validation_error",
                    "tray_error_field": "Cart.variant_id",
                    "tray_error_fields": ["Cart.variant_id"],
                    "tray_error_message": "campo invÃ¡lido",
                },
            )

    result = await execute_tool(
        "create_cart",
        {"product_id": "803", "variant_id": None, "quantity": 1, "price": "10.00"},
        FailingCartTray(),
    )

    assert result == {
        "error": "commerce_upstream_error",
        "status_code": 400,
        "error_type": "TrayAdapterError",
        "tray_error_code": "invalid_cart",
        "tray_error_type": "validation_error",
        "tray_error_field": "Cart.variant_id",
        "tray_error_fields": ["Cart.variant_id"],
        "tray_error_message": "campo invÃ¡lido",
    }


@pytest.mark.asyncio
async def test_cart_tools_use_normalized_adapter_contract():
    client = FakeTray()

    created = await execute_tool(
        "create_cart",
        {
            "product_id": "641",
            "variant_id": "123",
            "quantity": 2,
            "price": "99.90",
        },
        client,
    )
    loaded = await execute_tool("get_cart", {"session_id": "S1"}, client)

    assert created == {
        "cart_id": "C1",
        "session_id": "S1",
        "cart_url": "https://loja.example/checkout/S1",
    }
    assert loaded["session_id"] == "S1"
    assert client.calls[-2] == (
        "create_cart",
        {
            "product_id": "641",
            "variant_id": "123",
            "quantity": 2,
            "price": "99.90",
        },
    )


@pytest.mark.asyncio
async def test_wrapped_product_detail_preserves_commercial_price():
    class WrappedTray(FakeTray):
        async def get_product(self, product_id):
            return {
                "data": {
                    "product": {
                        "id": product_id,
                        "name": "Produto",
                        "current_price": "6199.99",
                        "available": True,
                    }
                }
            }

    product = await execute_tool(
        "get_product",
        {"product_id": "1025"},
        WrappedTray(),
    )

    assert product["id"] == "1025"
    assert product["current_price"] == "6199.99"


@pytest.mark.asyncio
async def test_product_link_accepts_direct_and_legacy_urls_preferring_https():
    class ProductLinkTray(FakeTray):
        def __init__(self):
            super().__init__()
            self.products = {
                "direct": {
                    "id": "direct",
                    "name": "Direto",
                    "url": "https://loja.example/direto",
                },
                "legacy": {
                    "id": "legacy",
                    "name": "Legado",
                    "url": {
                        "http": "http://loja.example/legado",
                        "https": "https://loja.example/legado",
                    },
                },
            }

        async def get_product(self, product_id):
            return self.products[product_id]

    client = ProductLinkTray()
    direct = await execute_tool(
        "get_product_link",
        {"product_id": "direct"},
        client,
    )
    legacy = await execute_tool(
        "get_product_link",
        {"product_id": "legacy"},
        client,
    )

    assert direct == {
        "product_id": "direct",
        "product_name": "Direto",
        "product_url": "https://loja.example/direto",
    }
    assert legacy["product_url"] == "https://loja.example/legado"


@pytest.mark.asyncio
async def test_complete_cart_and_payment_options_are_normalized():
    client = FakeTray()

    cart = await execute_tool(
        "get_cart_complete",
        {"session_id": "S1"},
        client,
    )
    payments = await execute_tool(
        "get_payment_options",
        {"cart_session_id": "S1"},
        client,
    )

    assert cart["total"] == "199.80"
    assert cart["items"] == [{
        "product_id": "641",
        "variant_id": "123",
        "quantity": 2,
        "unit_price": "99.90",
        "payment_methods": [{
            "payment_method_id": "CARD",
            "blocked": "0",
            "max_plots": "6",
        }],
    }]
    assert payments["payment_options"]["pix"]["total_base"] == 189.81
    assert payments["payment_options"]["installments"][0] == {
        "count": 10,
        "value": 19.98,
        "interest": False,
        "order_total": 199.8,
    }
    payment_schema = next(
        schema for schema in TOOL_SCHEMAS
        if schema["function"]["name"] == "get_payment_options"
    )
    assert payment_schema["function"]["parameters"]["oneOf"] == [
        {"required": ["cart_session_id"]}, {"required": ["order_id"]},
    ]


def test_cart_side_effect_is_not_exposed_as_an_openai_tool():
    exposed = {
        schema["function"]["name"]
        for schema in TOOL_SCHEMAS
    }

    assert "create_cart" not in exposed
    assert "create_order" not in exposed
    assert "quote_shipping" not in exposed
    assert "update_order_shipping" not in exposed
    assert "get_order_payment" not in exposed
    assert {"get_cart", "get_cart_complete", "get_payment_options"} <= exposed


@pytest.mark.asyncio
async def test_shipping_and_order_tools_normalize_internal_contracts():
    class CommerceAdapter:
        async def quote_shipping(self, **kwargs):
            assert kwargs["zipcode"] == "19900000"
            return {"success": True, "zipcode": "19900000", "options": [{
                "shipping_id": 1, "quotation_id": "Q1", "name": "PAC Tray",
                "price": "35.10", "min_period": 3, "max_period": 8,
                "secret": "omit",
            }]}

        async def create_order(self, payload):
            assert payload["session_id"] == "S1"
            return {"success": True, "order_id": 123, "status": "AGUARDANDO VINDI"}

        async def get_order_complete(self, order_id):
            assert order_id == "123"
            return {
                "order_id": 123, "status": "ENVIADO", "status_group": "shipped",
                "sending_code": "TRACK123", "tracking_url": "https://track.example/123",
            }

        async def get_order_payment(self, order_id):
            assert order_id == "123"
            return {
                "success": True,
                "order_id": 123,
                "payment": {
                    "method_id": "10545",
                    "method": "Pix - Vindi",
                    "type": "pix",
                    "has_payment": False,
                    "payment_date": None,
                    "payment_url": "https://pay.example/x?token=exact%2Bvalue",
                    "transaction_urls": ["https://internal.example/omit"],
                    "records": [{"access_code": "omit"}],
                    "payments_notification": {"notification": "omit"},
                },
            }

    adapter = CommerceAdapter()
    quote = await execute_tool("quote_shipping", {
        "zipcode": "19900000",
        "products": [{"product_id": "803", "price": "4699.99", "quantity": 1}],
    }, adapter)
    created = await execute_tool("create_order", {"session_id": "S1"}, adapter)
    status = await execute_tool("get_order_complete", {"order_id": "123"}, adapter)
    payment = await execute_tool("get_order_payment", {"order_id": "123"}, adapter)

    assert quote["options"] == [{
        "shipping_id": 1, "quotation_id": "Q1", "name": "PAC Tray",
        "price": "35.10", "min_period": 3, "max_period": 8,
    }]
    assert created["order_id"] == 123
    assert status["status"] == "ENVIADO"
    assert status["status_group"] == "shipped"
    assert status["sending_code"] == "TRACK123"
    assert payment == {
        "success": True,
        "order_id": 123,
        "payment": {
            "method_id": "10545",
            "method": "Pix - Vindi",
            "type": "pix",
            "has_payment": False,
            "payment_date": None,
            "payment_url": "https://pay.example/x?token=exact%2Bvalue",
        },
    }


@pytest.mark.asyncio
async def test_list_orders_accepts_orders_envelope_for_session_reconciliation():
    class OrdersAdapter:
        async def list_orders(self, **kwargs):
            assert kwargs == {"session_id": "SESSION-1"}
            return {
                "success": True,
                "orders": [{
                    "order_id": 321,
                    "status": "AGUARDANDO PAGAMENTO",
                    "status_group": "awaiting_payment",
                }],
            }

    result = await execute_tool(
        "list_orders", {"session_id": "SESSION-1"}, OrdersAdapter(),
    )

    assert result == {"orders": [{
        "order_id": 321,
        "status": "AGUARDANDO PAGAMENTO",
        "status_group": "awaiting_payment",
    }]}


@pytest.mark.asyncio
async def test_token_search_uses_adaptor_endpoint_when_available():
    class TokenTray:
        def __init__(self):
            self.calls = []

        async def search_products_by_tokens(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "products": [
                    {
                        "id": "rosa",
                        "name": (
                            "Relógio Christopher Ward C63 Sealander "
                            "Automático Rosa 36 mm"
                        ),
                        "brand": "Christopher Ward",
                    },
                    {
                        "id": "blue",
                        "name": "Relógio Christopher Ward C63 Sealander Automático Azul",
                        "brand": "Christopher Ward",
                    },
                ]
            }

        async def search_products(self, **kwargs):
            raise AssertionError("legacy search must not run when tokens work")

    client = TokenTray()
    result = await search_products(
        client,
        tokens=["sealander", "rosa", "automatico"],
        brand="Christopher Ward",
        limit=20,
    )

    assert [item["id"] for item in result["products"]] == ["rosa"]
    assert client.calls[0]["tokens"] == ["sealander", "rosa", "automatico"]


@pytest.mark.asyncio
async def test_token_search_polyfills_when_adaptor_route_missing():
    class LegacyTray:
        async def search_products_by_tokens(self, **_kwargs):
            raise TrayAdapterError("tray_adapter_http_404", status_code=404)

        async def search_products(self, **kwargs):
            name = (kwargs.get("name") or "").casefold()
            if "rosa" in name and kwargs.get("brand"):
                return {
                    "products": [
                        {
                            "id": "king",
                            "name": (
                                "Relógio Christopher Ward C63 Sealander "
                                "Automático Kingfisher"
                            ),
                            "brand": "Christopher Ward",
                        },
                        {
                            "id": "rosa",
                            "name": (
                                "Relógio Christopher Ward C63 Sealander "
                                "Automático Rosa C63-36ADA4-S00P0-B0 36 mm"
                            ),
                            "brand": "Christopher Ward",
                        },
                    ],
                    "paging": {"total": 2, "page": 1, "limit": 20},
                }
            return {"products": []}

    result = await search_products(
        LegacyTray(),
        tokens=["sealander", "rosa"],
        brand="Christopher Ward",
        limit=20,
    )

    assert [item["id"] for item in result["products"]] == ["rosa"]
