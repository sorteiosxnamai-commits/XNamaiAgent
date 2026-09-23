"""B11 — cada log de entrega carrega trace, inbound, outbox e tentativa; sem PII."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.ingress import outbox, outbox_worker, worker
from app.models import AgentResult, IncomingMessage

PHONE = "5511987654321"


def _obs_lines(output: str) -> list[dict]:
    return [json.loads(line.split("[agent.obs] ", 1)[1]) for line in output.splitlines() if "[agent.obs] " in line]


@pytest.mark.asyncio
async def test_worker_delivery_logs_are_correlated_and_pii_free(monkeypatch, capsys):
    incoming = IncomingMessage(provider="ycloud", channel="whatsapp", sender_phone=PHONE, conversation_id=f"wa:{PHONE}")
    row = {
        "id": 77, "inbound_id": 501, "inbox_id": None, "lease_owner": "w", "attempts": 2, "max_attempts": 8,
        "channel": "whatsapp", "reply_text": "Seu pedido está em revisão.",
        "reply_payload": outbox.build_outbound_envelope(incoming, AgentResult(reply_text="Seu pedido está em revisão.")),
    }
    monkeypatch.setattr(outbox_worker, "claim_pending_outbox", lambda **kwargs: [row])
    monkeypatch.setattr(outbox_worker, "outbox_lease_current", lambda _: True)
    monkeypatch.setattr(outbox_worker, "has_later_queued_inbound", lambda _: False)
    monkeypatch.setattr("app.db.has_successful_agent_response", lambda _: False)
    monkeypatch.setattr("app.db.is_latest_inbound_message", lambda *args: True)
    monkeypatch.setattr("app.human_takeover.human_takeover_active", lambda _: False)
    monkeypatch.setattr(outbox_worker, "get_settings", lambda: SimpleNamespace(agent_inbox_batch_size=5))
    monkeypatch.setattr(outbox_worker, "mark_outbox_failed", Mock())
    monkeypatch.setattr(worker, "_send_reply", AsyncMock(side_effect=httpx.ReadTimeout("read")))

    await outbox_worker.process_outbox_batch()

    output = capsys.readouterr().out
    events = {line["event"]: line for line in _obs_lines(output)}
    delivery = events["outbox.send_exception"]
    assert delivery["trace_id"] == "outbox-77-a2"
    assert delivery["inbound_id"] == 501
    assert delivery["outbox_id"] == 77
    assert delivery["delivery_attempt"] == 2
    assert delivery["outcome"] == "unknown"
    assert PHONE not in output
    # o contexto da entrega nao vaza para o proximo log fora do loop
    assert events["outbox.batch_processed"].get("outbox_id") is None


@pytest.mark.asyncio
async def test_inline_dispatch_tags_the_inbound_turn_with_the_delivery(monkeypatch):
    from app.runtime_context import reset_current_turn, set_current_turn
    from app.turn_runtime import TurnRuntimeContext

    row = {"id": 9, "attempts": 1, "max_attempts": 8, "lease_owner": "inline", "reply_text": "ok",
           "reply_payload": {}}
    monkeypatch.setattr(outbox, "claim_outbox_for_send", lambda _: row)
    monkeypatch.setattr(outbox, "mark_outbox_sent", lambda *a, **k: True)
    monkeypatch.setattr(outbox_worker, "_resend_outbox_row", AsyncMock(return_value={"ok": True}))

    runtime = TurnRuntimeContext(trace_id="inbox-3", inbound_id=12)
    token = set_current_turn(runtime)
    try:
        await outbox.dispatch_accepted_outbound(9, AsyncMock())
    finally:
        reset_current_turn(token)
    assert (runtime.outbox_id, runtime.delivery_attempt) == (9, 1)


def test_llm_and_commerce_observations_share_the_turn_trace(capsys):
    from app.observability import log_event
    from app.runtime_context import reset_current_turn, set_current_turn
    from app.turn_runtime import TurnRuntimeContext

    token = set_current_turn(TurnRuntimeContext(trace_id="inbox-5", inbound_id=40, channel="whatsapp"))
    try:
        log_event("commerce.call", {"tool": "search_products"})
        log_event("openai.call", {"call_type": "decision"})
    finally:
        reset_current_turn(token)
    lines = _obs_lines(capsys.readouterr().out)
    assert {line["trace_id"] for line in lines} == {"inbox-5"}
    assert {line["inbound_id"] for line in lines} == {40}
