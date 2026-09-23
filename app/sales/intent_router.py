"""Intent Router — single boundary for the sales intent of a turn.

Turns what the interpreter produced (``SalesInterpretation``) — or, without an
LLM, the message text — into the intent the sales pipeline acts on, and says
which commerce capability (if any) serves that intent. Taxonomy and origins:
``docs/intent_taxonomy.md``.

This module only CLASSIFIES. It never answers the customer, calls the commerce
provider, runs checkout, sends messages, writes memory, hands off, or touches
persona/tone/formatting — it has no such imports and must not grow them
(``tests/test_intent_router.py`` guards the import graph).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.product_vocabulary import mentions_product_category

if TYPE_CHECKING:
    from app.models import SalesInterpretation

#: Conversation domain (interpreter schema; deterministic fallback below).
Domain = Literal["commerce", "store_general", "greeting", "out_of_scope"]

#: What the sales pipeline does with a commerce turn.
SalesIntent = Literal[
    "clarification",  # ask before searching
    "product_search",  # find a product/category
    "recommendation",  # search to recommend within constraints
    "product_comparison",  # compare named products (served by search)
    "price",  # price / payment condition of a product
    "inventory",  # stock / availability of a product
    "coupon",  # coupon lookup
    "purchase_intent",  # wants to buy; keyword fallback only (no action by itself)
]

#: Commerce capability the pipeline calls for each intent (``commerce_router``).
CommerceAction = Literal["product_search", "product_price", "product_inventory", "coupon_search"]

COMMERCE_ACTION_BY_INTENT: dict[str, CommerceAction] = {
    "product_search": "product_search",
    "recommendation": "product_search",
    "product_comparison": "product_search",
    "price": "product_price",
    "inventory": "product_inventory",
    "coupon": "coupon_search",
}

#: Keyword capability -> sales intent (deterministic path).
_INTENT_BY_COMMERCE_ACTION: dict[str, SalesIntent] = {
    "product_search": "product_search",
    "product_price": "price",
    "product_inventory": "inventory",
    "coupon_search": "coupon",
}

#: Sales intent -> interpreter goal, for interpretations built without an LLM.
_GOAL_BY_INTENT: dict[str, str] = {
    "purchase_intent": "buy",
    "product_search": "find",
    "price": "inspect",
    "inventory": "inspect",
    "coupon": "inspect",
    "recommendation": "recommend",
    "product_comparison": "compare",
    "clarification": "discover",
}

_GREETINGS = frozenset(
    {"oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "oi tudo bem", "olá tudo bem", "ola tudo bem"}
)
_PURCHASE_TERMS = (
    "quero comprar", "quero adquirir", "quero um ", "quero uma ", "gostaria de comprar",
    "gostaria de um ", "procuro", "busco", "recomende",
)
_FACT_QUESTION_TERMS = ("quanto custa", "preço", "preco", "estoque", "disponibilidade")
_COMMERCE_PREFIXES = ("tem ", "vocês têm ", "voces tem ", "vende ")
_COMMERCE_TERMS = (
    "comprar", "adquirir", "quero ", "procuro", "busco", "orçamento", "orcamento", "comparar", "recomende",
)
_STORE_TERMS = ("xnamai", "loja", "pedido", "compra", "atendimento comercial", "catálogo", "catalogo")


@dataclass(frozen=True)
class IntentResult:
    """Routing decision for a commerce turn."""

    intent: SalesIntent
    #: The planner asked to clarify, but the customer asked to act.
    forced_retrieval: bool = False

    @property
    def commerce_action(self) -> CommerceAction | None:
        """``None`` means: no catalog call serves this intent."""
        return COMMERCE_ACTION_BY_INTENT.get(self.intent)


# --- deterministic (no LLM) ---------------------------------------------------


def is_greeting(text: str | None) -> bool:
    normalized = " ".join((text or "").lower().strip().split()).strip("!?.,")
    return normalized in _GREETINGS


def commerce_action_from_text(text: str | None) -> CommerceAction | None:
    """Keyword capability for a message (fallback and legacy commerce router)."""
    normalized = (text or "").lower()
    if any(term in normalized for term in ("cupom comercial", "cupom disponível", "cupom disponivel", "algum cupom")):
        return "coupon_search"
    if any(term in normalized for term in ("estoque", "disponibilidade", "disponível", "disponivel")):
        return "product_inventory"
    if any(term in normalized for term in ("pix", "parcelamento", "parcelar", "promocao", "promoção")):
        return "product_price"
    if any(term in normalized for term in ("quanto custa", "qual o preço", "qual o preco", "preço", "preco", "valor")):
        return "product_price"
    if mentions_product_category(text) or any(
        term in normalized
        for term in ("tem ", "vocês têm", "voces tem", "vende", "produto", "marca", "modelo", "sku", "ean")
    ):
        return "product_search"
    return None


def intent_from_text(text: str | None) -> SalesIntent | None:
    """Sales intent from keywords alone; ``None`` when nothing commercial is asked."""
    normalized = (text or "").lower()
    purchase = any(term in normalized for term in _PURCHASE_TERMS)
    if purchase and not any(term in normalized for term in _FACT_QUESTION_TERMS):
        return "purchase_intent"
    action = commerce_action_from_text(text)
    return _INTENT_BY_COMMERCE_ACTION.get(action) if action else None


def domain_from_text(text: str | None, *, commerce_inquiry: bool = False) -> Domain:
    """Conversation domain from keywords alone (used when the interpreter is unavailable).

    ``commerce_inquiry`` is the guardrail detector's verdict, passed in so this
    module does not depend on guardrails.
    """
    value = (text or "").strip()
    normalized = value.lower()
    if is_greeting(value):
        return "greeting"
    if (
        commerce_inquiry
        or normalized.startswith(_COMMERCE_PREFIXES)
        or any(term in normalized for term in _COMMERCE_TERMS)
    ):
        return "commerce"
    if any(term in normalized for term in _STORE_TERMS):
        return "store_general"
    return "out_of_scope"


def goal_for_intent(intent: str | None) -> str | None:
    return _GOAL_BY_INTENT.get(intent or "")


# --- account intents (served by deterministic flows, never by the LLM) ----------

#: Intents about the customer's own account, outside the sales taxonomy above.
AccountIntent = Literal["customer_registration"]
CUSTOMER_REGISTRATION: AccountIntent = "customer_registration"

_REGISTRATION_STEM = re.compile(r"\bcadastr\w*")
#: Wanting / asking / creating — any of these next to the stem makes it a request.
_REGISTRATION_ASK = re.compile(
    r"\b(?:quero|queria|quer|gostaria|preciso|precisava|posso|pode|podem|como|onde|da pra|consigo|"
    r"fazer|faco|faz|criar|crio|abrir|abro|realizar|efetuar|solicitar|iniciar|comecar|me)\b"
)
#: Not a new registration: negated, already has one, or wants to change an existing one.
_REGISTRATION_NOT_NEW = re.compile(
    r"\b(?:nao|nem|nunca|ja|atualiz\w*|desatualiz\w*|alter\w*|mud\w*|corrig\w*|edit\w*|exclu\w*|"
    r"apag\w*|remov\w*|cancel\w*|produto\w*|club|clube)\b"
)
_REGISTRATION_SHORT_TOKENS = 3


def _fold_text(text: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", (text or "").casefold())
    folded = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w@.\-]+", " ", folded).split())


def is_customer_registration_request(text: str | None) -> bool:
    """The customer asks to CREATE their own registration.

    Grammar instead of a phrase list: the ``cadastr-`` stem plus an asking /
    creating word ("gostaria de me cadastrar", "como faço meu cadastro?"), or
    the bare word on its own ("cadastro"). Negations, "já tenho", updates and
    Club subscriptions are not new registrations. Reads text only — no PII is
    extracted here.
    """
    folded = _fold_text(text)
    if not _REGISTRATION_STEM.search(folded) or _REGISTRATION_NOT_NEW.search(folded):
        return False
    return len(folded.split()) <= _REGISTRATION_SHORT_TOKENS or bool(_REGISTRATION_ASK.search(folded))


def account_intent_from_text(text: str | None) -> AccountIntent | None:
    return CUSTOMER_REGISTRATION if is_customer_registration_request(text) else None


# --- interpreter output -------------------------------------------------------


def intent_from_interpretation(interpretation: "SalesInterpretation") -> SalesIntent:
    """Translate the interpreter's structured output into the sales intent."""
    information_needed = set(interpretation.information_needed)
    inspect_intent: SalesIntent = (
        "inventory" if "inventory" in information_needed
        else "coupon" if "coupons" in information_needed
        else "price" if information_needed.intersection({"price", "payment"})
        else "product_search"
    )
    goal_to_intent: dict[str, SalesIntent] = {
        "discover": "clarification",
        "find": "product_search",
        "recommend": "recommendation",
        "compare": "product_comparison",
        "inspect": inspect_intent,
        "buy": "clarification",
        "after_sales": "clarification",
    }
    retrieval_signal = any((
        interpretation.enough_information_to_search,
        interpretation.ready_for_retrieval,
        interpretation.stop_clarification,
    ))
    if retrieval_signal and interpretation.goal in {"discover", "recommend", "buy"}:
        return "recommendation"
    if interpretation.needs_clarification:
        return "clarification"
    return goal_to_intent.get(interpretation.goal or "discover", "clarification")


# --- routing ------------------------------------------------------------------


def route_sales_intent(intent: str | None, *, force_retrieval: bool = False) -> IntentResult | None:
    """Final routing of a planned intent. ``None`` for an unknown intent."""
    if intent not in _GOAL_BY_INTENT:
        return None
    if force_retrieval and intent == "clarification":
        return IntentResult(intent="recommendation", forced_retrieval=True)
    return IntentResult(intent=intent)  # type: ignore[arg-type]
