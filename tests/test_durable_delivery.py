from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.ingress import outbox, worker
from app.models import AgentResult, IncomingMessage


def envelope():
    incoming = IncomingMessage(provider="ycloud", channel="whatsapp", sender_phone="5511999999999",
                               conversation_id="wa:customer", channel_metadata={"ycloud_to": "5511888888888"})
    result = AgentResult(reply_text="Seu pedido está em revisão.", reply_audio_bytes=b"\xff\x00",
                         response_metadata={"commerce_turn_state": {"pending_commerce_action": "confirm"},
                                            "outbound_image_url": "https://cdn.example/photo.jpg"})
    return incoming, result


def test_retry_envelope_keeps_recipient_media_and_state_without_binary():
    incoming, result = envelope()
    saved = outbox.build_outbound_envelope(incoming, result)
    row = {"reply_text": result.reply_text, "reply_payload": saved}
    assert outbox.incoming_from_outbox_row(row).sender_phone == incoming.sender_phone
    restored = outbox.result_from_outbox_row(row)
    assert restored.response_metadata == result.response_metadata
    assert restored.reply_text == result.reply_text
    assert "reply_audio_bytes" not in saved["result"]


@pytest.mark.asyncio
async def test_inline_dispatch_uses_saved_response_not_regenerated_content(monkeypatch):
    incoming, result = envelope()
    saved = {"id": 4, "lease_owner": "owner-1", "attempts": 1, "max_attempts": 8,
             "reply_text": result.reply_text, "reply_payload": outbox.build_outbound_envelope(incoming, result)}
    monkeypatch.setattr(outbox, "claim_outbox_for_send", lambda _: saved)
    mark = Mock()
    monkeypatch.setattr(outbox, "mark_outbox_sent", mark)
    send = AsyncMock(return_value={"ok": True})
    await outbox.dispatch_accepted_outbound(4, send)
    assert send.await_args.args[0].provider == "ycloud"
    assert send.await_args.args[1].reply_text == result.reply_text
    assert mark.call_args.kwargs["owner"] == "owner-1"


@pytest.mark.asyncio
async def test_other_worker_owns_delivery_no_second_send(monkeypatch):
    monkeypatch.setattr(outbox, "claim_outbox_for_send", lambda _: None)
    monkeypatch.setattr(outbox, "get_outbox_status", lambda _: "leased")
    send = AsyncMock()
    result = await outbox.dispatch_accepted_outbound(4, send)
    assert result["queued"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_delivery_drops_reply_when_customer_already_sent_another_message(monkeypatch):
    from app.ingress import outbox_worker
    incoming, result = envelope()
    saved = {"id": 4, "inbox_id": 3, "lease_owner": "owner-1", "attempts": 1,
             "reply_text": result.reply_text, "reply_payload": outbox.build_outbound_envelope(incoming, result)}
    monkeypatch.setattr(outbox, "claim_outbox_for_send", lambda _: saved)
    monkeypatch.setattr("app.db.has_successful_agent_response", lambda _: False)
    monkeypatch.setattr(outbox_worker, "has_later_queued_inbound", lambda _: True)
    mark = Mock()
    monkeypatch.setattr(outbox, "mark_outbox_failed", mark)
    send = AsyncMock()
    answer = await outbox.dispatch_accepted_outbound(4, send)
    assert answer["error"] == "superseded_by_later_inbox"
    assert mark.call_args.kwargs["dead"] is True
    send.assert_not_awaited()


@pytest.fixture
def inbox_fakes(monkeypatch):
    incoming, _ = envelope()
    monkeypatch.setattr(worker, "incoming_from_inbox_payload", lambda _: incoming.model_copy(deep=True))
    monkeypatch.setattr(worker, "attach_recent_image_for_followup", lambda item: item)
    monkeypatch.setattr("app.human_takeover.human_takeover_active", lambda _: False)
    monkeypatch.setattr(worker, "claim_inbound_message", lambda _: (False, 91))
    monkeypatch.setattr(worker, "has_successful_agent_response", lambda _: False)
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(database_url=""))
    monkeypatch.setattr(worker, "_customer_context_for", AsyncMock(return_value={"found": False}))
    monkeypatch.setattr(worker, "insert_agent_response", Mock())
    done = Mock()
    monkeypatch.setattr(worker, "mark_inbox_processed", done)
    return incoming, done


@pytest.mark.asyncio
async def test_duplicate_inbound_with_accepted_reply_never_runs_agent(inbox_fakes, monkeypatch):
    incoming, done = inbox_fakes
    _, result = envelope()
    accepted = {"id": 4, "reply_text": result.reply_text,
                "reply_payload": outbox.build_outbound_envelope(incoming, result)}
    monkeypatch.setattr(worker, "get_accepted_outbound", lambda _: accepted)
    pipeline = AsyncMock(side_effect=AssertionError("Agent must not run twice"))
    monkeypatch.setattr("app.message_pipeline.process_incoming_message", pipeline)
    dispatch = AsyncMock(return_value={"ok": False, "queued": True, "skipped": True, "status": "failed"})
    monkeypatch.setattr(worker, "dispatch_accepted_outbound", dispatch)
    answer = await worker._process_inbox_row_locked({"id": 3, "payload_json": {}, "lease_owner": "owner-2"})
    assert answer["queued"]
    pipeline.assert_not_awaited()
    assert done.call_args.kwargs["owner"] == "owner-2"


