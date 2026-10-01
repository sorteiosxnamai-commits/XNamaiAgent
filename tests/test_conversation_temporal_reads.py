"""A customer answer cannot authorize a question delivered afterwards."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import db
from app.commerce_context import CommerceConversationState
from app.memory_scope import stamp_state

WORKSPACE = "aa774d20-509f-4d54-865b-7a5de22b6d30"
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
REVIEW = {"cart_session_id": "cart-a", "pending_action": "awaiting_order_confirmation",
    "pending_commerce_action": "confirm_order", "order_confirmation_status": "pending",
    "order_review_version": "review-a", "pending_followup": {"question": "Confirma o pedido?"}}


class TemporalDB:
    """Replay arrival/delivery order without network or production credentials."""
    def __init__(self, monkeypatch, *, delivery_offset=1, session=None, session_offset=None, current_exists=True):
        self.delivery_at = NOW + timedelta(seconds=delivery_offset)
        self.session_at = NOW + timedelta(seconds=delivery_offset if session_offset is None else session_offset)
        self.session = session
        self.current_exists = current_exists
        self.calls = []
        monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(
            database_url="configured", chatbo_workspace_id=WORKSPACE, auto_create_tables=False))
        monkeypatch.setattr(db, "get_conn", self.connection)
        monkeypatch.setattr("app.customer_identity.resolve_linked_identity_candidates", lambda **_: [])
        monkeypatch.setattr("app.customer_identity.resolve_person_key_candidates", lambda **_: ["sender:test"])
        monkeypatch.setattr(db, "_history_identity_candidates", lambda *_: [("conversation-a", None, None)])

    @contextmanager
    def connection(self):
        yield SimpleNamespace(cursor=lambda: self)

    def __enter__(self): return self
    def __exit__(self, *_): pass
    def execute(self, sql, params):
        self.sql, self.params = sql, params
        self.calls.append((sql, params))

    def fetchall(self):
        if "FROM public.ai_customer_commerce_sessions" in self.sql:
            if not self.session:
                return []
            assert "updated_at <" in self.sql and "current_inbound.workspace_id" in self.sql
            predates = self.current_exists and self.session_at < NOW
            return [{"commerce_state": stamp_state(self.session, WORKSPACE), "predates_inbound": predates}]
        if "FROM public.ai_inbound_messages AS inbound" not in self.sql and "JOIN public.ai_inbound_messages AS inbound" not in self.sql:
            raise AssertionError("Unexpected database query")
        bounded = self.params["before_inbound_id"] is not None
        delivered_before = not bounded or (self.current_exists and self.delivery_at < NOW)
        if bounded:
            assert "response.created_at <" in self.sql
            assert "current_inbound.workspace_id = %(workspace_id)s::uuid" in self.sql
        else:
            assert "response.created_at <" not in self.sql
        response = {"_agent_context": {"commerce_state": stamp_state(REVIEW, WORKSPACE)}}
        if "AS delivered ON true" in self.sql:
            return [{"id": 10, "text": "quero finalizar", "reply_text": "Confirma o pedido?" if delivered_before else None,
                "provider_response": response if delivered_before else None}]
        return [{"provider_response": response}] if delivered_before else []


@pytest.mark.parametrize("offset,current_exists,seen", [(-1, True, True), (0, True, False), (1, True, False), (-1, False, False)])
def test_history_never_attributes_late_review_to_earlier_yes(monkeypatch, offset, current_exists, seen):
    TemporalDB(monkeypatch, delivery_offset=offset, current_exists=current_exists)
    turns = db.load_recent_conversation_turns(conversation_id="conversation-a", sender_phone=None, before_inbound_id=11)
    assert turns[0] == {"role": "user", "content": "quero finalizar"}
    assert any(turn["role"] == "assistant" for turn in turns) is seen


@pytest.mark.parametrize("offset,current_exists,seen", [(-1, True, True), (0, True, False), (1, True, False), (-1, False, False)])
def test_commerce_state_never_authorizes_late_review(monkeypatch, offset, current_exists, seen):
    TemporalDB(monkeypatch, delivery_offset=offset, current_exists=current_exists)
    state = db.load_commerce_conversation_state(conversation_id="conversation-a", sender_phone=None, before_inbound_id=11)
    assert (state.get("pending_action") == "awaiting_order_confirmation") is seen
    assert (state.get("order_review_version") == "review-a") is seen


def test_unbounded_history_still_includes_successful_delivery(monkeypatch):
    TemporalDB(monkeypatch, delivery_offset=10)
    turns = db.load_recent_conversation_turns(conversation_id="conversation-a", sender_phone=None, before_inbound_id=None)
    assert turns[-1]["content"] == "Confirma o pedido?"


@pytest.mark.parametrize("facts", [
    {"order_id": "created-123", "order_payment_url": "https://example.com/payment/123"},
    {"order_creation_ambiguous": True},
    {"mercos_customer_id": "customer-a", "customer_registration": {"status": "created", "draft": {}}},
])
def test_later_session_preserves_completed_operations_but_not_confirmation(monkeypatch, facts):
    TemporalDB(monkeypatch, session={**REVIEW, **facts})
    state = db.load_commerce_conversation_state(conversation_id="conversation-a", sender_phone=None, before_inbound_id=11)
    assert state["pending_action"] is None and state["pending_commerce_action"] is None
    assert state["order_review_version"] is None
    assert state["pending_followup"] is None
    for key, value in facts.items():
        assert state[key] == value


def test_later_registration_review_keeps_draft_without_authority(monkeypatch):
    draft = {"document": "52998224725", "legal_name": "Pessoa Teste"}
    TemporalDB(monkeypatch, session={"pending_action": "awaiting_customer_registration_confirmation",
        "customer_registration": {"status": "review", "draft": draft}})
    state = db.load_customer_commerce_sessions(["sender:test"], before_inbound_id=11)[0]
    assert state["customer_registration"] == {"status": "collecting", "draft": draft}
    assert state["pending_action"] is None


def test_prior_session_retains_legitimate_delivered_review(monkeypatch):
    TemporalDB(monkeypatch, session=REVIEW, delivery_offset=-1)
    assert db.load_customer_commerce_sessions(["sender:test"], before_inbound_id=11) == [REVIEW]


def test_writer_session_reads_have_no_temporal_cutoff(monkeypatch):
    TemporalDB(monkeypatch, session=REVIEW)
    assert db.load_customer_commerce_sessions(["sender:test"]) == [REVIEW]


def test_cancelled_review_is_not_resurrected_when_cancel_reply_failed(monkeypatch):
    cancelled = {**REVIEW, "pending_action": None, "pending_commerce_action": None,
        "order_confirmation_status": "not_ready", "order_review_version": None,
        "confirmed_order_review_version": None, "pending_followup": None}
    TemporalDB(monkeypatch, session=cancelled, delivery_offset=-2, session_offset=-1)
    payload = db.load_commerce_conversation_state(conversation_id="conversation-a", sender_phone=None, before_inbound_id=11)
    assert payload["cart_session_id"] == "cart-a"
    assert payload["pending_action"] is None and payload["pending_commerce_action"] is None
    assert payload["order_confirmation_status"] == "not_ready"
    assert payload["order_review_version"] is None and payload["confirmed_order_review_version"] is None


def test_later_draft_without_authority_cannot_inherit_an_older_review(monkeypatch):
    TemporalDB(monkeypatch, session=REVIEW, delivery_offset=-2, session_offset=1)
    payload = db.load_commerce_conversation_state(conversation_id="conversation-a", sender_phone=None, before_inbound_id=11)
    assert payload["order_review_version"] is None
    assert payload["pending_action"] is None


@pytest.mark.asyncio
async def test_replay_after_order_was_created_cannot_create_it_twice(monkeypatch):
    from unittest.mock import AsyncMock
    from app.order_service import create_order
    TemporalDB(monkeypatch, session={**REVIEW, "order_id": "created-123"})
    payload = db.load_commerce_conversation_state(conversation_id="conversation-a", sender_phone=None, before_inbound_id=11)
    execute = AsyncMock(side_effect=AssertionError("Completed order must not be retried"))
    result = await create_order(state=CommerceConversationState.from_payload(payload), execute=execute)
    assert result.commercial_data["existing"]
    execute.assert_not_awaited()
