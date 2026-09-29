"""Dialogue Phase — "em que etapa da conversa comercial estamos?".

Derived from the commerce state the pipeline already persists (no extra persisted
field), adjusted by what the customer is doing in THIS turn (intent +
interpretation). Intent answers "what does the customer want now"; the phase
answers "where are we"; Qualification Slots answer "what do we know". None of
them decides what to say.

Phases (validated against ``CommerceConversationState``; see
``docs/dialogue_phase_and_slots.md``):

``discovery``     nothing presented or selected yet
``shortlist``     options were presented (``last_presented_products``)
``product_focus`` one product is the subject (``active_product``)
``cart``          items in the cart (``cart_items`` / ``cart_session_id``)
``checkout``      collecting channel / shipping / checkout data
``order_review``  order summary awaiting the customer's confirmation
``after_sale``    an order exists (payment pending, placed, or finished)

Transitions. Forward moves come from the state the previous turn persisted.
This turn can only move the phase by explicit evidence:

1. ``shortlist`` -> ``product_focus``: the customer points at a presented item
   (position, name, "esse", the recommendation).
2. ``shortlist`` / ``product_focus`` -> ``discovery``: a new independent search
   (new subject, not referring to the previous context), an explicit change of
   subject, "começar de novo", or "nenhuma dessas".
3. ``cart`` -> ``product_focus`` (or ``discovery``): removing the only item.
4. ``after_sale`` + a product turn (not about the existing order): a NEW sale;
   the phase is read from the browsing fields, ignoring the order, then rules
   1-2 apply. A question about the order keeps ``after_sale``.

``cart``/``checkout``/``order_review`` are an open sale: browsing more products
does not regress them. Non-commerce turns (greeting, store question) never move
the phase.

Handoff is NOT a phase: a customer in ``product_focus`` can be handed to a
human and still be in ``product_focus``. It travels as ``handoff_active``.

This module only reads. It never sends, writes state/memory/outbox, calls a
provider, or touches persona/tone (``tests/test_dialogue_phase.py`` guards it).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.commerce_context import CommerceConversationState
    from app.models import SalesInterpretation
    from app.sales.intent_router import IntentResult

DialoguePhase = Literal[
    "discovery", "shortlist", "product_focus", "cart", "checkout", "order_review", "after_sale",
]

#: Order of commercial progress (for reading; the phase is not forced forward).
PHASE_ORDER: tuple[DialoguePhase, ...] = (
    "discovery", "shortlist", "product_focus", "cart", "checkout", "order_review", "after_sale",
)

_AFTER_SALE_STAGES = frozenset({"awaiting_payment", "payment_confirmed", "after_sales"})
_REVIEW_PENDING_ACTIONS = frozenset({"awaiting_order_confirmation", "awaiting_order_customer_document"})
_CHECKOUT_PENDING_ACTIONS = frozenset({
    "choose_checkout_channel", "awaiting_shipping_zipcode", "awaiting_shipping_selection", "awaiting_checkout_data",
})
_CHECKOUT_STAGES = frozenset({"checkout_channel_selection", "shipping", "checkout_ready"})

#: The customer points at something already on screen.
_SELECTION_REFERENCES = frozenset({
    "list_position", "explicit_product", "last_presented_product", "previous_recommendation", "current_product",
})
#: Intents that open a search (as opposed to asking about the focused item).
_SEARCH_INTENTS = frozenset({"product_search", "recommendation", "product_comparison", "clarification"})
#: Phases a new independent search leaves. Cart/checkout/review keep the open sale.
_BROWSING_PHASES = frozenset({"shortlist", "product_focus"})

_FRESH_START_RE = re.compile(
    r"\b(come[cç]ar de novo|recome[cç]ar|do zero|nova conversa|outra conversa)\b"
)
_SHORTLIST_REJECTION_RE = re.compile(
    r"^\s*(nenhuma|nenhum|nenhuma dessas|nenhum desses|nao gostei de nenhuma|nao gostei de nenhum)\s*[!.?]*\s*$"
)


@dataclass(frozen=True)
class DialogueState:
    """Where the conversation is in this turn."""

    phase: DialoguePhase
    #: Phase implied by the persisted state alone, before this turn's evidence.
    state_phase: DialoguePhase
    handoff_active: bool = False

    @property
    def transitioned(self) -> bool:
        return self.phase != self.state_phase


def _fold(text: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).strip()


def phase_from_state(
    state: "CommerceConversationState | None",
    *,
    ignore_order: bool = False,
) -> DialoguePhase:
    """Most advanced stage the persisted state proves."""
    if state is None:
        return "discovery"
    stage = str(state.purchase_stage or "")
    pending = state.pending_action
    if not ignore_order:
        if state.order_id or state.order_lookup_id or pending == "awaiting_payment" or stage in _AFTER_SALE_STAGES:
            return "after_sale"
        if (
            state.order_confirmation_status == "pending"
            or state.order_review_version
            or pending in _REVIEW_PENDING_ACTIONS
        ):
            return "order_review"
        if pending in _CHECKOUT_PENDING_ACTIONS or stage in _CHECKOUT_STAGES:
            return "checkout"
        if state.cart_items or state.cart_session_id or stage == "cart_created":
            return "cart"
    if state.active_product is not None:
        return "product_focus"
    if state.last_presented_products:
        return "shortlist"
    return "discovery"


def message_restarts_discovery(message_text: str | None) -> bool:
    """Customer explicitly starts over or rejects the whole shortlist."""
    folded = _fold(message_text)
    return bool(_FRESH_START_RE.search(folded) or _SHORTLIST_REJECTION_RE.match(folded))


def _starts_independent_search(
    interpretation: "SalesInterpretation | None",
    intent: "IntentResult | None",
) -> bool:
    if interpretation is None or intent is None or intent.intent not in _SEARCH_INTENTS:
        return False
    if interpretation.goal == "after_sales" or interpretation.reference_type is not None:
        return False
    subject = interpretation.subject
    new_subject = any((subject.product_type, subject.brand, subject.model, subject.reference, subject.ean))
    return bool(
        interpretation.domain_change_explicit
        or (new_subject and not interpretation.references_previous_context)
    )


def _is_about_existing_order(interpretation: "SalesInterpretation | None") -> bool:
    if interpretation is None:
        return True  # no evidence of a new sale
    return bool(
        interpretation.goal == "after_sales"
        or interpretation.order_action
        or interpretation.payment_action == "order_payment"
        or interpretation.order_id
    )


def resolve_dialogue_phase(
    state: "CommerceConversationState | None",
    *,
    intent: "IntentResult | None" = None,
    interpretation: "SalesInterpretation | None" = None,
    message_text: str | None = None,
    handoff_active: bool = False,
) -> DialogueState:
    """Phase for this turn: the persisted stage, moved only by explicit evidence."""
    state_phase = phase_from_state(state)
    base: DialoguePhase = state_phase

    if interpretation is not None and interpretation.domain != "commerce":
        # A greeting or a store question mid-sale does not move the sale.
        return DialogueState(phase=base, state_phase=state_phase, handoff_active=handoff_active)

    if state_phase == "after_sale" and not _is_about_existing_order(interpretation):
        base = phase_from_state(state, ignore_order=True)  # rule 4: a new sale

    phase: DialoguePhase = base
    if base in _BROWSING_PHASES and (
        message_restarts_discovery(message_text) or _starts_independent_search(interpretation, intent)
    ):
        phase = "discovery"  # rule 2
    elif base == "after_sale" or base == "discovery":
        phase = base
    elif base == "shortlist" and interpretation is not None and interpretation.reference_type in _SELECTION_REFERENCES:
        phase = "product_focus"  # rule 1
    elif (
        base == "cart"
        and state is not None
        and interpretation is not None
        and interpretation.purchase_action == "remove_cart_item"
        and len(state.cart_items) <= 1
    ):
        # rule 3: reconsidering the only item steps the sale back.
        phase = "product_focus" if (state.active_product or state.last_presented_products) else "discovery"

    return DialogueState(phase=phase, state_phase=state_phase, handoff_active=handoff_active)
