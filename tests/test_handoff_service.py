from app.handoff_service import (
    build_human_handoff_result,
    enrich_handoff_metadata,
    handoff_provider_payload,
    should_request_human_handoff,
)
from app.models import AgentResult, IncomingMessage
from app.site_knowledge import NS_SALES_WHATSAPP


def test_customer_request_triggers_handoff():
    incoming = IncomingMessage(
        channel="whatsapp",
        text="Quero falar com um atendente",
    )
    assert should_request_human_handoff(incoming) == "customer_requested_human"
    result = build_human_handoff_result(reason="customer_requested_human")
    assert result.handoff_required is True
    assert NS_SALES_WHATSAPP in result.reply_text
    assert handoff_provider_payload(result)["provider_action"] == "mark_for_human"


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
