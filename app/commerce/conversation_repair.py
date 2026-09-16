"""Recover explicit misunderstandings without executing purchase actions."""
import re
import unicodedata

from app.models import AgentResult


_COMPLAINT = re.compile(
    r"^(?:(?:voce|voces)\s+)?(?:nao (?:foi isso que (?:eu )?(?:perguntei|pedi)|"
    r"(?:me )?entendeu|perguntei (?:o )?(?:preco|valor))|"
    r"(?:ta|esta) entendendo nada|que pergunta de preco)[\s?!.,:;]*"
)


def repair_request(text: str) -> tuple[bool, str]:
    folded = unicodedata.normalize("NFKD", text.casefold()).encode("ascii", "ignore").decode().strip()
    match = _COMPLAINT.match(folded)
    return (True, folded[match.end():].strip()) if match else (False, "")


async def recover_conversation(text, *, state, execute, render, handoff_after=None):
    from app.business_policy import current_policy
    policy = current_policy()
    handoff_after = handoff_after if handoff_after is not None else policy.conversation_repair_handoff_after
    complaint, remainder = repair_request(text)
    if not complaint or state is None:
        return None
    state.conversation_repair_attempts += 1
    metadata = {"domain": "commerce", "used_commerce_provider": False,
                "commerce_turn_state": {"conversation_repair_attempts": state.conversation_repair_attempts,
                                        "pending_commerce_action": None,
                                        "last_catalog_query": state.last_catalog_query}}
    if state.conversation_repair_attempts >= handoff_after:
        return AgentResult(reply_text=policy.repair_handoff,
                           intent="commerce", handoff_required=True, safety_reason="conversation_repair_failed",
                           response_metadata=metadata)
    query = remainder or state.last_catalog_query
    if not query:
        return AgentResult(reply_text=policy.repair_clarification,
                           intent="commerce", safety_reason="conversation_repair_missing_context", response_metadata=metadata)
    from .turn_resolver import READ_ONLY_ACTIONS, resolve_commerce_turn
    from .turn_flow import run_commerce_turn

    recovery_state = state.model_copy(deep=True)
    recovery_state.pending_commerce_action = None
    # A complaint must not replay a cart/order mutation, even if it includes
    # "confirmo" or the previous conversation was awaiting a purchase approval.
    decision = resolve_commerce_turn(query, state=recovery_state)
    if decision.action not in READ_ONLY_ACTIONS:
        return AgentResult(reply_text="Entendi, interpretei errado. Pode me dizer qual informação você precisa?",
                           intent="commerce", safety_reason="conversation_repair_clarification", response_metadata=metadata)
    outcome = await run_commerce_turn(query, state=recovery_state, execute=execute)
    response = render(outcome, recovery_state)
    if response is None:
        return AgentResult(reply_text="Não consegui confirmar essa informação agora. Pode me indicar o produto ou referência?",
                           intent="commerce", safety_reason="conversation_repair_unavailable", response_metadata=metadata)
    response.reply_text = policy.repair_ack + "\n\n" + response.reply_text
    response.response_metadata.setdefault("commerce_turn_state", {}).update({
        "conversation_repair_attempts": state.conversation_repair_attempts,
        "pending_commerce_action": None,
        "last_catalog_query": recovery_state.last_catalog_query,
    })
    response.response_metadata["conversation_repair"] = {"attempt": state.conversation_repair_attempts}
    return response
