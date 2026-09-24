"""Deterministic account flows (XNaMai Club and customer registration).

Runs before any model interpretation, shared by ``openai_agent`` and
``sales_agent``: the LLM never decides, starts or narrates a registration and
never invents Club conditions. Order:

1. Club questions (information only; never creates a membership);
2. a pending registration (data, corrections, confirmation, cancel, greetings);
3. a new registration request recognised by the Intent Router.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from . import capability_catalog
from .club_xnamai import handle_club_turn, is_club_request, is_club_followup
from .club_provider import ClubProvider
from .commerce_context import CommerceConversationState
from .customer_registration import (
    REGISTRATION_PENDING_ACTIONS,
    handle_customer_registration_turn,
    registration_capability_available,
)
from .models import AgentResult, IncomingMessage
from .sales.intent_router import is_customer_registration_request


async def handle_account_flows(
    message: IncomingMessage,
    *,
    state: CommerceConversationState,
    execute: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> AgentResult | None:
    text = message.text
    if is_club_request(text) or (
        state.pending_action not in REGISTRATION_PENDING_ACTIONS and is_club_followup(text, state)
    ):
        from .config import get_settings

        plans = await ClubProvider(get_settings().club_api_url).get_public_plans()
        return handle_club_turn(text, state=state, plans=plans)
    registration_turn = (
        state.pending_action in REGISTRATION_PENDING_ACTIONS
        or is_customer_registration_request(text)
    )
    if not registration_turn:
        return None
    return await handle_customer_registration_turn(
        text,
        state=state,
        execute=execute,
        registration_enabled=True,
        commit_enabled=registration_capability_available(
            capability_catalog.runtime_commerce_capabilities()
        ),
        sender_name=message.sender_name,
        sender_phone=message.sender_phone,
    )
