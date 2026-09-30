from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.commerce.mercos.client import AdaptorPage
from app.commerce.mercos.order_status import OrderStatusIndex, normalize_status, status_result
from app.commerce_context import CommerceConversationState
from app.order_service import get_order_facts


def row(**overrides):
    return {"id": 1000001, "numero": 95933, "status": 2, "status_faturamento": 0, **overrides}


def test_number_is_not_confused_with_internal_id_and_private_data_is_discarded():
    item = normalize_status(row(cliente_cnpj="private", itens=[{"private": True}], total=900))
    assert set(item) == {"mercos_id", "order_number", "status_code", "billing_code", "excluded"}
    result = status_result([item], "95933")
    assert result["order_id"] == "1000001"
    assert result["status"] == "Pedido gerado — não faturado"
    assert result["payment_supported"] is False


def test_id_number_collision_fails_closed():
    rows = [normalize_status(row()), normalize_status(row(id=95933, numero=41))]
    assert status_result(rows, "95933")["code"] == "ambiguous_reference"


@pytest.mark.parametrize("changes", [{"status": 9}, {"excluido": True}])
def test_unknown_status_or_excluded_order_is_not_a_business_fact(changes):
    assert "error" in status_result([normalize_status(row(**changes))], "95933")


def test_absence_in_recent_window_does_not_claim_order_does_not_exist():
    result = status_result([], "123")
    assert result["error"] == "order_reference_unconfirmed"
    assert result.get("status_code") != 404


class TransactionFixture:
    """Observe commits/rollbacks and SQL data, without an external database."""
    def __init__(self):
        self.cursor = "start"
        self.rows = []
        self.completed = False

    @contextmanager
    def connect(self):
        before = deepcopy((self.cursor, self.rows, self.completed))
        try:
            yield self
        except Exception:
            self.cursor, self.rows, self.completed = before
            raise

    def execute(self, sql, args=()):
        if "SELECT cursor_value" in sql:
            return NS(fetchone=lambda: {"cursor_value": self.cursor})
        if "INSERT INTO public.ai_mercos_order_status" in sql:
            self.rows.append(args)
        if "SET cursor_value" in sql:
            self.cursor, self.completed = args[0], False
        if "SET completed_at=now()" in sql:
            self.completed = True
        return NS()


@pytest.mark.asyncio
async def test_partial_sync_commits_cursor_but_never_marks_ready():
    db = TransactionFixture()
    client = NS(list_resource=AsyncMock(return_value=AdaptorPage("orders", 1, [row()], "next", "next")))
    result = await OrderStatusIndex(tenant_id="one", connect=db.connect).sync(client, max_pages=1)
    assert result["complete"] is False
    assert db.cursor == "next" and not db.completed
    assert db.rows[0][0] == "one"
    client.list_resource.assert_awaited_once_with("orders", changed_after="start")


@pytest.mark.asyncio
async def test_invalid_next_page_rolls_back_rows_and_cursor():
    db = TransactionFixture()
    client = NS(list_resource=AsyncMock(side_effect=[AdaptorPage("orders", 1, [row()], "next", "next"),
        AdaptorPage("orders", 1, [{"id": 8}], "end", None)]))
    result = await OrderStatusIndex(tenant_id="one", connect=db.connect).sync(client)
    assert not result["ok"]
    assert db.cursor == "start" and db.rows == [] and not db.completed


@pytest.mark.asyncio
async def test_full_sync_marks_ready_only_at_end():
    db = TransactionFixture()
    client = NS(list_resource=AsyncMock(return_value=AdaptorPage("orders", 1, [row()], "end", None)))
    result = await OrderStatusIndex(tenant_id="one", connect=db.connect).sync(client)
    assert result["complete"] and db.completed and db.cursor == "end"


@pytest.mark.asyncio
async def test_order_response_does_not_turn_billing_into_payment_link():
    state = CommerceConversationState(order_payment_status="pending", order_payment_url="https://example.com/old")
    execute = AsyncMock(return_value=status_result([normalize_status(row())], "95933"))
    result = await get_order_facts(state=state, execute=execute, order_id="95933")
    assert "não faturado" in result.reply_text
    assert "pagamento" not in result.reply_text
    assert "example.com" not in result.reply_text
    assert result.response_metadata["purchase_stage"] == "order_created"
    assert result.response_metadata["payment_state"]["order_payment_url"] is None


@pytest.mark.asyncio
async def test_unconfirmed_order_preserves_number_for_retry():
    result = await get_order_facts(state=CommerceConversationState(), order_id="95933",
        execute=AsyncMock(return_value={"error": "order_index_not_ready"}))
    assert result.response_metadata["order_state"]["order_lookup_id"] == "95933"
    assert "não significa" in result.reply_text


@pytest.mark.asyncio
async def test_provider_dispatch_reaches_real_status_mapping(monkeypatch):
    from app.commerce.mercos import order_status
    from app.commerce.mercos.provider import MercosCommerceProvider
    index = OrderStatusIndex(tenant_id="one")
    monkeypatch.setattr(index, "ready", lambda: True)

    @contextmanager
    def connect():
        class Connection:
            def execute(self, sql, args):
                assert args == ("one", "95933", "95933")
                return NS(fetchall=lambda: [normalize_status(row())])
        yield Connection()

    index.connect = connect
    monkeypatch.setattr(order_status, "OrderStatusIndex", lambda **kwargs: index)
    client = NS(list_resource=AsyncMock())
    provider = MercosCommerceProvider(client, tenant_id="one")
    result = await get_order_facts(state=CommerceConversationState(), order_id="95933", execute=provider.execute)
    assert result.safety_reason is None
    assert result.commercial_data["order_id"] == "1000001"
    assert "Pedido gerado" in result.reply_text
    client.list_resource.assert_not_awaited()

    # Replay the user's exact two turns through the inbound pipeline and the
    # real provider dispatch, not a mocked get_order_facts success.
    from tests.test_chatbo_pipeline_replay import Replay
    from app import openai_agent
    replay = Replay(monkeypatch)
    monkeypatch.setattr(openai_agent, "execute_tool", provider.execute)
    await replay.say("fiz um pedido no carrinho")
    _, reply = await replay.say("95933")
    assert "Pedido gerado" in reply["reply"]
    assert "não faturado" in reply["reply"]
    assert "produto" not in reply["reply"].casefold()
