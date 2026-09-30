from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
    assert set(item) == {"mercos_id", "order_number", "status_code", "billing_code", "excluded", "contents"}
    assert item["contents"] is None  # invalid/private item data never becomes verified contents
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


@pytest.mark.asyncio
async def test_legacy_zero_number_does_not_block_historical_pagination():
    db = TransactionFixture()
    client = NS(list_resource=AsyncMock(return_value=AdaptorPage(
        "orders", 2, [row(numero=0), row(id=2000002, numero=95694)], "next", "next")))
    result = await OrderStatusIndex(tenant_id="one", connect=db.connect).sync(client, max_pages=1, historical=True)
    assert result["ok"] and result["records"] == 2 and db.history_cursor == "next"
    assert db.rows[0][2] == "0" and db.rows[0][1] == "1000001"


class TransactionFixture:
    """Observe commits/rollbacks and SQL data, without an external database."""
    def __init__(self):
        self.cursor = "start"
        self.rows = []
        self.completed = False
        self.history_cursor = None
        self.history_completed_at = None
        self.verified = {}

    @contextmanager
    def connect(self):
        before = deepcopy(self.__dict__)
        try:
            yield self
        except Exception:
            self.__dict__.update(before)
            raise

    def execute(self, sql, args=()):
        if "SELECT cursor_value" in sql:
            return NS(fetchone=lambda: {"cursor_value": self.cursor, "history_cursor": self.history_cursor,
                                       "history_completed_at": self.history_completed_at})
        if "SELECT o.mercos_id" in sql:
            return NS(fetchall=lambda: [dict(zip(
                ("tenant_id", "mercos_id", "order_number", "status_code", "billing_code", "excluded", "contents"),
                (*item[:6], getattr(item[6], "obj", item[6]) if len(item) > 6 else None)),
                verified_at=self.verified.get(item[1]),
                completed_at=datetime.now(timezone.utc) if self.completed else None)
                for item in self.rows if item[0] == args[0] and args[1] in (item[1], item[2])][:2])
        if "INSERT INTO public.ai_mercos_order_status" in sql:
            existing = next((item for item in self.rows if item[:2] == args[:2]), None)
            if existing and "WHERE ai_mercos_order_status.contents IS NULL" in sql:
                if len(existing) == 6 or existing[6] is None:
                    self.rows[self.rows.index(existing)] = (*existing[:6], args[6])
                return NS()
            if existing and "DO NOTHING" in sql:
                return NS()
            if existing:
                self.rows.remove(existing)
            self.rows.append(args)
            self.verified[args[1]] = datetime.now(timezone.utc)
        if "SET cursor_value" in sql:
            self.cursor, self.completed = args[0], False
        if "SET completed_at=clock_timestamp()" in sql:
            self.completed = True
        if "SET history_cursor" in sql:
            self.history_cursor, self.history_completed_at = args[0], None
        if "SET history_completed_at=clock_timestamp()" in sql:
            self.history_completed_at = datetime.now(timezone.utc)
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
                return NS(fetchall=lambda: [{**normalize_status(row()), "verified_at": datetime.now(timezone.utc)}])
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


@pytest.mark.asyncio
async def test_matching_order_is_usable_before_unrelated_pages_finish():
    db = TransactionFixture()
    client = NS(list_resource=AsyncMock(side_effect=[
        AdaptorPage("orders", 1, [row()], "next", "next"),
        AdaptorPage("orders", 1, [row(id=2000002, numero=95934)], "later", "later"),
    ]))
    result = await OrderStatusIndex(tenant_id="one", connect=db.connect).lookup("95933", client)
    assert result["ok"] and result["order_number"] == "95933"
    assert not db.completed and client.list_resource.await_count == 2


@pytest.mark.asyncio
async def test_expired_order_is_not_returned_after_sync_failure():
    db = TransactionFixture()
    db.rows.append(("one", "1000001", "95933", "2", "0", False))
    db.verified["1000001"] = datetime.now(timezone.utc) - timedelta(hours=1)
    client = NS(list_resource=AsyncMock(side_effect=TimeoutError))
    result = await OrderStatusIndex(tenant_id="one", connect=db.connect).lookup("95933", client)
    assert result["error"] == "order_index_not_ready"
    assert result["code"] == "order_sync_timeout"


@pytest.mark.asyncio
async def test_history_resumes_without_replacing_current_status_or_cursor():
    db = TransactionFixture()
    index = OrderStatusIndex(tenant_id="one", connect=db.connect)
    client = NS(list_resource=AsyncMock(side_effect=[
        AdaptorPage("orders", 1, [row(status=0)], "live", None),
        AdaptorPage("orders", 2, [row(status=1), row(id=2000002, numero=100)], "old", "old"),
        AdaptorPage("orders", 0, [], "end", None),
    ]))
    await index.sync(client, max_pages=1)
    partial = await index.sync(client, max_pages=1, historical=True)
    assert partial["complete"] is False
    assert db.cursor == "live" and db.completed
    assert db.history_cursor == "old"
    assert db.rows[0][3] == "0"  # historical budget never resurrects cancelled order
    assert (await index.lookup("100", client))["ok"]
    assert (await index.sync(client, historical=True))["complete"]
    assert (await index.sync(client, historical=True))["pages"] == 0
    assert [call.kwargs["changed_after"] for call in client.list_resource.await_args_list] == ["start", None, "old"]


@pytest.mark.asyncio
async def test_background_prioritizes_pending_incremental_then_loads_history():
    db = TransactionFixture()
    index = OrderStatusIndex(tenant_id="one", connect=db.connect)
    client = NS(list_resource=AsyncMock(side_effect=[
        AdaptorPage("orders", 1, [row()], "a", "a"),
        AdaptorPage("orders", 1, [row()], "b", "b"),
        AdaptorPage("orders", 0, [], "c", None),
        AdaptorPage("orders", 1, [row(id=2000002, numero=100)], "h", "h"),
    ]))
    first = await index.background_sync(client)
    assert first["ok"] and first["history"] is None
    second = await index.background_sync(client)
    assert second["ok"] and second["incremental"]["complete"]
    assert not second["history"]["complete"] and db.history_cursor == "h"


@pytest.mark.asyncio
async def test_history_failure_keeps_successfully_refreshed_orders_available():
    db = TransactionFixture()
    index = OrderStatusIndex(tenant_id="one", connect=db.connect)
    client = NS(list_resource=AsyncMock(side_effect=[
        AdaptorPage("orders", 1, [row()], "live", None), TimeoutError,
    ]))
    result = await index.background_sync(client)
    assert not result["ok"] and result["incremental"]["complete"]
    assert result["history"]["error"] == "order_sync_timeout"
    assert db.completed and db.cursor == "live" and db.history_cursor is None
    assert (await index.lookup("95933", client))["ok"]
