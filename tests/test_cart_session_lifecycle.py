import pytest

from app.cart_service import CartItemRequest, create_cart_items_checkout
from app.commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    evolve_commerce_state,
)


def _item(product_id: str, quantity: int = 1) -> CartItemRequest:
    return CartItemRequest(
        CommerceProductReference(product_id=product_id, name=f"Produto {product_id}"),
        quantity=quantity,
    )


def _product(product_id: str) -> dict:
    return {
        "id": product_id,
        "name": f"Produto {product_id}",
        "current_price": "100.00",
        "available": True,
        "has_variation": False,
    }


@pytest.mark.asyncio
async def test_first_purchase_generates_hex_session_and_sends_it_to_cart():
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return _product(arguments["product_id"])
        if tool == "create_cart":
            session_id = arguments["session_id"]
            return {
                "cart_id": "C1",
                "session_id": session_id,
                "cart_url": f"https://loja.example/checkout/{session_id}",
            }
        if tool == "get_cart_complete":
            return {
                "items": [{"product_id": "A", "quantity": 1}],
                "total": "100.00",
            }
        raise AssertionError(tool)

    result = await create_cart_items_checkout(
        item_requests=[_item("A")],
        state=CommerceConversationState(),
        execute=execute,
    )

    post = next(arguments for tool, arguments in calls if tool == "create_cart")
    assert len(post["session_id"]) == 32
    int(post["session_id"], 16)
    assert result.response_metadata["cart_state"]["cart_session_id"] == post["session_id"]


@pytest.mark.asyncio
async def test_multi_item_reuses_one_generated_session():
    posts = []

    async def execute(tool, arguments):
        if tool == "get_product":
            return _product(arguments["product_id"])
        if tool == "create_cart":
            posts.append(dict(arguments))
            session_id = arguments["session_id"]
            return {
                "cart_id": "C1",
                "session_id": session_id,
                "cart_url": f"https://loja.example/checkout/{session_id}",
            }
        if tool == "get_cart_complete":
            return {
                "items": [
                    {"product_id": item["product_id"], "quantity": item["quantity"]}
                    for item in posts
                ],
                "total": "300.00",
            }
        raise AssertionError(tool)

    await create_cart_items_checkout(
        item_requests=[_item("A"), _item("B", quantity=2)],
        state=CommerceConversationState(),
        execute=execute,
    )

    assert len(posts) == 2
    assert posts[0]["session_id"] == posts[1]["session_id"]


@pytest.mark.asyncio
async def test_timeout_reconciles_with_get_without_second_post():
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return _product(arguments["product_id"])
        if tool == "create_cart":
            raise TimeoutError("upstream timeout")
        if tool == "get_cart_complete":
            session_id = arguments["session_id"]
            return {
                "cart_id": "C1",
                "session_id": session_id,
                "cart_url": f"https://loja.example/checkout/{session_id}",
                "items": [{"product_id": "A", "quantity": 1}],
                "total": "100.00",
            }
        raise AssertionError(tool)

    result = await create_cart_items_checkout(
        item_requests=[_item("A")],
        state=CommerceConversationState(),
        execute=execute,
    )

    assert [tool for tool, _ in calls].count("create_cart") == 1
    assert [tool for tool, _ in calls].count("get_cart_complete") == 1
    assert result.safety_reason is None


@pytest.mark.asyncio
async def test_failed_attempt_persists_session_and_next_message_reuses_it():
    first_posts = []

    async def failing_execute(tool, arguments):
        if tool == "get_product":
            return _product(arguments["product_id"])
        if tool == "create_cart":
            first_posts.append(dict(arguments))
            raise TimeoutError("upstream timeout")
        if tool == "get_cart_complete":
            return {"items": []}
        raise AssertionError(tool)

    previous = CommerceConversationState()
    failed = await create_cart_items_checkout(
        item_requests=[_item("A")],
        state=previous,
        execute=failing_execute,
    )
    persisted = evolve_commerce_state(previous, failed)
    generated_session = first_posts[0]["session_id"]

    assert failed.safety_reason == "cart_technical_failure"
    assert persisted.cart_session_id == generated_session

    second_posts = []

    async def successful_execute(tool, arguments):
        if tool == "get_product":
            return _product(arguments["product_id"])
        if tool == "create_cart":
            second_posts.append(dict(arguments))
            return {
                "session_id": arguments["session_id"],
                "cart_url": f"https://loja.example/checkout/{arguments['session_id']}",
            }
        if tool == "get_cart_complete":
            return {
                "cart_url": f"https://loja.example/checkout/{arguments['session_id']}",
                "items": [{"product_id": "A", "quantity": 1}],
                "total": "100.00",
            }
        raise AssertionError(tool)

    await create_cart_items_checkout(
        item_requests=[_item("A")],
        state=persisted,
        execute=successful_execute,
    )

    assert second_posts == []


@pytest.mark.asyncio
async def test_no_purchase_items_do_not_generate_cart_session():
    async def never_execute(tool, arguments):
        raise AssertionError((tool, arguments))

    result = await create_cart_items_checkout(
        item_requests=[],
        state=CommerceConversationState(),
        execute=never_execute,
    )

    assert "cart_state" not in result.response_metadata


@pytest.mark.asyncio
async def test_cart_technical_failure_does_not_blame_client_cache():
    async def execute(tool, arguments):
        if tool == "get_product":
            return _product(arguments["product_id"])
        if tool == "create_cart":
            return {"error": "bad gateway", "status_code": 502}
        if tool == "get_cart_complete":
            return {"items": []}
        raise AssertionError(tool)

    result = await create_cart_items_checkout(
        item_requests=[_item("A")],
        state=CommerceConversationState(),
        execute=execute,
    )

    reply = result.reply_text.casefold()
    assert result.safety_reason == "cart_technical_failure"
    assert "cache" not in reply
    assert "navegador" not in reply
    assert "internet" not in reply
