"""Unsent reviews confer no authority; delivered snapshots cannot move backwards."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.commerce_context import CommerceConversationState
from app.conversation_lifecycle import commit_delivered_conversation, finalize_dialogue, operation_snapshot
from app.memory_scope import stamp_state
from app.models import AgentResult

WORKSPACE = "aa774d20-509f-4d54-865b-7a5de22b6d30"


def reviewed(**changes):
    return CommerceConversationState(cart_session_id="cart-a",
        cart_items=[{"product_id": "a", "quantity": 2}],
        pending_action="awaiting_order_confirmation", pending_commerce_action="confirm_order",
        order_review_version="review-a", order_confirmation_status="pending", **changes)


@pytest.mark.parametrize("rejected", [None, "factual_validation_failed", "response_critique_regenerate_failed"])
@pytest.mark.asyncio
async def test_new_review_cannot_authorize_order_before_delivery(rejected):
    previous = CommerceConversationState(cart_session_id="cart-a")
    proposed = reviewed()
    if rejected:
        result = AgentResult(reply_text="Não consegui validar a resposta.", intent="commerce", safety_reason=rejected)
        snapshot = finalize_dialogue(result, proposed, previous)
    else:
        snapshot = operation_snapshot(proposed, previous)
    assert snapshot.pending_action is None and snapshot.pending_commerce_action is None
    assert snapshot.order_confirmation_status == "not_ready"
    assert snapshot.order_review_version is None and snapshot.confirmed_order_review_version is None
    from app.order_service import create_order
    execute = AsyncMock(side_effect=AssertionError("Undelivered review cannot create or query an order"))
    result = await create_order(state=snapshot, execute=execute)
    assert result.safety_reason == "order_confirmation_required"
    execute.assert_not_awaited()


def test_valid_final_review_is_retained_for_delivery_but_not_operational_snapshot():
    previous = CommerceConversationState(cart_session_id="cart-a")
    proposed = reviewed()
    result = AgentResult(reply_text="Confira os itens. Confirma o pedido?", intent="commerce")
    final = finalize_dialogue(result, proposed, previous)
    assert final.order_review_version == "review-a"
    assert final.order_confirmation_status == "pending"
    assert final.pending_action == "awaiting_order_confirmation"
    assert operation_snapshot(proposed, previous).order_review_version is None


@pytest.mark.parametrize("changes", [
    {"order_review_version": "review-b"},
    {"cart_items": [{"product_id": "a", "quantity": 3}]},
    {"mercos_payment_condition_id": "new-condition"},
])
def test_changed_order_invalidates_even_previously_delivered_review(changes):
    previous = reviewed()
    proposed = CommerceConversationState.from_payload({**previous.model_dump(mode="json"), **changes})
    snapshot = operation_snapshot(proposed, previous)
    assert snapshot.pending_action is None and snapshot.pending_commerce_action is None
    assert snapshot.order_confirmation_status == "not_ready"
    assert snapshot.order_review_version is None


def test_user_confirmation_of_unchanged_delivered_review_remains_an_authorized_fact():
    previous = reviewed()
    proposed = previous.model_copy(update={"order_confirmation_status": "confirmed",
        "confirmed_order_review_version": "review-a", "pending_action": None,
        "pending_commerce_action": None})
    snapshot = operation_snapshot(proposed, previous)
    assert snapshot.order_confirmation_status == "confirmed"
    assert snapshot.confirmed_order_review_version == snapshot.order_review_version == "review-a"


@pytest.mark.parametrize("operational", [
    {"order_id": "created-123", "order_payment_url": "https://example.com/payment/123"},
    {"order_creation_ambiguous": True},
])
@pytest.mark.asyncio
async def test_created_or_ambiguous_order_survives_failed_reply_without_rearming_review(operational):
    previous = reviewed()
    proposed = previous.model_copy(update=operational)
    snapshot = operation_snapshot(proposed, previous)
    assert snapshot.pending_action is None and snapshot.pending_commerce_action is None
    for key, value in operational.items():
        assert getattr(snapshot, key) == value
    if snapshot.order_id:
        from app.order_service import create_order
        execute = AsyncMock(side_effect=AssertionError("Already created order must not be repeated"))
        result = await create_order(state=snapshot, execute=execute)
        assert result.commercial_data["existing"]
        execute.assert_not_awaited()


@pytest.mark.parametrize("old_status", ["collecting", "review"])
@pytest.mark.asyncio
async def test_unsent_registration_review_keeps_submitted_data_without_creation_authority(old_status):
    from app.customer_registration import handle_customer_registration_turn
    draft = {"document": "52998224725", "legal_name": "Pessoa Teste",
             "email": "teste@example.com", "phone": "11988880000", "person_type": "F"}
    previous = CommerceConversationState(
        pending_action=("awaiting_customer_registration_confirmation" if old_status == "review"
                        else "awaiting_customer_registration_data"),
        customer_registration={"status": old_status, "draft": {**draft, "legal_name": "Pessoa Anterior"}})
    proposed = previous.model_copy(update={"pending_action": "awaiting_customer_registration_confirmation",
        "customer_registration": {"status": "review", "draft": draft}})
    snapshot = operation_snapshot(proposed, previous)
    assert snapshot.customer_registration["draft"] == draft
    assert snapshot.customer_registration["status"] == "collecting"
    assert snapshot.pending_action == "awaiting_customer_registration_data"
    execute = AsyncMock(side_effect=AssertionError("Unseen registration data cannot be confirmed"))
    result = await handle_customer_registration_turn("confirmo", state=snapshot, execute=execute,
        registration_enabled=True, commit_enabled=True)
    assert result.response_metadata["pending_action"] == "awaiting_customer_registration_confirmation"
    execute.assert_not_awaited()


@pytest.mark.parametrize("status", ["created", "created_pending_sync", "unknown", "cancelled", "handoff"])
def test_registration_terminal_or_uncertain_result_survives_delivery_failure(status):
    previous = CommerceConversationState(pending_action="awaiting_customer_registration_confirmation",
        customer_registration={"status": "review", "draft": {"document": "52998224725"}})
    proposed = previous.model_copy(update={"pending_action": None,
        "mercos_customer_id": "customer-a" if status == "created" else None,
        "customer_registration": {"status": status, "draft": {}}})
    snapshot = operation_snapshot(proposed, previous)
    assert snapshot.pending_action is None
    assert snapshot.customer_registration["status"] == status
    assert snapshot.mercos_customer_id == proposed.mercos_customer_id


@pytest.mark.parametrize("pending", ["send_product_link", "create_cart", "show_images", "show_payment_options"])
def test_undelivered_offer_cannot_be_confirmed(pending):
    previous = CommerceConversationState()
    proposed = previous.model_copy(update={"pending_action": pending, "pending_action_product_ids": ["a"]})
    snapshot = operation_snapshot(proposed, previous)
    assert snapshot.pending_action is None and snapshot.pending_action_product_ids == []


def receipt(inbound_id, topic):
    return {"provider_send_ok": True, "workspace_id": WORKSPACE, "inbound_id": inbound_id,
        "channel": "whatsapp", "sender_key": "test-person", "reply_text": "Resposta entregue.",
        "response_metadata": {"conversation_commit": {"version": 1, "conversation_id": "conversation-a"},
            "commerce_state": stamp_state({"active_topic": topic}, WORKSPACE)}}


def test_delayed_success_receipt_cannot_restore_older_dialogue_or_summary(monkeypatch):
    from app import db
    settings = SimpleNamespace(chatbo_workspace_id=WORKSPACE, database_url="configured")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    newest_inbound = 20
    commands = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, sql, params):
            commands.append((sql, params))
            self.params = params
        def fetchone(self):
            return {"newer": 1} if self.params["inbound_id"] < newest_inbound else None

    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=Cursor)

    monkeypatch.setattr(db, "get_conn", connection)
    persist, summary = Mock(), Mock()
    monkeypatch.setattr("app.conversation_lifecycle.persist_state", persist)
    monkeypatch.setattr("app.conversation_memory.update_conversation_memory", summary)
    commit_delivered_conversation(receipt(20, "current_topic"), 200)
    commit_delivered_conversation(receipt(10, "old_topic"), 201)
    persist.assert_called_once()
    assert persist.call_args.args[1].active_topic == "current_topic"
    summary.assert_called_once()
    assert summary.call_args.args[1].response_metadata["response_id"] == 200
    assert all(commands[i][0].startswith("SELECT pg_advisory_xact_lock") for i in (0, 2))
    assert all(commands[i][1]["workspace"] == WORKSPACE for i in (1, 3))
    assert "response.inbound_id >" in commands[1][0]
    assert "response.provider_send_ok = true" in commands[1][0]


@pytest.mark.parametrize("identity", [
    {"sender_phone": "5511999990000", "sender_key": None, "conversation_id": None},
    {"sender_phone": None, "sender_key": "test-person", "conversation_id": None},
    {"sender_phone": None, "sender_key": None, "conversation_id": "conversation-a"},
])
@pytest.mark.parametrize("newer_scope", ["same", "other_workspace", "other_identity", "none"])
def test_newer_inbound_without_reply_blocks_stale_confirmation(monkeypatch, identity, newer_scope):
    """The customer's cancellation need not have a delivered reply yet."""
    from app import db
    from app.conversation_lifecycle import _ordered_delivery
    from app.models import IncomingMessage
    settings = SimpleNamespace(database_url="configured")
    incoming = IncomingMessage(workspace_id=WORKSPACE, raw={"inbound_id": 10}, **identity)
    newer = {"id": 11, "workspace": WORKSPACE, **identity}
    if newer_scope == "other_workspace":
        newer["workspace"] = "bb774d20-509f-4d54-865b-7a5de22b6d30"
    if newer_scope == "other_identity":
        newer.update({key: "other" for key in identity})
    entered = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, sql, params):
            self.sql, self.params = sql, params
        def fetchone(self):
            # There are no successful response rows; only the newer inbound
            # can reject this receipt. Assert every nullable bind is typed.
            assert "newer.id > %(inbound_id)s" in self.sql
            assert "newer.workspace_id = %(workspace)s::uuid" in self.sql
            for key in identity:
                assert f"%({key})s::text IS NOT NULL" in self.sql
            return {"newer": 1} if (newer_scope != "none"
                and newer["workspace"] == self.params["workspace"]
                and any(self.params[key] and newer[key] == self.params[key] for key in identity)) else None

    @contextmanager
    def connection():
        entered.append(True)
        try:
            yield SimpleNamespace(cursor=Cursor)
        finally:
            entered.pop()

    monkeypatch.setattr(db, "get_conn", connection)
    with _ordered_delivery(incoming, settings, 100) as current:
        assert entered  # the transaction still holds its advisory lock
        assert current is (newer_scope != "same")
    assert not entered


@pytest.mark.parametrize("inbound_id,identity", [(None, "known"), (10, None)])
def test_live_delivery_without_ordering_identity_does_not_commit(monkeypatch, inbound_id, identity):
    from app.conversation_lifecycle import _ordered_delivery
    from app.models import IncomingMessage
    query = Mock(side_effect=AssertionError("An unordered receipt cannot query or commit state"))
    monkeypatch.setattr("app.db.get_conn", query)
    incoming = IncomingMessage(workspace_id=WORKSPACE, sender_key=identity, raw={"inbound_id": inbound_id})
    with _ordered_delivery(incoming, SimpleNamespace(database_url="configured"), 100) as current:
        assert current is False
    query.assert_not_called()