@pytest.mark.asyncio
async def test_duplicate_without_accepted_reply_recovers_interrupted_processing(inbox_fakes, monkeypatch):
    _, done = inbox_fakes
    events = []
    monkeypatch.setattr(worker, "get_accepted_outbound", lambda _: None)
    async def generate(*args):
        events.append("generate")
        return AgentResult(reply_text="Resposta aceita")
    monkeypatch.setattr("app.message_pipeline.process_incoming_message", generate)
    def accept(**kwargs):
        events.append("persist")
        assert kwargs["result"].reply_text == "Resposta aceita"
        return 4
    monkeypatch.setattr(worker, "enqueue_accepted_outbound", accept)
    async def dispatch(*args):
        events.append("send")
        return {"ok": False, "error": "temporary"}
    monkeypatch.setattr(worker, "dispatch_accepted_outbound", dispatch)
    await worker._process_inbox_row_locked({"id": 3, "payload_json": {}})
    assert events == ["generate", "persist", "send"]
    done.assert_called_once()
    assert "turn_trace" in worker.insert_agent_response.call_args.args[0]["response_metadata"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["new_message", "takeover", "expired_lease"])
async def test_retry_rechecks_conversation_and_ownership_before_sending(monkeypatch, reason):
    from app.ingress import outbox_worker
    incoming, result = envelope()
    row = {"id": 4, "inbound_id": 91, "lease_owner": "owner", "reply_text": result.reply_text,
           "reply_payload": outbox.build_outbound_envelope(incoming, result)}
    monkeypatch.setattr("app.db.is_latest_inbound_message", lambda *args: reason != "new_message")
    monkeypatch.setattr("app.human_takeover.human_takeover_active", lambda _: reason == "takeover")
    monkeypatch.setattr(outbox_worker, "outbox_lease_current", lambda _: reason != "expired_lease")
    send = AsyncMock()
    monkeypatch.setattr(worker, "_send_reply", send)
    answer = await outbox_worker._resend_outbox_row(row)
    assert not answer["ok"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_retry_reaches_dead_letter_at_configured_limit(monkeypatch):
    from app.ingress import outbox_worker
    monkeypatch.setattr(outbox_worker, "claim_pending_outbox", lambda **kwargs: [
        {"id": 4, "attempts": 3, "max_attempts": 3, "lease_owner": "owner"}])
    monkeypatch.setattr(outbox_worker, "_resend_outbox_row", AsyncMock(return_value={"ok": False, "error": "down"}))
    mark = Mock()
    monkeypatch.setattr(outbox_worker, "mark_outbox_failed", mark)
    report = await outbox_worker.process_outbox_batch()
    assert report["dead"] == 1
    assert mark.call_args.kwargs["dead"] is True
    assert mark.call_args.kwargs["owner"] == "owner"


@pytest.mark.asyncio
async def test_expired_inbox_lease_never_starts_agent(inbox_fakes, monkeypatch):
    monkeypatch.setattr(worker, "renew_inbox_lease", lambda *args, **kwargs: False)
    process = AsyncMock()
    monkeypatch.setattr(worker, "_process_inbox_row_locked", process)
    result = await worker.process_inbox_row({"id": 3, "payload_json": {}, "lease_owner": "stale"})
    assert result["error"] == "lease_lost"
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivered_audit_recovers_missing_outbox_receipt_without_resend(monkeypatch):
    from app.ingress import outbox_worker
    incoming, result = envelope()
    row = {"id": 4, "inbound_id": 91, "reply_text": result.reply_text,
           "reply_payload": outbox.build_outbound_envelope(incoming, result)}
    monkeypatch.setattr(outbox_worker, "outbox_lease_current", lambda _: True)
    monkeypatch.setattr("app.db.has_successful_agent_response", lambda _: True)
    send = AsyncMock()
    monkeypatch.setattr(worker, "_send_reply", send)
    assert (await outbox_worker._resend_outbox_row(row))["recovered_receipt"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_both_queues_are_attempted_even_when_inbox_is_unavailable(monkeypatch):
    from app.ingress.dispatch import process_pending_queues
    monkeypatch.setattr(worker, "process_inbox_batch", AsyncMock(side_effect=RuntimeError("unavailable")))
    retry = AsyncMock(return_value={"ok": True, "sent": 1})
    monkeypatch.setattr("app.ingress.outbox_worker.process_outbox_batch", retry)
    result = await process_pending_queues()
    assert not result["ok"]
    assert result["outbox"]["sent"] == 1
    retry.assert_awaited_once()
