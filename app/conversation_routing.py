"""Read-only conversation ownership before lexical commerce shortcuts.

The model interprets intent; it never authorizes an operation. Existing order,
catalog and checkout handlers retain their evidence and authorization contracts.
"""
from __future__ import annotations

from .turn_understanding import get_turn_understanding


def enabled(settings):
    return bool(getattr(settings, "agent_conversation_first_enabled", False)
                and getattr(settings, "agent_consultative_enabled", False)
                and not getattr(settings, "agent_consultative_emergency_off", False)
                and getattr(settings, "openai_api_key", None))


def owns_advice(interpretation, state):
    understanding = get_turn_understanding(interpretation)
    if (interpretation._source != "openai" or understanding is None
            or understanding.conversation_mode != "advice"
            or understanding.confidence < .75
            or interpretation.domain not in {"commerce", "store_general", "greeting"}):
        return False
    action = understanding.requested_action
    if (action is None or action.kind not in {"none", "checkout_question", "payment_options"}
            or action.confirmation != "none" or action.purchase_items or action.image_request
            or action.checkout_data or action.shipping_selection_id or action.shipping_selection_position
            or action.shipping_zipcode or action.payment_option_id or action.order_id
            or action.installment_count or action.payment_request_kind == "checkout"
            or set(understanding.required_tools) - {"none"}):
        return False
    return True


def owns_conversation(interpretation, state):
    """One conversational owner; ambiguity about wording is not a SKU query."""
    if owns_advice(interpretation, state):
        return True
    understanding = get_turn_understanding(interpretation)
    if (interpretation._source != "openai" or understanding is None
            or interpretation.domain not in {"commerce", "store_general", "greeting"}
            or understanding.confidence < .75):
        return False
    # Reuse exactly the same action boundary, including hidden checkout fields.
    candidate = understanding.model_copy(update={"conversation_mode": "advice"})
    if understanding.catalog_mode == "link" and candidate.requested_action and candidate.requested_action.kind in {"none", "search"}:
        candidate.requested_action = candidate.requested_action.model_copy(update={"kind": "none"})
        candidate.required_tools = ["none"]
    from .turn_understanding import attach_turn_understanding
    adapted = attach_turn_understanding(interpretation.model_copy(), candidate)
    return owns_advice(adapted, state)


def pending_order_reference(text, state, recent_turns):
    """A numeric answer to an order-number request is never a catalog search."""
    import re
    from .commerce.generic_catalog import normalize_text
    candidate = (text or "").strip().lstrip("#").strip()
    if not re.fullmatch(r"[0-9]{3,10}", candidate):
        return None
    if state.pending_action and state.pending_action not in {"awaiting_order_number", "awaiting_order_id"}:
        return None
    last = next((turn.get("content", "") for turn in reversed(recent_turns or [])
                 if turn.get("role") == "assistant"), "")
    if last:
        # A later product question supersedes an older order topic.
        value = normalize_text(last)
        expected = re.search(r"\b(?:numero|codigo|referencia)\s+(?:(?:de|do|desse|deste|seu|meu)\s+){0,3}pedido\b", value)
        return candidate if expected else None
    return candidate if state.active_topic == "order_status" else None


def has_institutional_questions(interpretation):
    """Mixed catalog requests keep complete listing plus answer other questions."""
    understanding = get_turn_understanding(interpretation) if interpretation else None
    if understanding is None or len(understanding.questions) < 2:
        return False
    from .commerce.generic_catalog import normalize_text
    questions = normalize_text(" ".join(understanding.questions))
    return any(word in questions for word in ("atacado", "varejo", "cadastro", "cpf", "cnpj", "minimo", "entrega", "pagamento"))


async def mixed_catalog_reply(message, context, interpretation, state, settings, execute):
    """Resolve all clauses before a CPF keyword can steal the whole request."""
    if not has_institutional_questions(interpretation) or not getattr(settings, "mercos_adaptor_configured", False):
        return None
    understanding = get_turn_understanding(interpretation)
    action = understanding.requested_action
    if (interpretation._source != "openai" or understanding.confidence < .75
            or action is None or action.kind not in {"none", "search", "recommend"} or action.purchase_items
            or action.confirmation != "none" or action.checkout_data or action.image_request):
        return None
    from .wholesale_catalog import requested_queries, handle_wholesale_catalog
    queries = []
    for question in understanding.questions:
        for query in requested_queries(question, state) or []:
            if query["query"] not in {q["query"] for q in queries}:
                queries.append(query)
    if not queries:
        return None
    result = await handle_wholesale_catalog(message.text, state=state, execute=execute, queries_override=queries)
    if result is None:
        return None
    from .consultative_agent import consult
    guidance = await consult(message, context, interpretation, settings=settings, state=state,
                             informational_only=True, catalog_answered=True)
    if guidance is not None and not guidance.safety_reason:
        result.reply_text = guidance.reply_text + "\n\n" + result.reply_text
        result.response_metadata.update({"response_source": "consultative_openai", "used_openai_responder": True,
            "answered_institutional_questions": True})
    return result
