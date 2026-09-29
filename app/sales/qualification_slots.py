"""Qualification Slots — "o que já sabemos para avançar?".

Describes what is known about the customer's request. It does NOT decide which
question to ask or whether to search (that stays with the clarification gates
in ``sales_agent`` until the Scope Send Gate exists), and never touches
persona, commerce, memory storage or outbound.

Slots are the ones the XNamai interpreter already produces
(``SalesInterpretation``); none is mandatory. Statuses mean different things:

``UNKNOWN``        not said yet (absence is never refusal)
``KNOWN``          a value was given
``REFUSED``        the customer explicitly declined to constrain it — today this
                   is ``explicit_no_preferences`` ("tanto faz a cor", "sem
                   preferência de marca"): an answer that means "don't ask again"
``NOT_APPLICABLE`` the slot makes no sense in this flow (a non-commerce turn;
                   preference slots on an after-sale turn)

Precedence (deterministic, highest first): this turn stated explicitly >
this turn carried from context > the conversation state of earlier turns >
customer memory. An explicit value in this turn replaces an older one ("até
500" beats a remembered "até 1000"), and an explicit negation of an older value
("não quero MarcaB") drops it instead of letting it resurface.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import SalesInterpretation


class SlotStatus(str, Enum):
    UNKNOWN = "unknown"
    KNOWN = "known"
    NOT_APPLICABLE = "not_applicable"
    REFUSED = "refused"


class SlotSource(str, Enum):
    USER_EXPLICIT = "user_explicit"  # said in this message
    USER_INFERRED = "user_inferred"  # this turn, but carried from the context by the interpreter
    CONVERSATION_STATE = "conversation_state"  # persisted by an earlier turn
    MEMORY = "memory"  # long-lived customer memory

    @property
    def rank(self) -> int:
        return _SOURCE_RANK[self]


_SOURCE_RANK = {
    SlotSource.USER_EXPLICIT: 4,
    SlotSource.USER_INFERRED: 3,
    SlotSource.CONVERSATION_STATE: 2,
    SlotSource.MEMORY: 1,
}


@dataclass(frozen=True)
class Slot:
    status: SlotStatus = SlotStatus.UNKNOWN
    value: Any = None
    source: SlotSource | None = None


_UNKNOWN = Slot()
_NOT_APPLICABLE = Slot(status=SlotStatus.NOT_APPLICABLE)


@dataclass(frozen=True)
class QualificationState:
    category: Slot = _UNKNOWN  # product_type
    product: Slot = _UNKNOWN  # a specific item: reference or EAN
    brand: Slot = _UNKNOWN
    model: Slot = _UNKNOWN
    budget: Slot = _UNKNOWN  # {"min": ..., "max": ...}
    quantity: Slot = _UNKNOWN
    color: Slot = _UNKNOWN
    style: Slot = _UNKNOWN
    material: Slot = _UNKNOWN
    occasion: Slot = _UNKNOWN
    recipient: Slot = _UNKNOWN
    attributes: Slot = _UNKNOWN  # characteristics: connector, power, bluetooth...
    payment_method: Slot = _UNKNOWN

    def slot(self, name: str) -> Slot:
        return getattr(self, name)

    def names_with(self, status: SlotStatus) -> list[str]:
        return [name for name in SLOT_NAMES if self.slot(name).status is status]

    def summary(self) -> dict[str, Any]:
        """Compact, PII-free view for observability (status + source only)."""
        return {
            name: {"status": slot.status.value, "source": slot.source.value if slot.source else None}
            for name in SLOT_NAMES
            if (slot := self.slot(name)).status is not SlotStatus.UNKNOWN
        }


SLOT_NAMES: tuple[str, ...] = tuple(QualificationState.__dataclass_fields__)

#: Canonical names the interpreter uses in ``explicit_no_preferences``.
_REFUSABLE = ("budget", "brand", "color", "style", "material", "occasion", "recipient", "attributes")
#: Order ``known_preferences`` has always had in DISCOVERY_STATE (prompt parity).
_PREFERENCE_ORDER = ("budget", "color", "style", "material", "occasion", "recipient", "brand", "attributes")
#: What still matters on an after-sale turn.
_AFTER_SALE_SLOTS = frozenset({"product", "brand", "model", "quantity", "payment_method"})

_CORRECTION_RE = re.compile(
    r"\b(na verdade|quis dizer|quero dizer|mudei de ideia|corrigindo|me enganei|nao,? (?:e|era|eh))\b"
)
_NEGATION_TEMPLATE = r"\b(nao quero|nao gosto de|sem|nada de|menos|exceto|tirando)\s+(?:a |o |da |do |de )?{value}\b"


def _fold(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).strip()


def is_explicit_correction(message_text: str | None) -> bool:
    """"na verdade...", "quis dizer...", "mudei de ideia..." — the customer is correcting."""
    return bool(_CORRECTION_RE.search(_fold(message_text)))


def explicitly_negates(message_text: str | None, value: Any) -> bool:
    """True when this message rejects ``value`` ("não quero MarcaB", "sem preto")."""
    folded_value = _fold(value)
    if not folded_value or isinstance(value, (dict, list)):
        return False
    pattern = _NEGATION_TEMPLATE.format(value=re.escape(folded_value))
    return bool(re.search(pattern, _fold(message_text)))


def _known(value: Any, source: SlotSource) -> Slot:
    return Slot(status=SlotStatus.KNOWN, value=value, source=source)


def slots_from_interpretation(
    interpretation: "SalesInterpretation",
    *,
    message_text: str | None = None,
) -> QualificationState:
    """What THIS turn says (interpreter output), before flow applicability."""
    carried = interpretation.references_previous_context and not is_explicit_correction(message_text)
    source = SlotSource.USER_INFERRED if carried else SlotSource.USER_EXPLICIT
    subject = interpretation.subject
    preferences = interpretation.preferences
    values: dict[str, Slot] = {}

    if subject.product_type:
        values["category"] = _known(subject.product_type, source)
    if subject.reference or subject.ean:
        values["product"] = _known(subject.reference or subject.ean, source)
    if subject.brand:
        values["brand"] = _known(subject.brand, source)
    if subject.model:
        values["model"] = _known(subject.model, source)
    if preferences.budget_min is not None or preferences.budget_max is not None:
        values["budget"] = _known({"min": preferences.budget_min, "max": preferences.budget_max}, source)
    if interpretation.quantity is not None:
        values["quantity"] = _known(interpretation.quantity, source)
    for name in ("color", "style", "material", "occasion", "recipient"):
        value = getattr(preferences, name)
        if value:
            values[name] = _known(value, source)
    if preferences.attributes:
        values["attributes"] = _known(list(preferences.attributes), source)
    if interpretation.payment_method_preference:
        values["payment_method"] = _known(interpretation.payment_method_preference, source)

    # An explicit value wins over a refusal of the same slot in the same turn.
    for name in dict.fromkeys(preferences.explicit_no_preferences):
        if name in _REFUSABLE and name not in values:
            values[name] = Slot(status=SlotStatus.REFUSED, source=source)

    return QualificationState(**values)


def apply_flow_applicability(
    state: QualificationState,
    interpretation: "SalesInterpretation",
) -> QualificationState:
    """Mark slots that make no sense in this flow as NOT_APPLICABLE."""
    if interpretation.domain != "commerce":
        return QualificationState(**{name: _NOT_APPLICABLE for name in SLOT_NAMES})
    if interpretation.goal == "after_sales":
        return replace(state, **{name: _NOT_APPLICABLE for name in SLOT_NAMES if name not in _AFTER_SALE_SLOTS})
    return state


def slots_from_preferences(preferences: dict[str, Any] | None, *, source: SlotSource) -> QualificationState:
    """Older knowledge: ``CommerceConversationState.active_preferences`` or customer memory.

    Accepts the shape the pipeline persists (``ProductPreferences`` dump) plus
    ``brand``/``product_type``/``model`` when present.
    """
    prefs = preferences if isinstance(preferences, dict) else {}
    values: dict[str, Slot] = {}
    if prefs.get("budget_min") is not None or prefs.get("budget_max") is not None:
        values["budget"] = _known({"min": prefs.get("budget_min"), "max": prefs.get("budget_max")}, source)
    for name in ("color", "style", "material", "occasion", "recipient", "brand", "model"):
        if prefs.get(name):
            values[name] = _known(prefs[name], source)
    if prefs.get("product_type"):
        values["category"] = _known(prefs["product_type"], source)
    if prefs.get("attributes"):
        values["attributes"] = _known(list(prefs["attributes"]), source)
    for name in dict.fromkeys(prefs.get("explicit_no_preferences") or []):
        if name in _REFUSABLE and name not in values:
            values[name] = Slot(status=SlotStatus.REFUSED, source=source)
    return QualificationState(**values)


def merge_qualification(
    current: QualificationState,
    *older: QualificationState,
    message_text: str | None = None,
) -> QualificationState:
    """Apply the precedence rules; ``current`` is this turn."""
    merged: dict[str, Slot] = {}
    for name in SLOT_NAMES:
        slot = current.slot(name)
        if slot.status is not SlotStatus.UNKNOWN:
            merged[name] = slot  # this turn decided (known, refused or not applicable)
            continue
        candidates = [
            previous.slot(name)
            for previous in older
            if previous.slot(name).status in (SlotStatus.KNOWN, SlotStatus.REFUSED)
        ]
        candidates.sort(key=lambda item: item.source.rank if item.source else 0, reverse=True)
        chosen = candidates[0] if candidates else _UNKNOWN
        if chosen.status is SlotStatus.KNOWN and explicitly_negates(message_text, chosen.value):
            chosen = Slot(status=SlotStatus.UNKNOWN, source=SlotSource.USER_EXPLICIT)  # "não quero X"
        merged[name] = chosen
    return replace(current, **merged)


def build_qualification_state(
    interpretation: "SalesInterpretation",
    *,
    message_text: str | None = None,
    state_preferences: dict[str, Any] | None = None,
    memory_preferences: dict[str, Any] | None = None,
) -> QualificationState:
    """This turn + what earlier turns and memory knew, under the precedence rules."""
    current = slots_from_interpretation(interpretation, message_text=message_text)
    older = [slots_from_preferences(state_preferences, source=SlotSource.CONVERSATION_STATE)]
    if memory_preferences:
        older.append(slots_from_preferences(memory_preferences, source=SlotSource.MEMORY))
    merged = merge_qualification(current, *older, message_text=message_text)
    return apply_flow_applicability(merged, interpretation)


# --- views the existing discovery flow consumes -------------------------------


def known_preferences(state: QualificationState) -> dict[str, Any]:
    """``known_preferences`` exactly as DISCOVERY_STATE has always shown it."""
    known: dict[str, Any] = {}
    for name in _PREFERENCE_ORDER:
        slot = state.slot(name)
        if slot.status is SlotStatus.KNOWN:
            known[name] = slot.value
    return known


def subject_identifiable(state: QualificationState) -> bool:
    """A product or category can be searched for."""
    return any(
        state.slot(name).status is SlotStatus.KNOWN for name in ("category", "brand", "model", "product")
    )


__all__ = [
    "QualificationState",
    "apply_flow_applicability",
    "SLOT_NAMES",
    "Slot",
    "SlotSource",
    "SlotStatus",
    "build_qualification_state",
    "explicitly_negates",
    "is_explicit_correction",
    "known_preferences",
    "merge_qualification",
    "slots_from_interpretation",
    "slots_from_preferences",
    "subject_identifiable",
]
