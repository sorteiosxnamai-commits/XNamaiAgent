"""Deterministic registration; the model never receives mutation tools or drafts."""
import asyncio

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.conversation_choices import resolve_choice
from app.customer_registration import (
    PENDING_REGISTRATION_CONFIRMATION, handle_customer_registration_turn,
    registration_capability_available,
)
from app.memory_scope import restore_state, stamp_state


async def load_state(incoming, previous):
    if incoming.provider == "test":
        payload = restore_state((previous or {}).get("commerce_state"), incoming.workspace_id)
    else:
        from app.db import load_commerce_conversation_state
        payload = await asyncio.to_thread(load_commerce_conversation_state,
            workspace_id=incoming.workspace_id, conversation_id=incoming.conversation_id,
            sender_key=incoming.sender_key, sender_phone=incoming.sender_phone,
            before_inbound_id=(incoming.raw or {}).get("inbound_id"))
    return CommerceConversationState.from_payload(payload)


async def registration_turn(incoming, state, provider, history, previous):
    preview = incoming.provider == "test"
    ready = not preview and registration_capability_available(
        getattr(provider, "runtime_capabilities", set()))

    async def execute(capability, arguments):
        if preview or capability not in {"lookup_customer_by_document", "create_customer"}:
            return {"ok": False, "error": "registration_not_allowed"}
        # The provider uses synchronous DB access internally. Keep the event loop
        # responsive, while its durable document claim prevents a second POST.
        return await asyncio.to_thread(lambda: asyncio.run(provider.execute(capability, arguments)))

    text = incoming.text or ""
    choice = resolve_choice(text, state, history)
    if not state.pending_action and choice and choice.kind == "registration" and choice.value == "cpf":
        text = "quero me cadastrar como pessoa física"
    # After an informational detour, ask for a new review confirmation. A bare
    # 'sim' must not approve a registration when the last question was unrelated.
    if (state.pending_action == PENDING_REGISTRATION_CONFIRMATION
            and not (previous or {}).get("customer_registration_state")):
        state = state.model_copy(deep=True)
        state.pending_action = "awaiting_customer_registration_data"
    return await handle_customer_registration_turn(text, state=state, execute=execute,
        registration_enabled=ready, commit_enabled=ready,
        sender_name=incoming.sender_name, sender_phone=incoming.sender_phone)


async def attach_state(incoming, result, state):
    updated = evolve_commerce_state(state, result)
    result.response_metadata["commerce_state"] = stamp_state(
        updated.model_dump(mode="json"), incoming.workspace_id)
    if incoming.provider != "test":
        from app.conversation_lifecycle import operation_snapshot, persist_state
        await asyncio.to_thread(persist_state, incoming, operation_snapshot(updated, state))
        result.response_metadata["conversation_commit"] = {"version": 1}
    return result
