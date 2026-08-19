"""Unified turn understanding (Etapa 3).

The model may interpret language and intent, but must not invent commercial
facts or internal product IDs. Downstream code still consumes
``SalesInterpretation`` via adapters until later phases cut over fully.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator

from .models import (
    CheckoutDataInput,
    ProductPreferences,
    ProductSubject,
    PurchaseItem,
    SalesInterpretation,
)

Intent = Literal[
    "greeting",
    "commerce_discover",
    "commerce_find",
    "commerce_recommend",
    "commerce_compare",
    "commerce_inspect",
    "commerce_buy",
    "commerce_after_sales",
    "raffle_info",
    "store_general",
    "out_of_scope",
    "human_handoff",
]

AnswerStrategy = Literal[
    "answer_directly",
    "search_catalog",
    "clarify",
    "handoff",
    "refuse",
    "acknowledge",
]

ToolName = Literal[
    "none",
    "search_products",
    "get_product",
    "get_stock",
    "get_price",
    "cart",
    "checkout",
    "order",
    "shipping",
    "payment",
]

AmbiguityKind = Literal[
    "missing_product",
    "missing_budget",
    "missing_reference",
    "ambiguous_reference",
    "conflicting_constraints",
    "underspecified_goal",
    "other",
]

ReferenceKind = Literal[
    "list_position",
    "current_product",
    "previous_recommendation",
    "last_presented_product",
    "explicit_product",
    "demonstrative",
]


class ExtractedEntities(BaseModel):
    brand: str | None = Field(default_factory=lambda: None)
    collection: str | None = Field(default_factory=lambda: None)
    model: str | None = Field(default_factory=lambda: None)
    reference: str | None = Field(default_factory=lambda: None)
    sku: str | None = Field(default_factory=lambda: None)
    ean: str | None = Field(default_factory=lambda: None)
    category: str | None = Field(default_factory=lambda: None)
    dial_color: str | None = Field(default_factory=lambda: None)
    strap_color: str | None = Field(default_factory=lambda: None)
    material: str | None = Field(default_factory=lambda: None)
    strap_type: str | None = Field(default_factory=lambda: None)
    mechanism: str | None = Field(default_factory=lambda: None)
    gender: str | None = Field(default_factory=lambda: None)
    case_size: str | None = Field(default_factory=lambda: None)
    budget_min: float | None = Field(default_factory=lambda: None)
    budget_max: float | None = Field(default_factory=lambda: None)
    quantity: int | None = Field(default_factory=lambda: None, ge=1)
    previously_mentioned_product: str | None = Field(default_factory=lambda: None)
    demonstrative_terms: list[str] = Field(default_factory=list)
    # Never trust model-supplied internal IDs — sanitized to None.
    claimed_product_id: str | None = Field(default_factory=lambda: None)
    claimed_variant_id: str | None = Field(default_factory=lambda: None)


class ProductHardConstraints(BaseModel):
    """Mandatory filters. Violations exclude candidates."""

    brand: str | None = Field(default_factory=lambda: None)
    brand_exclusive: bool = Field(default_factory=bool)
    model: str | None = Field(default_factory=lambda: None)
    reference: str | None = Field(default_factory=lambda: None)
    sku: str | None = Field(default_factory=lambda: None)
    ean: str | None = Field(default_factory=lambda: None)
    category: str | None = Field(default_factory=lambda: None)
    gender: str | None = Field(default_factory=lambda: None)
    dial_color: str | None = Field(default_factory=lambda: None)
    strap_color: str | None = Field(default_factory=lambda: None)
    material: str | None = Field(default_factory=lambda: None)
    mechanism: str | None = Field(default_factory=lambda: None)
    case_size: str | None = Field(default_factory=lambda: None)
    budget_min: float | None = Field(default_factory=lambda: None)
    budget_max: float | None = Field(default_factory=lambda: None)
    exact_only: bool = Field(default_factory=bool)
    must_match_fields: list[str] = Field(default_factory=list)


class ProductSoftPreferences(BaseModel):
    """Flexible ranking signals — never hard-exclude alone."""

    brand: str | None = Field(default_factory=lambda: None)
    model: str | None = Field(default_factory=lambda: None)
    color: str | None = Field(default_factory=lambda: None)
    style: str | None = Field(default_factory=lambda: None)
    material: str | None = Field(default_factory=lambda: None)
    occasion: str | None = Field(default_factory=lambda: None)
    recipient: str | None = Field(default_factory=lambda: None)
    mechanism: str | None = Field(default_factory=lambda: None)
    case_size: str | None = Field(default_factory=lambda: None)
    budget_min: float | None = Field(default_factory=lambda: None)
    budget_max: float | None = Field(default_factory=lambda: None)
    attributes: list[str] = Field(default_factory=list)
    explicit_no_preferences: list[
        Literal[
            "budget",
            "brand",
            "color",
            "style",
            "material",
            "occasion",
            "recipient",
            "attributes",
        ]
    ] = Field(default_factory=list)


class ConversationReference(BaseModel):
    kind: ReferenceKind | None = Field(default_factory=lambda: None)
    position: int | None = Field(default_factory=lambda: None, ge=1)
    surface_text: str | None = Field(default_factory=lambda: None)
    # Surface labels only — never internal catalog IDs from the model.
    product_label: str | None = Field(default_factory=lambda: None)


class Ambiguity(BaseModel):
    kind: AmbiguityKind = Field(default_factory=lambda: "other")
    field: str | None = Field(default_factory=lambda: None)
    blocking: bool = Field(default_factory=bool)
    detail: str | None = Field(default_factory=lambda: None)


class RequestedAction(BaseModel):
    kind: Literal[
        "none",
        "search",
        "recommend",
        "compare",
        "inspect",
        "create_cart",
        "show_cart_link",
        "checkout_question",
        "inspect_cart",
        "set_cart_item_quantity",
        "remove_cart_item",
        "get_product_link",
        "payment_options",
        "installment",
        "order_payment",
        "shipping_quote",
        "shipping_select",
        "shipping_list",
        "checkout_update",
        "checkout_prepare",
        "checkout_create",
        "get_order",
        "get_order_status",
        "get_order_tracking",
        "handoff",
    ] | None = Field(default_factory=lambda: None)
    quantity: int | None = Field(default_factory=lambda: None, ge=1)
    purchase_items: list[PurchaseItem] = Field(default_factory=list)
    image_request: bool = Field(default_factory=bool)
    payment_request_kind: Literal["informational", "checkout"] | None = Field(
        default_factory=lambda: None
    )
    payment_method_preference: Literal["pix", "card", "boleto", "other"] | None = Field(
        default_factory=lambda: None
    )
    checkout_channel_preference: Literal["whatsapp", "site"] | None = Field(
        default_factory=lambda: None
    )
    payment_option_id: str | None = Field(default_factory=lambda: None)
    shipping_zipcode: str | None = Field(default_factory=lambda: None)
    shipping_selection_id: str | None = Field(default_factory=lambda: None)
    shipping_selection_position: int | None = Field(default_factory=lambda: None, ge=1)
    checkout_data: CheckoutDataInput | None = Field(default_factory=lambda: None)
    order_id: str | None = Field(default_factory=lambda: None)
    confirmation: Literal["confirm", "reject", "none"] = Field(
        default_factory=lambda: "none"
    )
    installment_count: int | None = Field(default_factory=lambda: None, ge=1)


class TurnUnderstanding(BaseModel):
    language: str = Field(default_factory=lambda: "pt-BR")
    primary_intent: Intent
    user_goal: str = Field(default_factory=lambda: "")
    entities: ExtractedEntities = Field(default_factory=ExtractedEntities)
    references: list[ConversationReference] = Field(default_factory=list)
    hard_constraints: ProductHardConstraints = Field(
        default_factory=ProductHardConstraints
    )
    soft_preferences: ProductSoftPreferences = Field(
        default_factory=ProductSoftPreferences
    )
    hypotheses: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    requested_action: RequestedAction | None = Field(default_factory=lambda: None)
    required_tools: list[ToolName] = Field(default_factory=list)
    ambiguity: list[Ambiguity] = Field(default_factory=list)
    clarification_required: bool = Field(default_factory=bool)
    clarification_reason: str | None = Field(default_factory=lambda: None)
    clarification_question: str | None = Field(default_factory=lambda: None)
    answer_strategy: AnswerStrategy = Field(
        default_factory=lambda: "answer_directly"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    references_previous_context: bool = Field(default_factory=bool)
    domain_change_explicit: bool = Field(default_factory=bool)
    active_topic: str | None = Field(default_factory=lambda: None)
    purchase_stage: Literal[
        "discovery",
        "selection",
        "details",
        "payment_discussion",
        "cart_created",
        "checkout_channel_selection",
        "shipping",
        "checkout_ready",
        "after_sales",
        "awaiting_payment",
        "payment_confirmed",
    ] | None = Field(default_factory=lambda: None)

    _source: str = PrivateAttr(default="openai")
    _fallback_reason: str | None = PrivateAttr(default=None)
    _clear_pending_action: bool = PrivateAttr(default=False)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, number))


_INTERNAL_ID_RE = re.compile(
    r"^(?:prod_|product_|var_|variant_|sku_id_)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_EXCLUSIVE_MARKERS = (
    "somente",
    "apenas",
    "só ",
    "so ",
    "exatamente",
    "exclusivamente",
    "only",
    "exactly",
)


TURN_UNDERSTANDING_INSTRUCTIONS = """
Você interpreta o turno atual do atendimento da NewStore.
NÃO responda ao cliente. Preencha TurnUnderstanding.

