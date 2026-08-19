from __future__ import annotations

import re
import unicodedata
from typing import Any

from .commerce_context import CommerceConversationState
from .models import AgentResult


_SHORT_AFFIRMATIONS = {
    "sim",
    "si",
    "yes",
    "ok",
    "okay",
    "certo",
    "beleza",
    "blz",
    "pode",
    "pode ser",
    "isso",
    "uhum",
    "uhu",
}


def _fold(text: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", (text or "").casefold())
    return " ".join(
        "".join(char for char in normalized if not unicodedata.combining(char)).split()
    )


def commerce_state_resumable_score(state: dict[str, Any] | CommerceConversationState | None) -> int:
    payload = (
        state.model_dump(mode="json")
        if isinstance(state, CommerceConversationState)
        else (state if isinstance(state, dict) else {})
    )
    score = 0
    if payload.get("order_id") or payload.get("order_lookup_id"):
        score += 100
    if payload.get("order_payment_url"):
        score += 40
    if payload.get("pending_action") == "awaiting_payment":
        score += 30
    if payload.get("cart_session_id"):
        score += 20
    if payload.get("pending_action"):
        score += 10
    if payload.get("active_product") or payload.get("last_presented_products"):
        score += 5
    return score


def has_resumable_commerce(
    state: CommerceConversationState | dict[str, Any] | None,
) -> bool:
    return commerce_state_resumable_score(state) >= 20


def merge_commerce_states(
    primary: dict[str, Any] | None,
    fallback: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep the latest state, but recover order/payment facts wiped by a later turn."""
    base = dict(primary or {})
    donor = dict(fallback or {})
    if not donor:
        return base
    if commerce_state_resumable_score(base) >= commerce_state_resumable_score(donor):
        # Still recover order fields if a later greeting/cart turn wiped them.
        if not base.get("order_id") and donor.get("order_id"):
            for key in (
                "order_id",
                "order_status",
                "order_status_group",
                "order_session_id",
                "order_created_at",
                "order_lookup_id",
                "order_payment_method_id",
                "order_payment_method",
                "order_payment_type",
                "order_payment_url",
                "order_payment_status",
                "order_has_payment",
                "order_payment_date",
                "pending_action",
                "purchase_stage",
                "active_product",
                "cart_session_id",
                "cart_url",
                "checkout_draft",
            ):
                if base.get(key) in (None, "", [], {}) and donor.get(key) not in (
                    None,
                    "",
                    [],
                    {},
                ):
                    base[key] = donor[key]
            if (
                base.get("pending_action") is None
                and donor.get("pending_action") == "awaiting_payment"
            ):
                base["pending_action"] = "awaiting_payment"
        return base
    return donor


def is_short_affirmation(text: str | None) -> bool:
    folded = _fold(text).strip("!?.,")
    return folded in _SHORT_AFFIRMATIONS


def is_soft_greeting(text: str | None) -> bool:
    folded = _fold(text).strip("!?.,")
    if folded in {
        "oi",
        "ola",
        "olá",
        "bom dia",
        "boa tarde",
        "boa noite",
        "noite",
        "tarde",
        "oi tudo bem",
        "ola tudo bem",
        "olá tudo bem",
    }:
        return True
    return bool(
        re.fullmatch(
            r"(opa|oie|eai|e ai|hey|ola|olá|oi)?[,\s]*"
            r"(boa noite|bom dia|boa tarde|ola|olá|oi|tudo bem)?",
            folded,
        )
        and any(
            token in folded
            for token in ("oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "opa")
        )
    )


def is_payment_link_request(text: str | None) -> bool:
    folded = _fold(text)
    signals = (
        "me da o pix",
        "me dá o pix",
        "me da o link",
        "me dá o link",
        "me manda o link",
        "me envia o link",
        "manda o pix",
        "manda o link",
        "envia o pix",
        "envia o link",
        "passa o link",
        "link de pagamento",
        "link do pagamento",
        "link para pagamento",
        "link pro pagamento",
        "link pra pagamento",
        "pix para pagamento",
        "quero o pix",
        "quero o link",
        "gerar o pix",
        "gera o pix",
        "codigo pix",
        "código pix",
    )
    return any(signal in folded for signal in signals)


def is_unpaid_order_resume_request(text: str | None) -> bool:
    folded = _fold(text)
    if is_payment_link_request(text):
        return True
    unpaid_signals = (
        "nao paguei",
        "não paguei",
        "nao fiz o pagamento",
        "não fiz o pagamento",
        "falta pagar",
        "ainda nao paguei",
        "ainda não paguei",
        "link de pagamento",
        "link do pagamento",
        "quero pagar",
        "vou pagar",
        "pagamento caiu",
        "pagamento pendente",
        "confirmar se o pagamento",
        "confirma se o pagamento",
        "pagamento",
    )
    conversation_signals = (
        "acabamos de conversar",
        "acabamos de falar",
        "acabou de conversar",
        "ja conversamos",
        "já conversamos",
        "continuamos",
        "continuar",
        "mais acima",
    )
    return any(signal in folded for signal in unpaid_signals) or (
        "pedido" in folded and any(signal in folded for signal in conversation_signals)
    )


def should_resume_pending_order(
    text: str | None,
    state: CommerceConversationState,
    *,
    is_greeting: bool = False,
    allow_without_state: bool = False,
) -> bool:
    """Resume payment/order only when the customer asks — not on bare greetings."""
    has_order_handle = bool(
        state.order_id
        or state.order_lookup_id
        or state.order_payment_url
        or (state.cart_session_id and state.pending_action == "awaiting_payment")
    )
    if not has_order_handle and not allow_without_state:
        return False
    # Soft greetings keep memory loaded but must not dump payment links.
    if is_greeting or is_soft_greeting(text):
        return False
    if is_payment_link_request(text) or is_unpaid_order_resume_request(text):
        return True
    if (
        state.pending_action == "awaiting_payment"
        and is_short_affirmation(text)
    ):
        return True
    return False


def build_contextual_greeting(state: CommerceConversationState) -> AgentResult:
    """Soft greeting: keep commerce memory silently; never volunteer order/payment."""
    _ = state  # Memory stays in pipeline state; reply remains non-intrusive.
    return AgentResult(
        reply_text="Olá! Em que posso ajudar?",
        intent="general",
        response_metadata={
            "domain": "greeting",
            "response_source": "context_resume_soft",
        },
    )
