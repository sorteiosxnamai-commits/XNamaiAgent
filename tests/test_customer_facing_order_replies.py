import pytest

from app.commerce_context import CommerceConversationState
from app.order_service import get_order_facts
from app.payment_service import inspect_order_payment


@pytest.mark.asyncio
async def test_order_status_reply_is_customer_facing():
    async def execute(_name, _args):
        return {
            "success": True,
            "order_id": "25400",
            "status": "AGUARDANDO PAGAMENTO",
            "status_group": "open",
        }

    result = await get_order_facts(
        state=CommerceConversationState(order_id="25400"),
        execute=execute,
        order_id="25400",
    )
    assert "AGUARDANDO PAGAMENTO" in result.reply_text
    assert "consultado" not in result.reply_text.casefold()
    assert "factual" not in result.reply_text.casefold()


@pytest.mark.asyncio
async def test_payment_reply_includes_link_for_customer():
    payment_url = (
        "https://www.newstorerj.com.br/loja/pagamento.php"
        "?loja=687890&pedido=0CC131B51070AEF"
    )

    async def execute(_name, _args):
        return {
            "success": True,
            "order_id": "25400",
            "payment": {
                "has_payment": False,
                "payment_url": payment_url,
                "type": "pix",
            },
        }

    result = await inspect_order_payment(
        state=CommerceConversationState(order_id="25400"),
        execute=execute,
        order_id="25400",
    )
    assert payment_url in result.reply_text
    assert "factual" not in result.reply_text.casefold()
    assert "consultado" not in result.reply_text.casefold()
