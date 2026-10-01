"""Resolve small answers to the last question without authorizing operations."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .commerce.generic_catalog import normalize_text
from .models import AgentResult


_OPTIONS = {
    "cpf": ("registration", ("cpf", "pessoa fisica", "pf")),
    "cnpj": ("registration", ("cnpj", "pessoa juridica", "pj")),
    "pix": ("payment", ("pix",)),
    "cartão": ("payment", ("cartao", "cartao de credito", "credito", "debito")),
    "dinheiro": ("payment", ("dinheiro",)),
    "retirada": ("delivery", ("retirada", "retirar", "vou retirar")),
    "entrega": ("delivery", ("entrega", "receber")),
}


def short_option(text):
    value = normalize_text(text or "").strip(" .!?,")
    value = re.sub(r"^(?:(?:com|por|no|na|prefiro|quero|seria|pelo|pela|o|a)\s+)+", "", value)
    value = re.sub(r"\s+por favor$", "", value)
    for option, (kind, aliases) in _OPTIONS.items():
        if value in aliases:
            return kind, option
    return None


@dataclass(frozen=True)
class Choice:
    kind: str
    value: str
    question: str


def resolve_choice(text, state, recent_turns=None):
    option = short_option(text)
    if not option:
        return None
    # Prefer the latest delivered assistant turn to a potentially stale state.
    question = (getattr(state, "pending_followup", None) or {}).get("question", "")
    if recent_turns:
        assistant = next((t for t in reversed(recent_turns) if t.get("role") == "assistant"), None)
        if assistant:
            content = assistant.get("content") or ""
            if "?" not in content:
                # An invitation is a conversational pending choice even without
                # a question mark. Only the last topic can supply that choice.
                tail = next((part for part in reversed(re.split(r"(?<=[.!?])\s+", content))
                             if re.search(r"[a-z]", normalize_text(part))), "")
                folded_tail = normalize_text(tail)
                if option[0] != "registration" or not re.search(
                        r"\b(?:cadastro|cadastrar|cpf|cnpj)\b", folded_tail):
                    return None
                if not re.search(r"\b(?:cpf|cnpj|pessoa fisica|pessoa juridica)\b", normalize_text(content)):
                    return None
                question = content
            else:
                question = next((part for part in reversed(re.split(r"(?<=[.!?])\s+", content)) if "?" in part), "")
    if not question:
        return None
    folded = normalize_text(question)
    kind, value = option
    aliases = _OPTIONS[value][1]
    explicit = any(re.search(r"\b" + re.escape(a) + r"\b", folded) for a in aliases)
    category_question = {
        "registration": r"\b(?:cpf|cnpj|pessoa fisica|pessoa juridica)\b",
        "payment": r"\b(?:pagamento|pagar)\b",
        "delivery": r"\b(?:entrega|retirada|receber|retirar)\b",
    }[kind]
    if not (explicit or re.search(category_question, folded)):
        return None
    return Choice(kind, value, question)


async def handle_short_choice(message, *, state, execute, recent_turns=None, defer_ambiguous=False, interpretation=None):
    """Only local collection or informational replies; never provider mutations."""
    if message.image_url or message.transcription_failed:
        return None
    option = short_option(message.text)
    if not option:
        return None
    choice = resolve_choice(message.text, state, recent_turns)
    if choice is None and recent_turns and interpretation is not None:
        from .turn_understanding import get_turn_understanding
        understanding = get_turn_understanding(interpretation)
        if (interpretation._source == "openai" and understanding and understanding.confidence >= .75
                and option[0] == "registration" and understanding.registration_choice == option[1]):
            question = next((t.get("content", "") for t in reversed(recent_turns) if t.get("role") == "assistant"), "")
            choice = Choice("registration", option[1], question)
    if choice is None and recent_turns and defer_ambiguous:
        # Missing punctuation or a welcome after an offer must not force a
        # redundant menu. Let the shared interpreter read the whole exchange.
        return None
    # Existing data collection and verified checkout selections keep their owner.
    # A stale offer (link/photo/cart) cannot override a newer CPF/CNPJ question.
    registration_choice = bool(choice and choice.kind == "registration"
        and re.search(r"\bcpf\b", normalize_text(choice.question))
        and re.search(r"\bcnpj\b", normalize_text(choice.question)))
    stale_offer = state.pending_action in {"send_product_link", "create_cart", "show_images", "show_payment_options", "choose_checkout_channel"}
    if state.pending_action and not (registration_choice and stale_offer):
        return None
    if state.pending_commerce_action and not (registration_choice and state.pending_commerce_action == "browse_catalog"):
        return None
    kind, value = option
    checkout_active = bool(state.cart_id or state.cart_session_id or state.cart_items)
    if kind in {"payment", "delivery"} and (
        checkout_active
        or kind == "delivery" and state.shipping_quotes
    ):
        return None
    if choice and choice.kind == "registration" and choice.value == "cpf":
        from .customer_registration import handle_customer_registration_turn, registration_capability_available
        from .capability_catalog import runtime_commerce_capabilities
        result = await handle_customer_registration_turn(
            "quero me cadastrar como pessoa física", state=state, execute=execute, registration_enabled=True,
            commit_enabled=registration_capability_available(runtime_commerce_capabilities()),
            sender_name=message.sender_name, sender_phone=message.sender_phone,
        )
        if result is not None:
            result.response_metadata.update({"response_source": "contextual_registration_choice", "pending_followup": None,
                "commerce_turn_state": {"pending_commerce_action": None}})
        return result
    from .published_knowledge import published_policy
    if choice:
        topic = {"registration": "registration", "payment": "payment_methods", "delivery": "delivery"}[kind]
        policy = published_policy(topic)
        if kind == "registration":
            from .site_knowledge import STORE_URL
            reply = "Entendi, você quer comprar com CNPJ. 😊"
            reply += f"\n\nO cadastro com CNPJ pode ser feito diretamente no catálogo: {STORE_URL}"
            reply += "\n\nVocê já tem cadastro?"
        else:
            reply = f"Entendi, você prefere {value}. 😊"
            if policy:
                reply += "\n\n" + policy
            reply += "\n\nEssa preferência ainda precisa ser confirmada no fechamento do pedido. Você já finalizou o pedido no catálogo?"
    else:
        reply = {
            "registration": f"Você quer saber como comprar com {value.upper()} ou iniciar seu cadastro?",
            "payment": "Você quer saber como funciona essa forma de pagamento ou usá-la em um pedido?",
            "delivery": "Você quer saber como funciona a entrega ou retirada, ou combinar isso para um pedido?",
        }[kind]
    return AgentResult(reply_text=reply, intent="commerce", response_metadata={
        "domain": "commerce", "informational_only": True, "used_commerce_provider": False,
        "response_source": "contextual_choice", "active_topic": "commercial_guidance",
        **({"clear_pending_action": True, "commerce_turn_state": {"pending_commerce_action": None}} if registration_choice else {}),
        "conversation_goal": f"Orientar sobre {kind}: {value}",
    })