Regras:
1. Separe restrições OBRIGATÓRIAS (hard_constraints) de preferências FLEXÍVEIS (soft_preferences).
2. Termos como "somente", "apenas", "exatamente" → hard_constraints.exact_only=true e
   brand_exclusive quando a marca for exclusiva.
3. Orçamento com "até"/"no máximo" → hard_constraints.budget_max (filtro). Preferência vaga
   de preço sem número → soft ou missing_data.
4. Gênero (feminino/masculino/unissex) → entities.gender e soft/hard conforme o tom;
   NUNCA use gênero como model ou style.
5. Nunca invente product_id, variant_id, preço, estoque ou URL. claimed_product_id e
   claimed_variant_id devem ser null (IDs internos só o sistema resolve).
6. Referências ("esse", "o segundo", "o preto", "o mais barato") → references[] e
   entities.demonstrative_terms. Não invente o produto.
7. clarification_required=true SOMENTE se a ambiguidade impedir resposta segura.
   "quero relógios Casio até R$ 500" → search_catalog, clarification_required=false.
   "quero esse" sem referência recuperável → clarify.
8. Sorteios: primary_intent=raffle_info (somente informativo; nunca participar/apostar).
9. hypotheses = palpites não confirmados; missing_data = dados ausentes úteis.
10. required_tools lista ferramentas necessárias (search_products, get_stock, …) ou [none].
11. confidence entre 0 e 1. language use pt-BR salvo evidência clara.
12. requested_action descreve a ação comercial pedida; kind=none se só busca/conversa.
"""


def looks_like_internal_id(value: str | None) -> bool:
    """True for UUID-like IDs. Bare digits (EAN/SKU) are not internal IDs."""
    text = (value or "").strip()
    if not text:
        return False
    if _INTERNAL_ID_RE.match(text):
        return True
    try:
        uuid.UUID(text)
        return True
    except ValueError:
        return False


def message_has_exclusive_marker(text: str | None) -> bool:
    folded = (text or "").casefold()
    return any(marker in folded for marker in _EXCLUSIVE_MARKERS)


def sanitize_turn_understanding(understanding: TurnUnderstanding) -> TurnUnderstanding:
    """Strip untrusted internal IDs and normalize exclusive markers."""
    entities = understanding.entities
    entities.claimed_product_id = None
    entities.claimed_variant_id = None
    for field_name in ("sku", "reference", "ean", "previously_mentioned_product"):
        value = getattr(entities, field_name)
        if isinstance(value, str) and looks_like_internal_id(value):
            # Keep EAN-looking digit strings (8–14) — drop UUID-like only.
            if field_name == "ean" and value.isdigit() and 8 <= len(value) <= 14:
                continue
            if field_name in {"sku", "reference"} and not _INTERNAL_ID_RE.match(value):
                try:
                    uuid.UUID(value)
                    setattr(entities, field_name, None)
                except ValueError:
                    pass
                continue
            if field_name == "previously_mentioned_product":
                setattr(entities, field_name, None)

    for ref in understanding.references:
        if looks_like_internal_id(ref.product_label):
            ref.product_label = None

    # Drop tool-required IDs that are not in the allowlist shape.
    cleaned_tools: list[ToolName] = []
    for tool in understanding.required_tools:
        if tool in {
            "none",
            "search_products",
            "get_product",
            "get_stock",
            "get_price",
            "cart",
            "checkout",
            "order",
            "shipping",
            "payment",
        }:
            cleaned_tools.append(tool)
    understanding.required_tools = cleaned_tools or ["none"]
    return understanding


def apply_clarification_policy(
    understanding: TurnUnderstanding,
    *,
    message_text: str | None = None,
    has_recoverable_reference: bool = False,
) -> TurnUnderstanding:
    """Deterministic clarification gate — do not over-ask."""
    text = (message_text or "").strip()
    hard = understanding.hard_constraints
    entities = understanding.entities
    blocking = [item for item in understanding.ambiguity if item.blocking]

    can_search = bool(
        hard.brand
        or hard.model
        or hard.reference
        or hard.sku
        or hard.ean
        or hard.category
        or entities.brand
        or entities.model
        or entities.reference
        or entities.ean
        or entities.category
        or (entities.budget_max is not None or hard.budget_max is not None)
        or (entities.gender and (entities.category or entities.brand or "relógio" in text.casefold() or "relogio" in text.casefold()))
    )

    demonstrative = bool(entities.demonstrative_terms) or any(
        term in text.casefold()
        for term in ("esse", "essa", "isso", "o segundo", "a segunda", "o primeiro", "o preto", "o mais barato")
    )
    has_ref = has_recoverable_reference or any(
        ref.kind in {
            "list_position",
            "current_product",
            "previous_recommendation",
            "last_presented_product",
            "explicit_product",
        }
        and (ref.position is not None or ref.product_label or ref.surface_text)
        for ref in understanding.references
    )

    if demonstrative and not has_ref and not can_search:
        understanding.clarification_required = True
        understanding.clarification_reason = (
            understanding.clarification_reason or "ambiguous_reference"
        )
        understanding.answer_strategy = "clarify"
        if not understanding.clarification_question:
            understanding.clarification_question = (
                "Qual produto você quer dizer? Pode me dizer a marca, o modelo "
                "ou a posição na lista?"
            )
        if not any(a.kind == "ambiguous_reference" for a in understanding.ambiguity):
            understanding.ambiguity.append(
                Ambiguity(
                    kind="ambiguous_reference",
                    field="references",
                    blocking=True,
                    detail="demonstrative_without_recoverable_target",
                )
            )
        return understanding

    if can_search and understanding.primary_intent.startswith("commerce_"):
        # Safe to act — suppress unnecessary clarification.
        understanding.clarification_required = False
        understanding.clarification_reason = None
        if understanding.answer_strategy == "clarify":
            understanding.answer_strategy = "search_catalog"
        # Keep non-blocking ambiguities only.
        understanding.ambiguity = [a for a in understanding.ambiguity if not a.blocking]
        return understanding

    if blocking and not can_search:
        understanding.clarification_required = True
        understanding.answer_strategy = "clarify"
    return understanding


def _intent_to_domain_goal(
    intent: Intent,
) -> tuple[str, str | None]:
    mapping: dict[str, tuple[str, str | None]] = {
        "greeting": ("greeting", None),
        "commerce_discover": ("commerce", "discover"),
        "commerce_find": ("commerce", "find"),
        "commerce_recommend": ("commerce", "recommend"),
        "commerce_compare": ("commerce", "compare"),
        "commerce_inspect": ("commerce", "inspect"),
        "commerce_buy": ("commerce", "buy"),
        "commerce_after_sales": ("commerce", "after_sales"),
        "raffle_info": ("raffle", None),
        "store_general": ("store_general", None),
        "out_of_scope": ("out_of_scope", None),
        "human_handoff": ("store_general", None),
    }
    return mapping.get(intent, ("commerce", "discover"))


def _goal_to_intent(domain: str, goal: str | None) -> Intent:
    if domain == "greeting":
        return "greeting"
    if domain == "raffle":
        return "raffle_info"
    if domain == "store_general":
        return "store_general"
    if domain == "out_of_scope":
        return "out_of_scope"
    goal_map: dict[str, Intent] = {
        "discover": "commerce_discover",
        "find": "commerce_find",
        "recommend": "commerce_recommend",
        "compare": "commerce_compare",
        "inspect": "commerce_inspect",
        "buy": "commerce_buy",
        "after_sales": "commerce_after_sales",
    }
    if goal in goal_map:
        return goal_map[goal]
    return "commerce_discover"


def turn_understanding_to_sales(
    understanding: TurnUnderstanding,
) -> SalesInterpretation:
    domain, goal = _intent_to_domain_goal(understanding.primary_intent)
    entities = understanding.entities
    hard = understanding.hard_constraints
    soft = understanding.soft_preferences
    action = understanding.requested_action or RequestedAction()

    subject = ProductSubject(
        product_type=hard.category or entities.category,
        brand=hard.brand or entities.brand or soft.brand,
        model=hard.model or entities.model or soft.model,
        reference=hard.reference or entities.reference,
        ean=hard.ean or entities.ean,
    )
    if not subject.product_type and (subject.brand or subject.model or hard.budget_max or entities.budget_max):
        subject.product_type = entities.category or "relógio"

    color = (
        hard.dial_color
        or hard.strap_color
        or entities.dial_color
        or entities.strap_color
        or soft.color
    )
    preferences = ProductPreferences(
        budget_min=hard.budget_min or entities.budget_min or soft.budget_min,
        budget_max=hard.budget_max or entities.budget_max or soft.budget_max,
        color=color,
        style=soft.style,
        material=hard.material or entities.material or soft.material,
        occasion=soft.occasion,
        recipient=hard.gender or entities.gender or soft.recipient,
        attributes=list(soft.attributes or []),
        explicit_no_preferences=list(soft.explicit_no_preferences or []),
    )
    gender = hard.gender or entities.gender or soft.recipient
    if gender and gender not in preferences.attributes:
        preferences.attributes.append(gender)
    if hard.exact_only and "somente" not in preferences.attributes:
        preferences.attributes.append("somente")
    if hard.brand_exclusive and hard.brand and f"somente:{hard.brand}" not in preferences.attributes:
        preferences.attributes.append(f"somente:{hard.brand}")

    primary_ref = next((ref for ref in understanding.references if ref.kind), None)
    purchase_action = None
    product_action = None
    payment_action = None
    shipping_action = None
    checkout_action = None
    order_action = None
    kind = action.kind
    if kind in {
        "create_cart",
        "show_cart_link",
        "checkout_question",
        "inspect_cart",
        "set_cart_item_quantity",
        "remove_cart_item",
    }:
        purchase_action = kind  # type: ignore[assignment]
    elif kind == "get_product_link":
        product_action = "get_product_link"
    elif kind in {"payment_options", "installment", "order_payment"}:
        payment_action = kind  # type: ignore[assignment]
    elif kind == "shipping_quote":
        shipping_action = "quote"
    elif kind == "shipping_select":
        shipping_action = "select"
    elif kind == "shipping_list":
        shipping_action = "list_methods"
    elif kind == "checkout_update":
        checkout_action = "update_data"
    elif kind == "checkout_prepare":
        checkout_action = "prepare_order"
    elif kind == "checkout_create":
        checkout_action = "create_order"
    elif kind in {"get_order", "get_order_status", "get_order_tracking"}:
        order_action = kind  # type: ignore[assignment]

    info_needed: list[Literal["catalog", "price", "inventory", "coupons", "payment"]] = []
    if "search_products" in understanding.required_tools or understanding.answer_strategy == "search_catalog":
        info_needed.append("catalog")
    if "get_price" in understanding.required_tools:
        info_needed.append("price")
    if "get_stock" in understanding.required_tools:
        info_needed.append("inventory")
    if "payment" in understanding.required_tools:
        info_needed.append("payment")

    enough = not understanding.clarification_required and (
        understanding.answer_strategy == "search_catalog"
        or bool(subject.brand or subject.model or subject.reference or subject.ean or preferences.budget_max)
    )
    ready = enough and understanding.answer_strategy in {"search_catalog", "answer_directly"}

    interpretation = SalesInterpretation(
        domain=domain,  # type: ignore[arg-type]
        goal=goal,  # type: ignore[arg-type]
        subject=subject,
        preferences=preferences,
        information_needed=info_needed,
        references_previous_context=understanding.references_previous_context,
        enough_information_to_search=enough,
        ready_for_retrieval=ready,
        stop_clarification=False,
        needs_clarification=understanding.clarification_required,
        clarification_question=understanding.clarification_question,
        reference_type=primary_ref.kind if primary_ref and primary_ref.kind in {
            "list_position",
            "current_product",
            "previous_recommendation",
            "last_presented_product",
            "explicit_product",
        } else None,
        reference_position=primary_ref.position if primary_ref else None,
        purchase_action=purchase_action,
        quantity=action.quantity or entities.quantity,
        purchase_items=list(action.purchase_items or []),
        image_request=bool(action.image_request),
        product_action=product_action,
        payment_action=payment_action,
        payment_request_kind=action.payment_request_kind,
        payment_method_preference=action.payment_method_preference,
        checkout_channel_preference=action.checkout_channel_preference,
        payment_option_id=action.payment_option_id,
        shipping_action=shipping_action,
        shipping_zipcode=action.shipping_zipcode,
        shipping_selection_id=action.shipping_selection_id,
        shipping_selection_position=action.shipping_selection_position,
        checkout_action=checkout_action,
        checkout_data=action.checkout_data,
        order_action=order_action,
        order_id=action.order_id,
        confirmation=action.confirmation or "none",
        installment_count=action.installment_count,
        active_topic=understanding.active_topic or understanding.user_goal or None,
        purchase_stage=understanding.purchase_stage,
        domain_change_explicit=understanding.domain_change_explicit,
        confidence=understanding.confidence,
    )
    interpretation._source = understanding._source
    interpretation._fallback_reason = understanding._fallback_reason
    interpretation._clear_pending_action = understanding._clear_pending_action
    interpretation._turn_understanding = understanding
    return interpretation


def sales_to_turn_understanding(
    interpretation: SalesInterpretation,
    *,
    message_text: str | None = None,
) -> TurnUnderstanding:
    """Compatibility adapter for legacy SalesInterpretation → TurnUnderstanding."""
    subject = interpretation.subject
    prefs = interpretation.preferences
    exclusive = message_has_exclusive_marker(message_text) or any(
        str(item).startswith("somente") for item in (prefs.attributes or [])
    )
    hard = ProductHardConstraints(
        brand=subject.brand,
        brand_exclusive=exclusive and bool(subject.brand),
        model=subject.model if interpretation.goal in {"find", "inspect", "buy"} else None,
        reference=subject.reference,
        ean=subject.ean,
        category=subject.product_type,
        gender=prefs.recipient if prefs.recipient in {"feminino", "masculino", "unissex"} else None,
        dial_color=prefs.color if exclusive else None,
        material=prefs.material if exclusive else None,
        budget_min=prefs.budget_min,
        budget_max=prefs.budget_max,
        exact_only=exclusive,
        must_match_fields=[
            name
            for name, value in (
                ("brand", subject.brand if exclusive else None),
                ("reference", subject.reference),
                ("ean", subject.ean),
                ("budget_max", prefs.budget_max),
            )
            if value is not None
        ],
    )
    soft = ProductSoftPreferences(
        brand=None if exclusive else subject.brand,
        model=subject.model if interpretation.goal in {"discover", "recommend"} else None,
        color=prefs.color if not exclusive else None,
        style=prefs.style,
        material=prefs.material if not exclusive else None,
        occasion=prefs.occasion,
        recipient=prefs.recipient,
        budget_min=None,
        budget_max=None,
        attributes=list(prefs.attributes or []),
        explicit_no_preferences=list(prefs.explicit_no_preferences or []),
    )
    entities = ExtractedEntities(
        brand=subject.brand,
        model=subject.model,
        reference=subject.reference,
        ean=subject.ean,
        category=subject.product_type,
        dial_color=prefs.color,
        material=prefs.material,
        gender=prefs.recipient if prefs.recipient in {"feminino", "masculino", "unissex"} else None,
        budget_min=prefs.budget_min,
        budget_max=prefs.budget_max,
        quantity=interpretation.quantity,
        claimed_product_id=None,
        claimed_variant_id=None,
    )
    references: list[ConversationReference] = []
    if interpretation.reference_type or interpretation.reference_position:
        references.append(
            ConversationReference(
                kind=interpretation.reference_type,
                position=interpretation.reference_position,
            )
        )

    action_kind: str | None = "none"
    if interpretation.purchase_action:
        action_kind = interpretation.purchase_action
    elif interpretation.product_action:
        action_kind = interpretation.product_action
    elif interpretation.payment_action:
        action_kind = interpretation.payment_action
    elif interpretation.shipping_action == "quote":
        action_kind = "shipping_quote"
    elif interpretation.shipping_action == "select":
        action_kind = "shipping_select"
    elif interpretation.shipping_action == "list_methods":
        action_kind = "shipping_list"
    elif interpretation.checkout_action == "update_data":
        action_kind = "checkout_update"
    elif interpretation.checkout_action == "prepare_order":
        action_kind = "checkout_prepare"
    elif interpretation.checkout_action == "create_order":
        action_kind = "checkout_create"
    elif interpretation.order_action:
        action_kind = interpretation.order_action

    tools: list[ToolName] = []
    for needed in interpretation.information_needed:
        if needed == "catalog":
            tools.append("search_products")
        elif needed == "price":
            tools.append("get_price")
        elif needed == "inventory":
            tools.append("get_stock")
        elif needed == "payment":
            tools.append("payment")
    if not tools:
        tools = ["none"]

    if interpretation.needs_clarification:
        strategy: AnswerStrategy = "clarify"
    elif interpretation.ready_for_retrieval or interpretation.enough_information_to_search:
        strategy = "search_catalog"
    elif interpretation.domain == "greeting":
        strategy = "acknowledge"
    else:
        strategy = "answer_directly"

    ambiguity: list[Ambiguity] = []
    if interpretation.needs_clarification:
        ambiguity.append(
            Ambiguity(
                kind="underspecified_goal",
                blocking=True,
                detail=interpretation.clarification_question,
            )
        )

    understanding = TurnUnderstanding(
        language="pt-BR",
        primary_intent=_goal_to_intent(interpretation.domain, interpretation.goal),
        user_goal=interpretation.active_topic or "",
        entities=entities,
        references=references,
        hard_constraints=hard,
        soft_preferences=soft,
        requested_action=RequestedAction(
            kind=action_kind,  # type: ignore[arg-type]
            quantity=interpretation.quantity,
            purchase_items=list(interpretation.purchase_items or []),
            image_request=bool(interpretation.image_request),
            payment_request_kind=interpretation.payment_request_kind,
            payment_method_preference=interpretation.payment_method_preference,
            checkout_channel_preference=interpretation.checkout_channel_preference,
            payment_option_id=interpretation.payment_option_id,
            shipping_zipcode=interpretation.shipping_zipcode,
            shipping_selection_id=interpretation.shipping_selection_id,
            shipping_selection_position=interpretation.shipping_selection_position,
            checkout_data=interpretation.checkout_data,
            order_id=interpretation.order_id,
            confirmation=interpretation.confirmation or "none",
            installment_count=interpretation.installment_count,
        ),
        required_tools=tools,
        ambiguity=ambiguity,
        clarification_required=bool(interpretation.needs_clarification),
        clarification_reason="legacy_needs_clarification" if interpretation.needs_clarification else None,
        clarification_question=interpretation.clarification_question,
        answer_strategy=strategy,
        confidence=interpretation.confidence,
        references_previous_context=interpretation.references_previous_context,
        domain_change_explicit=interpretation.domain_change_explicit,
        active_topic=interpretation.active_topic,
        purchase_stage=interpretation.purchase_stage,
    )
    understanding._source = interpretation._source
    understanding._fallback_reason = interpretation._fallback_reason
    understanding._clear_pending_action = interpretation._clear_pending_action
    return understanding


def get_turn_understanding(
    interpretation: SalesInterpretation,
) -> TurnUnderstanding | None:
    stored = getattr(interpretation, "_turn_understanding", None)
    if isinstance(stored, TurnUnderstanding):
        return stored
    return None


def attach_turn_understanding(
    interpretation: SalesInterpretation,
    understanding: TurnUnderstanding,
) -> SalesInterpretation:
    interpretation._turn_understanding = understanding  # type: ignore[attr-defined]
    return interpretation
