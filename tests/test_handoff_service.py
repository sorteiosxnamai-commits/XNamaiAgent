from app.handoff_service import (
    build_human_handoff_result,
    enrich_handoff_metadata,
    handoff_provider_payload,
    should_request_human_handoff,
)
from app.models import AgentResult, IncomingMessage


def test_customer_request_triggers_handoff():
    incoming = IncomingMessage(
        channel="whatsapp",
        text="Quero falar com um atendente",
    )
    assert should_request_human_handoff(incoming) == "customer_requested_human"
    result = build_human_handoff_result(reason="customer_requested_human")
    assert result.handoff_required is True
    # O contato da marca legada saiu da copy: encaminhar para la mandaria o
    # cliente da XNamai para outra empresa.
    assert "XNamai" in result.reply_text
    assert "+55" not in result.reply_text
    payload = handoff_provider_payload(result)
    assert payload["provider_action"] == "mark_for_human"
    assert payload["confirmed"] is True
    assert payload["consent_reason"] == "customer_requested_human"


def test_exact_customer_phrase_from_whatsapp_confirms_queue_handoff():
    incoming = IncomingMessage(
        channel="whatsapp",
        text="quero falar com um humano",
    )
    result = enrich_handoff_metadata(
        incoming,
        AgentResult(reply_text="Vou chamar a equipe."),
    )

    assert result.handoff_required is True
    assert result.safety_reason == "customer_requested_human"
    assert result.response_metadata["handoff"] == {
        "required": True,
        "reason": "customer_requested_human",
        "confirmed": True,
        "consent_reason": "customer_requested_human",
        "channel": "whatsapp",
        "conversation_id_present": False,
        "visitor_id_present": False,
        "contact_whatsapp": None,
        "provider_action": "mark_for_human",
    }


def test_enrich_handoff_keeps_existing_reason():
    incoming = IncomingMessage(channel="instagram", text="ok")
    result = AgentResult(
        reply_text="Encaminhando",
        intent="handoff",
        handoff_required=True,
        safety_reason="blocked_topic:apostar",
    )
    enriched = enrich_handoff_metadata(incoming, result)
    assert enriched.response_metadata["handoff"]["reason"] == "blocked_topic:apostar"
    assert enriched.response_metadata["handoff"]["channel"] == "instagram"
    assert enriched.response_metadata["handoff"]["confirmed"] is False
    assert enriched.response_metadata["handoff"]["consent_reason"] is None
