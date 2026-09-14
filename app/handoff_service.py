from __future__ import annotations

from typing import Any

from .guardrails import default_safe_handoff, detect_human_support_request
from .models import AgentResult, IncomingMessage
from .site_knowledge import HUMAN_SUPPORT_MESSAGE, NS_SALES_WHATSAPP, TRADE_IN_HANDOFF_MESSAGE


def should_request_human_handoff(
    incoming: IncomingMessage,
    *,
    result: AgentResult | None = None,
) -> str | None:
    if result is not None and result.handoff_required:
        return result.safety_reason or "handoff_required"
    if detect_human_support_request(incoming.text):
        return "customer_requested_human"
    from .guardrails import detect_trade_in_or_appraisal_request

    if detect_trade_in_or_appraisal_request(incoming.text):
        return "trade_in_or_appraisal"
    return None


def build_human_handoff_result(
    *,
    reason: str,
    reply_text: str | None = None,
) -> AgentResult:
    text = (reply_text or "").strip()
    if not text:
        if reason == "trade_in_or_appraisal":
            text = TRADE_IN_HANDOFF_MESSAGE
        else:
            text = (
                "Vou encaminhar seu atendimento para a equipe da New Store. "
                f"{HUMAN_SUPPORT_MESSAGE}"
            )
    if reason.startswith("blocked_topic:"):
        text = default_safe_handoff()
    return AgentResult(
        reply_text=text,
        intent="handoff",
        handoff_required=True,
        safety_reason=reason,
        response_metadata={
            "domain": "guardrail",
            "response_source": "handoff",
            "handoff": {
                "required": True,
                "reason": reason,
                "contact_whatsapp": NS_SALES_WHATSAPP,
                "provider_action": "mark_for_human",
            },
        },
    )


def enrich_handoff_metadata(
    incoming: IncomingMessage,
    result: AgentResult,
) -> AgentResult:
    reason = should_request_human_handoff(incoming, result=result)
    if not reason:
        return result
    result.handoff_required = True
    result.safety_reason = result.safety_reason or reason
    handoff = result.response_metadata.get("handoff")
    if not isinstance(handoff, dict):
        handoff = {}
    handoff.update(
        {
            "required": True,
            "reason": reason,
            "channel": incoming.channel,
            "conversation_id_present": bool(incoming.conversation_id),
            "visitor_id_present": bool(incoming.visitor_id),
            "contact_whatsapp": NS_SALES_WHATSAPP,
            "provider_action": "mark_for_human",
        }
    )
    result.response_metadata["handoff"] = handoff
    result.response_metadata.setdefault("domain", "guardrail")
    return result


def handoff_provider_payload(result: AgentResult) -> dict[str, Any] | None:
    handoff = (result.response_metadata or {}).get("handoff")
    if not isinstance(handoff, dict) or not handoff.get("required"):
        return None
    return {
        "required": True,
        "reason": handoff.get("reason"),
        "provider_action": handoff.get("provider_action"),
        "contact_whatsapp": handoff.get("contact_whatsapp"),
    }
