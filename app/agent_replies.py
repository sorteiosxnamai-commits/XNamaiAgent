from __future__ import annotations

from typing import Any

from .models import AgentResult, IncomingMessage
from .site_knowledge import THIRD_PARTY_REFUSAL
from .user_preferences import detect_preferred_name_update, save_preferred_name

# Generic name preferences and third-party privacy replies.


def build_preferred_name_reply(message: IncomingMessage, account: dict[str, Any]) -> AgentResult | None:
    preferred_name = detect_preferred_name_update(message.text)
    if not preferred_name or not account.get("found"):
        return None

    save_preferred_name(int(account["user_id"]), preferred_name)
    # Confirmacao operacional de um dado gravado; sem variacao de personalidade.
    return AgentResult(
        reply_text=f"Perfeito! A partir de agora vou te chamar de {preferred_name}.",
        intent="preferred_name_update",
        handoff_required=False,
    )


def _third_party_reply() -> AgentResult:
    return AgentResult(
        reply_text=THIRD_PARTY_REFUSAL,
        intent="security_refusal",
        handoff_required=False,
        safety_reason="third_party_account_inquiry",
    )
