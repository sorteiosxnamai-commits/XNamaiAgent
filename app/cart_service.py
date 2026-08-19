from __future__ import annotations

import secrets
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .commerce_context import (
    CommerceCartItem,
    CommerceConversationState,
    CommerceProductReference,
    normalize_variant_identity,
)
from .checkout_service import checkout_capabilities
from .models import AgentResult, SalesInterpretation
from .product_retrieval import (
    commercial_availability_facts,
    product_availability_state,
    resolve_commercial_price,
)


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class CartItemRequest:
    product_reference: CommerceProductReference
    quantity: int = 1
    position: int | None = None
    resolved_from: str = "context"
    variant_preferences: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _PreparedCartItem:
    product_reference: CommerceProductReference
    product: dict[str, Any]
    variant: dict[str, Any] | None
    quantity: int
    price: str
    original_price: str | None
    position: int | None
    resolved_from: str


def log_purchase_progress(
    stage: str,
    status: str,
    blocking_reason: str | None = None,
) -> None:
    payload = {
        "stage": stage,
        "status": status,
    }
    if blocking_reason is not None:
        payload["blocking_reason"] = blocking_reason
    print("[sales.purchase.progress]", payload)


def _technical_failure(
    *,
    stage: str,
    status_code: int | None = None,
    exception_type: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> AgentResult:
    diagnostics = diagnostics or {}
    diagnostic_log = {
        key: diagnostics.get(key)
        for key in (
            "tray_error_code",
            "tray_error_type",
            "tray_error_field",
            "tray_error_fields",
            "tray_error_message",
        )
        if diagnostics.get(key) not in (None, "", [])
    }
    log_purchase_progress(
        stage,
        "failed",
        exception_type or "cart_technical_failure",
    )
    print("[sales.cart.failure]", {
        "stage": stage,
        "exception_type": exception_type or "upstream_error",
        "upstream_status": status_code,
        **diagnostic_log,
    })
    print("[sales.cart.error]", {
        "error_type": "cart_technical_failure",
        "status_code": status_code,
    })
    response_metadata = {
        "used_tray": True,
        "cart_failure_stage": stage,
    }
    if status_code is not None:
        response_metadata["cart_failure_status"] = status_code
    for metadata_key, diagnostic_key in (
        ("cart_failure_code", "tray_error_code"),
        ("cart_failure_type", "tray_error_type"),
        ("cart_failure_field", "tray_error_field"),
        ("cart_failure_fields", "tray_error_fields"),
    ):
        if diagnostics.get(diagnostic_key) not in (None, "", []):
            response_metadata[metadata_key] = diagnostics[diagnostic_key]
    return AgentResult(
        reply_text="",
        intent="commerce",
        handoff_required=False,
        safety_reason="cart_technical_failure",
        commercial_data={
            "cart": {
                "cart_created": False,
                "failure_stage": stage,
                "recoverable": _is_transient_cart_failure(
                    status_code=status_code,
                    error_type=exception_type,
                ),
                "status_code": status_code,
            },
            "technical_failure": {
                "operation": "cart",
                "category": "integration_failure",
                "retryable": _is_transient_cart_failure(
                    status_code=status_code,
                    error_type=exception_type,
                ),
            },
        },
        response_metadata=response_metadata,
    )


def _cart_session_state(
    state: CommerceConversationState,
    session_id: str,
) -> dict[str, Any]:
    return {
        "cart_id": state.cart_id,
        "cart_session_id": session_id,
        "cart_url": state.cart_url,
        "cart_product_id": state.cart_product_id,
        "cart_variant_id": state.cart_variant_id,
        "cart_quantity": state.cart_quantity,
        "cart_items": [_cart_item_state(item) for item in state.cart_items],
    }


def _cart_item_state(item: CommerceCartItem) -> dict[str, Any]:
    """Keep a factual item name when Tray supplied one without changing old payloads."""
    payload = item.model_dump(mode="json")
    if payload.get("name") is None:
        payload.pop("name", None)
    return payload


def _persist_cart_session(
    result: AgentResult,
    state: CommerceConversationState,
    session_id: str | None,
) -> AgentResult:
    if not session_id:
        return result
    result.response_metadata.setdefault("domain", "commerce")
    current = result.response_metadata.get("cart_state")
    current = current if isinstance(current, dict) else {}
    result.response_metadata["cart_state"] = {
        **_cart_session_state(state, session_id),
        **current,
    }
    return result


def _validation_failure(
    message: str,
    reason: str = "cart_validation_error",
) -> AgentResult:
    print("[sales.cart.error]", {
        "error_type": reason,
        "status_code": None,
    })
    return AgentResult(
        reply_text=message,
        intent="commerce",
        handoff_required=False,
        safety_reason=reason,
    )


def _valid_cart_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _variant_id(variant: dict[str, Any]) -> str | None:
    value = variant.get("variant_id")
    if value is None:
        value = variant.get("id")
    return normalize_variant_identity(value)


def _flag_is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return _fold_choice(value) in {"1", "true", "yes", "sim"}
    return False


def _with_selected_product(
    result: AgentResult,
    product_reference: CommerceProductReference,
) -> AgentResult:
    result.response_metadata.setdefault("domain", "commerce")
    result.response_metadata.setdefault(
        "active_product",
        product_reference.model_dump(mode="json"),
    )
    result.response_metadata.setdefault("purchase_stage", "selection")
    if result.safety_reason == "variant_required":
        result.response_metadata.setdefault("pending_action", "create_cart")
        result.response_metadata.setdefault(
            "pending_action_product_ids",
            [product_reference.product_id],
        )
        print("[sales.pending_action]", {
            "action": "create_cart",
            "has_product": True,
            "confirmation": "none",
            "executed": False,
        })
    return result


_VARIANT_NON_CHOICE_FIELDS = {
    "id",
    "variant_id",
    "product_id",
    "reference",
    "sku",
    "price",
    "promotional_price",
    "current_price",
    "stock",
    "available",
    "available_in_store",
    "available_for_purchase",
    "availability",
    "variationsettings",
    "primary_image_url",
    "primary_image",
    "image_url",
    "image",
    "images",
}


def _choice_scalars(value: Any, *, prefix: str = "") -> dict[str, str]:
    choices: dict[str, str] = {}
    if isinstance(value, dict):
        label = value.get("name") or value.get("label") or value.get("key")
        selected = value.get("value")
        if label is not None and selected not in (None, "", [], {}):
            choices[str(label)] = str(selected)
        for key, item in value.items():
            if key in {"name", "label", "key", "value"}:
                continue
            if str(key).lower() in _VARIANT_NON_CHOICE_FIELDS:
                continue
            nested_prefix = f"{prefix}.{key}" if prefix else str(key)
            choices.update(_choice_scalars(item, prefix=nested_prefix))
        return choices
    if isinstance(value, list):
        for index, item in enumerate(value):
            choices.update(_choice_scalars(item, prefix=f"{prefix}.{index}"))
        return choices
    if value not in (None, "") and prefix:
        choices[prefix] = str(value)
    return choices


def variant_choices(variant: dict[str, Any]) -> dict[str, str]:
    choices: dict[str, str] = {}
    name = variant.get("name")
    value = variant.get("value")
    if name not in (None, "") and value not in (None, ""):
        choices[str(name)] = str(value)
    for key, item in variant.items():
        normalized_key = str(key).lower()
        if normalized_key in _VARIANT_NON_CHOICE_FIELDS:
            if normalized_key == "sku" and isinstance(item, (dict, list)):
                choices.update(_choice_scalars(item, prefix=str(key)))
            continue
        if key in {"name", "value"} and name not in (None, "") and value not in (None, ""):
            continue
        if isinstance(item, (dict, list)):
            choices.update(_choice_scalars(item, prefix=str(key)))
        elif item not in (None, "", True, False):
            choices[str(key)] = str(item)
    return choices


def _fold_choice(value: Any) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(char)
    ).strip()


def _choice_signature(variant: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(
        (
            _fold_choice(key),
            _fold_choice(value),
        )
        for key, value in variant_choices(variant).items()
        if str(value).strip()
    ))


def _preference_values(preferences: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in preferences.values():
        if isinstance(value, list):
            values.extend(
                _fold_choice(item)
                for item in value
                if str(item).strip()
            )
        elif value not in (None, "", {}, False):
            values.append(_fold_choice(value))
    return values


async def _resolve_variant(
    product: dict[str, Any],
    product_reference: CommerceProductReference,
    preferences: dict[str, Any],
    execute: ToolExecutor,
) -> tuple[dict[str, Any] | None, AgentResult | None]:
    log_purchase_progress("variant_resolution", "start")
    selected_id = product_reference.variant_id
    requires_variation = _flag_is_true(product.get("has_variation")) or selected_id is not None
    if not requires_variation:
        print("[sales.variant.resolve]", {
            "variant_count": 0,
            "eligible_count": 0,
            "distinct_choice_count": 0,
            "choice_required": False,
            "auto_selected": False,
            "has_variant_id": False,
        })
        log_purchase_progress("variant_resolution", "success")
        return None, None

    try:
        result = await execute(
            "list_product_variants",
            {"product_id": product_reference.product_id},
        )
    except Exception as exc:
        return None, _technical_failure(
            stage="variant_resolution",
            exception_type=type(exc).__name__,
        )
    if "error" in result:
        return None, _technical_failure(
            stage="variant_resolution",
            status_code=result.get("status_code"),
            exception_type=result.get("error_type"),
            diagnostics=result,
        )
    variants = [
        variant
        for variant in result.get("variants", [])
        if isinstance(variant, dict) and _variant_id(variant)
    ]

    if selected_id is not None:
        selected = next(
            (
                variant
                for variant in variants
                if _variant_id(variant) == str(selected_id)
            ),
            None,
        )
        if selected is None:
            log_purchase_progress(
                "variant_resolution",
                "blocked",
                "variant_required",
            )
            return None, _validation_failure(
                "Não consegui validar a variação escolhida. Escolha uma das opções disponíveis.",
                "variant_required",
            )
        print("[sales.variant.resolve]", {
            "variant_count": len(variants),
            "eligible_count": len(variants),
            "distinct_choice_count": len({_choice_signature(variant) for variant in variants}),
            "choice_required": False,
            "auto_selected": False,
            "has_variant_id": True,
        })
        log_purchase_progress("variant_resolution", "success")
        return selected, None

    eligible = [
        variant
        for variant in variants
        if product_availability_state(variant) != "unavailable"
    ]
    signatures = {_choice_signature(variant) for variant in eligible}
    selected: dict[str, Any] | None = None
    choice_required = False
    if not variants:
        selected = None
    elif not eligible:
        print("[sales.variant.resolve]", {
            "variant_count": len(variants),
            "eligible_count": 0,
            "distinct_choice_count": 0,
            "choice_required": False,
            "auto_selected": False,
            "has_variant_id": False,
        })
        log_purchase_progress(
            "variant_resolution",
            "blocked",
            "product_unavailable",
        )
        return None, _validation_failure(
            "As variações deste produto estão indisponíveis no momento.",
            "product_unavailable",
        )
    elif len(eligible) == 1 or len(signatures) <= 1:
        selected = eligible[0]
    else:
        preference_values = _preference_values(preferences)
        matched = [
            variant
            for variant in eligible
            if any(
                preference in " ".join(
                    f"{key} {value}"
                    for key, value in _choice_signature(variant)
                )
                for preference in preference_values
            )
        ]
        matched_signatures = {_choice_signature(variant) for variant in matched}
        if matched and len(matched_signatures) == 1:
            selected = matched[0]
        else:
            choice_required = True
    print("[sales.variant.resolve]", {
        "variant_count": len(variants),
        "eligible_count": len(eligible),
        "distinct_choice_count": len(signatures),
        "choice_required": choice_required,
        "auto_selected": selected is not None,
        "has_variant_id": bool(selected and _variant_id(selected)),
    })
    if not choice_required:
        log_purchase_progress("variant_resolution", "success")
        return selected, None
    choice_labels = [
        " / ".join(
            f"{key}: {value}"
            for key, value in variant_choices(variant).items()
        )
        for variant in eligible[:10]
    ]
    variant_reply = "Preciso confirmar uma das opções reais deste produto."
    if any(choice_labels):
        variant_reply += "\n" + "\n".join(
            f"{index}. {label}"
            for index, label in enumerate(choice_labels, start=1)
            if label
        )
    log_purchase_progress(
        "variant_resolution",
        "blocked",
        "variant_required",
    )
    return None, AgentResult(
        reply_text=variant_reply,
        intent="commerce",
        handoff_required=False,
        safety_reason="variant_required",
        commercial_data={
            "cart": {"status": "variant_required"},
            "variants": [
                {
                    **variant,
                    "choices": variant_choices(variant),
                }
                for variant in eligible[:10]
            ],
            "products": [product],
        },
        response_metadata={"used_tray": True},
    )


def _price_for_cart(
    product: dict[str, Any],
    variant: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    variant_resolution = resolve_commercial_price(
        variant or {},
        require_positive=True,
    )
    product_resolution = resolve_commercial_price(
        product,
        require_positive=True,
    )
    selected = (
        variant_resolution
        if variant_resolution.amount is not None
        else product_resolution
    )
    valid = selected.amount is not None and selected.amount > Decimal("0")
    print("[sales.cart.price]", {
        "product_id_present": bool(product.get("id") or product.get("product_id")),
        "price_source": selected.source,
        "price_valid": valid,
    })
    if not valid:
        return None, selected.source
    return format(selected.amount.quantize(Decimal("0.01")), "f"), selected.source

def _original_price_for_cart(
    product: dict[str, Any],
    variant: dict[str, Any] | None,
) -> str | None:
    candidates = (
        [variant.get("price"), product.get("price")]
        if variant is not None
        else [product.get("price")]
    )
    for value in candidates:
        if value is None:
            continue
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if amount > Decimal("0"):
            return format(amount.quantize(Decimal("0.01")), "f")
    return None


def current_cart_reply(
    state: CommerceConversationState,
    *,
    checkout_question: bool,
) -> AgentResult:
    cart_url = _valid_cart_url(state.cart_url)
    if not cart_url or not state.cart_session_id:
        return _validation_failure(
            "Ainda não há um carrinho ativo nesta conversa.",
        )
    if state.checkout_channel_preference != "site":
        return AgentResult(
            reply_text="O link do carrinho só fica disponível no checkout pelo site.",
            intent="commerce",
            handoff_required=False,
            safety_reason="site_checkout_not_selected",
            commercial_data={
                "cart": {
                    "status": "cart_ready",
                    "items": [
                        _cart_item_state(item)
                        for item in state.cart_items
                    ],
                },
                "checkout": checkout_capabilities(state),
            },
            response_metadata={
                "domain": "commerce",
                "purchase_stage": state.purchase_stage or "cart_created",
                "used_tray": False,
            },
        )
    checkout = checkout_capabilities(state)
    return AgentResult(
        reply_text=f"Carrinho atual consultado.\n{cart_url}",
        intent="commerce",
        handoff_required=False,
        commercial_data={
            "cart": {
                "status": "cart_ready",
                "cart_url": cart_url,
                "items": [
                    _cart_item_state(item)
                    for item in state.cart_items
                ],
            },
            "checkout": checkout,
        },
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "cart_created",
            **(
                {
                    "pending_action": "choose_checkout_channel",
                    "pending_action_product_ids": [],
                }
                if checkout_question
                else {}
            ),
            "used_tray": False,
        },
    )

async def _prepare_item(
    request: CartItemRequest,
    execute: ToolExecutor,
) -> tuple[_PreparedCartItem | None, AgentResult | None]:
    reference = request.product_reference
    if isinstance(request.quantity, bool) or request.quantity < 1:
        log_purchase_progress(
            "cart_preparation",
            "blocked",
            "cart_validation_error",
        )
        return None, _with_selected_product(
            _validation_failure("Informe uma quantidade válida para eu preparar o carrinho."),
            reference,
        )

    log_purchase_progress("product_resolution", "start")
    try:
        current = await execute("get_product", {"product_id": reference.product_id})
    except Exception as exc:
        return None, _with_selected_product(
            _technical_failure(
                stage="product_resolution",
                exception_type=type(exc).__name__,
            ),
            reference,
        )
    if "error" in current:
        return None, _with_selected_product(
            _technical_failure(
                stage="product_resolution",
                status_code=current.get("status_code"),
                exception_type=current.get("error_type"),
                diagnostics=current,
            ),
            reference,
        )
    log_purchase_progress("product_resolution", "success")
    product = {
        key: value
        for key, value in {
            "id": reference.product_id,
            "name": reference.name,
            "reference": reference.reference,
            "ean": reference.ean,
            "brand": reference.brand,
        }.items()
        if value is not None
    }
    product.update(current)
    log_purchase_progress("availability", "start")
    try:
        product["commercial_availability"] = commercial_availability_facts(product)
        availability_state = product_availability_state(product)
    except Exception as exc:
        return None, _with_selected_product(
            _technical_failure(
                stage="availability",
                exception_type=type(exc).__name__,
            ),
            reference,
        )
    print("[sales.availability.fact]", {
        "has_stock": product["commercial_availability"]["has_stock"],
        "has_lead_time": product["commercial_availability"]["has_lead_time"],
        "immediate_delivery_supported": product["commercial_availability"]["immediate_delivery_supported"],
    })

    if availability_state == "unavailable":
        log_purchase_progress(
            "availability",
            "blocked",
            "product_unavailable",
        )
        return None, _with_selected_product(
            _validation_failure(
                "Esse produto está indisponível no momento, então não foi adicionado ao carrinho.",
                "product_unavailable",
            ),
            reference,
        )
    log_purchase_progress("availability", "success")

    variant, variant_error = await _resolve_variant(
        product,
        reference,
        request.variant_preferences,
        execute,
    )
    if variant_error is not None:
        print("[sales.purchase.progress]", {
            "purchase_stage": "selection",
            "blocking_reason": variant_error.safety_reason,
        })
        return None, _with_selected_product(variant_error, reference)
    if variant is not None and product_availability_state(variant) == "unavailable":
        log_purchase_progress(
            "variant_resolution",
            "blocked",
            "product_unavailable",
        )
        return None, _with_selected_product(
            _validation_failure(
                "A variação escolhida está indisponível no momento.",
                "product_unavailable",
            ),
            reference,
        )

    log_purchase_progress("price_resolution", "start")
    try:
        price, _price_source = _price_for_cart(product, variant)
    except Exception as exc:
        return None, _with_selected_product(
            _technical_failure(
                stage="price_resolution",
                exception_type=type(exc).__name__,
            ),
            reference,
        )
    if price is None:
        log_purchase_progress(
            "price_resolution",
            "blocked",
            "cart_validation_error",
        )
        return None, _with_selected_product(
            _validation_failure(
                "Não consegui validar o preço atual desse produto para criar o carrinho.",
            ),
            reference,
        )
    log_purchase_progress("price_resolution", "success")

    active_reference = CommerceProductReference(
        product_id=str(product.get("id") or reference.product_id),
        reference=str(product["reference"]) if product.get("reference") is not None else reference.reference,
        variant_id=_variant_id(variant) if variant is not None else None,
        name=str(product["name"]) if product.get("name") is not None else reference.name,
        ean=str(product["ean"]) if product.get("ean") is not None else reference.ean,
        brand=str(product["brand"]) if product.get("brand") is not None else reference.brand,
    )
    return _PreparedCartItem(
        product_reference=active_reference,
        product=product,
        variant=variant,
        quantity=request.quantity,
        price=price,
        original_price=_original_price_for_cart(product, variant),
        position=request.position,
        resolved_from=request.resolved_from,
    ), None


def _verified_items(
    complete: dict[str, Any],
) -> list[CommerceCartItem]:
    parsed: list[CommerceCartItem] = []
    for item in complete.get("items", []):
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id") or item.get("id")
        quantity = item.get("quantity")
        try:
            if product_id is not None:
                parsed.append(CommerceCartItem(
                    product_id=str(product_id),
                    variant_id=normalize_variant_identity(item.get("variant_id")),
                    quantity=int(quantity or 1),
                    unit_price=(
                        str(item.get("unit_price") or item.get("price"))
                        if item.get("unit_price") is not None
                        or item.get("price") is not None
                        else None
                    ),
                    original_price=(
                        str(item["original_price"])
                        if item.get("original_price") is not None
                        else None
                    ),
                    name=(
                        str(item.get("name") or item.get("product_name"))
                        if item.get("name") or item.get("product_name")
                        else None
                    ),
                ))
        except (TypeError, ValueError):
            continue
    return parsed


def _merge_cart_item_prices(
    items: list[CommerceCartItem],
    factual_sources: list[CommerceCartItem],
) -> list[CommerceCartItem]:
    prices = {
        (source.product_id, normalize_variant_identity(source.variant_id)): (
            source.unit_price,
            source.original_price,
        )
        for source in factual_sources
    }
    merged: list[CommerceCartItem] = []
    for item in items:
        factual = prices.get((
            item.product_id,
            normalize_variant_identity(item.variant_id),
        ))
        merged.append(item.model_copy(update={
            "unit_price": item.unit_price or (factual[0] if factual else None),
            "original_price": item.original_price or (factual[1] if factual else None),
            "name": item.name or next((
                source.name
                for source in factual_sources
                if source.product_id == item.product_id
                and normalize_variant_identity(source.variant_id)
                == normalize_variant_identity(item.variant_id)
            ), None),
        }))
    return merged

_TRANSIENT_CART_STATUSES = {502, 503, 504}


def _is_transient_cart_failure(
    *,
    status_code: Any = None,
    error_type: Any = None,
) -> bool:
    try:
        if int(status_code) in _TRANSIENT_CART_STATUSES:
            return True
    except (TypeError, ValueError):
        pass
    normalized = str(error_type or "").casefold()
    return "timeout" in normalized or "connect" in normalized


async def _reconcile_cart_item(
    *,
    execute: ToolExecutor,
    session_id: str,
    item: _PreparedCartItem,
) -> dict[str, Any] | None:
    try:
        complete = await execute(
            "get_cart_complete",
            {"session_id": session_id},
        )
    except Exception as exc:
        print("[sales.cart.reconcile]", {
            "attempted": True,
            "found": False,
            "exception_type": type(exc).__name__,
        })
        return None
    if "error" in complete:
        print("[sales.cart.reconcile]", {
            "attempted": True,
            "found": False,
            "status_code": complete.get("status_code"),
        })
        return None
    found = any(
        verified.product_id == item.product_reference.product_id
        and (
            item.product_reference.variant_id is None
            or verified.variant_id == item.product_reference.variant_id
        )
        and verified.quantity >= item.quantity
        for verified in _verified_items(complete)
    )
    print("[sales.cart.reconcile]", {
        "attempted": True,
        "found": found,
        "item_count": len(_verified_items(complete)),
    })
    return complete if found else None


def _cart_state(
    *,
    cart: dict[str, Any],
    session_id: str,
    cart_url: str,
    items: list[CommerceCartItem],
) -> dict[str, Any]:
    last = items[-1] if items else None
    return {
        "cart_id": str(cart["cart_id"]) if cart.get("cart_id") is not None else None,
        "cart_session_id": session_id,
        "cart_url": cart_url,
        "cart_product_id": last.product_id if last else None,
        "cart_variant_id": last.variant_id if last else None,
        "cart_quantity": last.quantity if last else None,
        "cart_items": [_cart_item_state(item) for item in items],
    }


def _cart_next_metadata(
    state: CommerceConversationState,
) -> dict[str, Any]:
    if state.checkout_channel_preference is None:
        return {
            "purchase_stage": "cart_created",
            "pending_action": "choose_checkout_channel",
            "pending_action_product_ids": [],
        }
    if state.checkout_channel_preference == "whatsapp":
        if not state.shipping_quotes:
            pending = "awaiting_shipping_zipcode"
            blockers = ["shipping_zipcode_missing"]
        elif not state.selected_shipping:
            pending = "awaiting_shipping_selection"
            blockers = ["shipping_not_selected"]
        else:
            pending = "awaiting_checkout_data"
            blockers = []
        print("[sales.checkout.next_requirement]", {
            "purchase_stage": "shipping",
            "pending_action": pending,
            "blocker_codes": blockers,
        })
        return {
            "purchase_stage": "shipping",
            "pending_action": pending,
            "pending_action_product_ids": [],
        }
    return {
        "purchase_stage": "cart_created",
        "clear_pending_action": True,
    }


def _reconciled_cart_result(
    *,
    state: CommerceConversationState,
    complete: dict[str, Any],
    requested: CartItemRequest,
    changed: bool,
    already_satisfied: bool,
) -> AgentResult:
    cart_url = _valid_cart_url(complete.get("cart_url")) or _valid_cart_url(
        state.cart_url
    )
    if not cart_url or not state.cart_session_id:
        return _technical_failure(
            stage="cart_validation",
            exception_type="invalid_cart_response",
            diagnostics=complete,
        )
    items = _merge_cart_item_prices(
        _verified_items(complete),
        list(state.cart_items),
    )
    checkout_state = state.model_copy(deep=True)
    checkout_state.cart_url = cart_url
    checkout_state.cart_items = items
    next_metadata = _cart_next_metadata(checkout_state)
    cart_facts: dict[str, Any] = {
        "status": "cart_ready",
        "items": [item.model_dump(mode="json", exclude={"name"}) for item in items],
        "subtotal": complete.get("subtotal"),
        "total": complete.get("total") or complete.get("current_total"),
        "mutation_success": True,
        "changed": changed,
        "already_satisfied": already_satisfied,
    }
    if state.checkout_channel_preference == "site":
        cart_facts["cart_url"] = cart_url
    print("[sales.cart.ensure]", {
        "session_hash": state.cart_session_id[-8:],
        "product_id": requested.product_reference.product_id,
        "variant_present": requested.product_reference.variant_id is not None,
        "quantity": requested.quantity,
        "already_satisfied": already_satisfied,
        "changed": changed,
    })
    return AgentResult(
        reply_text="Estado factual do carrinho confirmado.",
        intent="commerce",
        handoff_required=False,
        commercial_data={
            "cart": cart_facts,
            "checkout": checkout_capabilities(checkout_state),
        },
        response_metadata={
            "domain": "commerce",
            "cart_materially_changed": changed,
            "cart_state": _cart_state(
                cart=complete,
                session_id=state.cart_session_id,
                cart_url=cart_url,
                items=items,
            ),
            **next_metadata,
            "used_tray": True,
        },
    )


async def _ensure_existing_cart_item(
    *,
    request: CartItemRequest,
    state: CommerceConversationState,
    execute: ToolExecutor,
    allow_create: bool,
) -> AgentResult | None:
    if not state.cart_session_id:
        return None
    try:
        complete = await execute(
            "get_cart_complete",
            {"session_id": state.cart_session_id},
        )
    except Exception as exc:
        return _technical_failure(
            stage="cart_reconcile",
            exception_type=type(exc).__name__,
        )
    if "error" in complete:
        return _technical_failure(
            stage="cart_reconcile",
            status_code=complete.get("status_code"),
            exception_type=complete.get("error_type"),
            diagnostics=complete,
        )
    items = _verified_items(complete)
    target = next(
        (
            item
            for item in items
            if item.product_id == request.product_reference.product_id
            and item.variant_id == request.product_reference.variant_id
        ),
        None,
    )
    print("[sales.cart.reconcile]", {
        "attempted": True,
        "found": target is not None,
        "item_count": len(items),
    })
    if target is None:
        return None if allow_create else _validation_failure(
            "O item informado não existe no carrinho atual.",
            "cart_item_not_found",
        )
    if target.quantity == request.quantity:
        return _reconciled_cart_result(
            state=state,
            complete=complete,
            requested=request,
            changed=False,
            already_satisfied=True,
        )
    print("[sales.cart.quantity]", {
        "session_hash": state.cart_session_id[-8:],
        "product_id": request.product_reference.product_id,
        "variant_present": request.product_reference.variant_id is not None,
        "quantity": request.quantity,
    })
    try:
        updated = await execute(
            "set_cart_item_quantity",
            {
                "session_id": state.cart_session_id,
                "product_id": request.product_reference.product_id,
                "variant_id": request.product_reference.variant_id,
                "quantity": request.quantity,
            },
        )
    except Exception as exc:
        return _technical_failure(
            stage="cart_quantity_update",
            exception_type=type(exc).__name__,
        )
    if "error" in updated:
        return _technical_failure(
            stage="cart_quantity_update",
            status_code=updated.get("status_code"),
            exception_type=updated.get("error_type"),
            diagnostics=updated,
        )
    try:
        complete = await execute(
            "get_cart_complete",
            {"session_id": state.cart_session_id},
        )
    except Exception as exc:
        return _technical_failure(
            stage="cart_quantity_reconcile",
            exception_type=type(exc).__name__,
        )
    if "error" in complete:
        return _technical_failure(
            stage="cart_quantity_reconcile",
            status_code=complete.get("status_code"),
            exception_type=complete.get("error_type"),
            diagnostics=complete,
        )
    final_item = next(
        (
            item
            for item in _verified_items(complete)
            if item.product_id == request.product_reference.product_id
            and item.variant_id == request.product_reference.variant_id
        ),
        None,
    )
    if final_item is None or final_item.quantity != request.quantity:
        return _technical_failure(
            stage="cart_quantity_verification",
            exception_type="cart_quantity_not_reconciled",
            diagnostics=complete,
        )
    return _reconciled_cart_result(
        state=state,
        complete=complete,
        requested=request,
        changed=True,
        already_satisfied=False,
    )


async def set_cart_item_quantity(
    *,
    product_reference: CommerceProductReference,
    quantity: int,
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    if isinstance(quantity, bool) or quantity < 1:
        return _validation_failure(
            "Informe uma quantidade final válida.",
            "cart_validation_error",
        )
    result = await _ensure_existing_cart_item(
        request=CartItemRequest(
            product_reference=product_reference,
            quantity=quantity,
            resolved_from="context",
        ),
        state=state,
        execute=execute,
        allow_create=False,
    )
    return result or _validation_failure(
        "O item informado não existe no carrinho atual.",
        "cart_item_not_found",
    )

async def _create_cart_items_checkout_impl(
    *,
    item_requests: list[CartItemRequest],
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    log_purchase_progress("cart_preparation", "start")
    print("[sales.cart.prepare]", {
        "status": "start",
        "requested_count": len(item_requests),
        "prepared_count": 0,
    })
    print("[sales.cart.items]", {
        "requested_count": len(item_requests),
        "resolved_count": len(item_requests),
    })
    if not item_requests:
        log_purchase_progress(
            "cart_preparation",
            "blocked",
            "cart_validation_error",
        )
        print("[sales.cart.prepare]", {
            "status": "blocked",
            "requested_count": 0,
            "prepared_count": 0,
        })
        return _validation_failure(
            "Não consegui identificar quais produtos devem entrar no carrinho.",
            "cart_validation_error",
        )

    expected = sorted(
        (
            request.product_reference.product_id,
            request.product_reference.variant_id,
            request.quantity,
        )
        for request in item_requests
    )
    existing = sorted(
        (item.product_id, item.variant_id, item.quantity)
        for item in state.cart_items
    )
    if (
        state.cart_session_id
        and _valid_cart_url(state.cart_url)
        and expected
        and (
            expected == existing
            or (
                not existing
                and len(expected) == 1
                and state.cart_product_id == expected[0][0]
                and state.cart_variant_id == expected[0][1]
                and state.cart_quantity == expected[0][2]
            )
        )
    ):
        log_purchase_progress("cart_preparation", "success")
        print("[sales.cart.prepare]", {
            "status": "success",
            "requested_count": len(item_requests),
            "prepared_count": 0,
        })
        log_purchase_progress("completed", "success")
        return current_cart_reply(state, checkout_question=False)

    prepared: list[_PreparedCartItem] = []
    for request in item_requests:
        item, error = await _prepare_item(request, execute)
        print("[sales.cart.item]", {
            "position": request.position,
            "has_product_id": bool(request.product_reference.product_id),
            "quantity": request.quantity,
            "status": error.safety_reason if error else "validated",
        })
        if error is not None:
            print("[sales.cart.prepare]", {
                "status": "failed",
                "requested_count": len(item_requests),
                "prepared_count": len(prepared),
            })
            return error
        if item is not None:
            prepared.append(item)
    log_purchase_progress("cart_preparation", "success")
    print("[sales.cart.prepare]", {
        "status": "success",
        "requested_count": len(item_requests),
        "prepared_count": len(prepared),
    })

    session_id = state.cart_session_id
    cart_url = _valid_cart_url(state.cart_url)
    cart: dict[str, Any] = {}
    created: dict[str, Any] = {}
    successful = [
        CommerceCartItem.model_validate(item)
        for item in state.cart_items
    ]
    failed_item: _PreparedCartItem | None = None
    reconciled_complete: dict[str, Any] | None = None

    for item in prepared:
        payload = {
            "product_id": item.product_reference.product_id,
            "variant_id": item.product_reference.variant_id,
            "quantity": item.quantity,
            "price": item.price,
            "session_id": session_id,
        }
        log_purchase_progress("cart_http", "start")
        try:
            created = await execute("create_cart", payload)
        except Exception as exc:
            reconciled = (
                await _reconcile_cart_item(
                    execute=execute,
                    session_id=session_id,
                    item=item,
                )
                if _is_transient_cart_failure(error_type=type(exc).__name__)
                else None
            )
            if reconciled is not None:
                cart = reconciled
                cart_url = _valid_cart_url(reconciled.get("cart_url")) or cart_url
                successful.append(CommerceCartItem(
                    product_id=item.product_reference.product_id,
                    variant_id=item.product_reference.variant_id,
                    quantity=item.quantity,
                    unit_price=item.price,
                    original_price=item.original_price,
                    name=item.product_reference.name,
                ))
                reconciled_complete = reconciled
                log_purchase_progress("cart_http", "success")
                continue
            return _technical_failure(
                stage="cart_http",
                exception_type=type(exc).__name__,
            )
        if "error" in created:
            log_purchase_progress(
                "cart_http",
                "failed",
                created.get("error_type") or "upstream_error",
            )
            print("[sales.cart.failure]", {
                "stage": "cart_http",
                "exception_type": created.get("error_type") or "upstream_error",
                "upstream_status": created.get("status_code"),
            })
            failed_item = item
            print("[sales.cart.item]", {
                "position": item.position,
                "has_product_id": True,
                "quantity": item.quantity,
                "status": "cart_technical_failure",
            })
            reconciled = (
                await _reconcile_cart_item(
                    execute=execute,
                    session_id=session_id,
                    item=item,
                )
                if _is_transient_cart_failure(
                    status_code=created.get("status_code"),
                    error_type=created.get("error_type"),
                )
                else None
            )
            if reconciled is not None:
                cart = reconciled
                cart_url = _valid_cart_url(reconciled.get("cart_url")) or cart_url
                successful.append(CommerceCartItem(
                    product_id=item.product_reference.product_id,
                    variant_id=item.product_reference.variant_id,
                    quantity=item.quantity,
                    unit_price=item.price,
                    original_price=item.original_price,
                    name=item.product_reference.name,
                ))
                reconciled_complete = reconciled
                log_purchase_progress("cart_http", "success")
                continue
            if not session_id or not cart_url:
                return _technical_failure(
                    stage="cart_http",
                    status_code=created.get("status_code"),
                    exception_type=created.get("error_type"),
                    diagnostics=created,
                )
            break
        log_purchase_progress("cart_http", "success")
        cart = created
        returned_session_id = (
            str(created["session_id"])
            if created.get("session_id") is not None
            else None
        )
        if returned_session_id and returned_session_id != session_id:
            print("[sales.cart.session]", {
                "generated": False,
                "reused": False,
                "response_mismatch": True,
            })
            session_id = returned_session_id
        cart_url = _valid_cart_url(created.get("cart_url")) or cart_url
        reconciled_complete = None
        successful.append(CommerceCartItem(
            product_id=item.product_reference.product_id,
            variant_id=item.product_reference.variant_id,
            quantity=item.quantity,
            unit_price=item.price,
            original_price=item.original_price,
            name=item.product_reference.name,
        ))
        print("[sales.cart.item]", {
            "position": item.position,
            "has_product_id": True,
            "quantity": item.quantity,
            "status": "added",
        })

    print("[sales.cart.create]", {
        "success": failed_item is None and bool(session_id and cart_url),
        "has_session_id": bool(session_id),
        "has_cart_url": bool(cart_url),
    })
    log_purchase_progress("cart_validation", "start")
    if not session_id or not cart_url:
        return _technical_failure(
            stage="cart_validation",
            status_code=(
                created.get("status_code")
                if isinstance(created, dict)
                else None
            ),
            exception_type=(
                created.get("error_type")
                if isinstance(created, dict)
                else "invalid_cart_response"
            ),
            diagnostics=created if isinstance(created, dict) else None,
        )

    if reconciled_complete is not None:
        complete = reconciled_complete
        complete_error: str | None = None
    else:
        try:
            complete = await execute("get_cart_complete", {"session_id": session_id})
        except Exception as exc:
            complete = {}
            complete_error = type(exc).__name__
        else:
            complete_error = (
                str(complete.get("error_type") or "cart_verification_failed")
                if "error" in complete
                else None
            )
    verify_ok = "error" not in complete
    complete_items = _verified_items(complete) if verify_ok else []
    verified_items = complete_items or list(successful)
    verified_items = _merge_cart_item_prices(
        verified_items,
        [*state.cart_items, *successful],
    )
    print("[sales.cart.verify]", {
        "item_count": len(verified_items),
        "has_total": bool(
            verify_ok
            and any(complete.get(key) is not None for key in ("total", "current_total", "subtotal"))
        ),
    })
    verification_matches = verify_ok and all(
        any(
            verified.product_id == item.product_reference.product_id
            and (
                item.product_reference.variant_id is None
                or verified.variant_id == item.product_reference.variant_id
            )
            and verified.quantity >= item.quantity
            for verified in complete_items
        )
        for item in prepared
        if item is not failed_item
    )

    if not verification_matches and failed_item is None:
        # A successful POST with a valid session/link is authoritative. The
        # read model may lag briefly, so retain the successfully posted items.
        verified_items = list(successful)

    cart_state = _cart_state(
        cart=cart,
        session_id=session_id,
        cart_url=cart_url,
        items=verified_items,
    )
    active = prepared[-1].product_reference
    partial = failed_item is not None
    verification_pending = not partial and not verification_matches
    log_purchase_progress(
        "cart_validation",
        "success",
        "eventual_consistency" if verification_pending else complete_error,
    )
    status = "cart_partial_failure" if partial else "cart_created"
    reply = (
        "Carrinho atualizado parcialmente."
        if partial
        else "Carrinho atualizado."
    )
    print("[sales.cart.state]", {
        "purchase_stage": "cart_created",
        "has_cart_session": True,
    })
    log_purchase_progress(
        "completed",
        "success" if not partial else "failed",
        None if not partial else "cart_partial_failure",
    )
    checkout_state = state.model_copy(deep=True)
    checkout_state.cart_session_id = session_id
    checkout_state.cart_url = cart_url
    checkout_state.cart_items = verified_items
    checkout = checkout_capabilities(checkout_state)
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason="cart_partial_failure" if partial else None,
        commercial_data={
            "products": [item.product for item in prepared],
            "items": [
                {
                    "product_id": item.product_reference.product_id,
                    "variant_id": item.product_reference.variant_id,
                    "quantity": item.quantity,
                    "current_price": item.price,
                }
                for item in prepared
            ],
            "cart": {
                "status": status,
                "cart_id": cart_state["cart_id"],
                "session_id": session_id,
                "items": [
                    item.model_dump(mode="json", exclude={"unit_price", "name"})
                    for item in verified_items
                ],
                "total": complete.get("total") or complete.get("current_total"),
                "verification_ok": verification_matches,
                "verification_status": (
                    "partial"
                    if partial
                    else "pending"
                    if verification_pending
                    else "confirmed"
                ),
            },
            "checkout": checkout,
            **(
                {
                    "variant": prepared[0].variant,
                    "quantity": prepared[0].quantity,
                    "current_price": prepared[0].price,
                }
                if len(prepared) == 1
                else {}
            ),
        },
        response_metadata={
            "domain": "commerce",
            "cart_materially_changed": True,
            "active_product": active.model_dump(mode="json"),
            "purchase_stage": "cart_created",
            "cart_state": cart_state,
            **(
                {
                    "pending_action": "choose_checkout_channel",
                    "pending_action_product_ids": [],
                }
                if not partial
                else {}
            ),
            "used_tray": True,
        },
    )


async def create_cart_items_checkout(
    *,
    item_requests: list[CartItemRequest],
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    if len(item_requests) == 1 and state.cart_session_id:
        ensured = await _ensure_existing_cart_item(
            request=item_requests[0],
            state=state,
            execute=execute,
            allow_create=True,
        )
        if ensured is not None:
            return _persist_cart_session(
                ensured,
                state,
                state.cart_session_id,
            )
    execution_state = state
    if item_requests and not state.cart_session_id:
        execution_state = state.model_copy(deep=True)
        execution_state.cart_session_id = secrets.token_hex(16)
        print("[sales.cart.session]", {
            "generated": True,
            "reused": False,
            "response_mismatch": False,
        })
    elif state.cart_session_id:
        print("[sales.cart.session]", {
            "generated": False,
            "reused": True,
            "response_mismatch": False,
        })
    try:
        result = await _create_cart_items_checkout_impl(
            item_requests=item_requests,
            state=execution_state,
            execute=execute,
        )
    except Exception as exc:
        print("[sales.cart.prepare]", {
            "status": "failed",
            "requested_count": len(item_requests),
            "prepared_count": 0,
        })
        result = _technical_failure(
            stage="cart_preparation",
            exception_type=type(exc).__name__,
        )
    return _persist_cart_session(
        result,
        execution_state,
        execution_state.cart_session_id,
    )


async def create_cart_checkout(
    *,
    interpretation: SalesInterpretation,
    product_reference: CommerceProductReference,
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    quantity = interpretation.quantity or 1
    print("[sales.cart.resolve]", {
        "has_active_product": state.active_product is not None,
        "resolved_from": (
            "list_position"
            if interpretation.reference_type == "list_position"
            else "context"
        ),
        "has_variant": product_reference.variant_id is not None,
        "quantity": quantity,
        "purchase_stage": state.purchase_stage,
    })
    result = await create_cart_items_checkout(
        item_requests=[
            CartItemRequest(
                product_reference=product_reference,
                quantity=quantity,
                position=interpretation.reference_position,
                resolved_from=interpretation.reference_type or "context",
                variant_preferences=interpretation.preferences.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
            )
        ],
        state=state,
        execute=execute,
    )
    if result.safety_reason == "cart_technical_failure":
        _with_selected_product(result, product_reference)
    return result


def _normalize_text(value: str) -> str:
    text = str(value or "").strip().lower()
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    return text


def resolve_cart_item_reference(
    interpretation: SalesInterpretation,
    state: CommerceConversationState,
    resolved_product: CommerceProductReference | None = None,
) -> tuple[list[tuple[str | None, str | None]], str]:
    if not state.cart_items:
        return [], "empty_cart"

    if resolved_product is not None:
        for item in state.cart_items:
            if (str(resolved_product.product_id) == str(item.product_id) and
                normalize_variant_identity(resolved_product.variant_id) == item.variant_id):
                return [(str(item.product_id), item.variant_id)], "exact_match"

    if interpretation.reference_type == "list_position" and interpretation.reference_position is not None:
        pos = interpretation.reference_position
        if 1 <= pos <= len(state.cart_items):
            item = state.cart_items[pos - 1]
            return [(str(item.product_id), item.variant_id)], "list_position"
        else:
            return [], "position_out_of_range"

    message_text = (interpretation.subject.model or "").strip() if interpretation.subject else ""
    if message_text:
        message_normalized = _normalize_text(message_text)
        matches = []
        for item in state.cart_items:
            if item.name:
                item_name_normalized = _normalize_text(item.name)
                if item_name_normalized == message_normalized or message_normalized in item_name_normalized:
                    matches.append((str(item.product_id), item.variant_id))
        if len(matches) == 1:
            return matches, "name_match"
        elif len(matches) > 1:
            return [], "ambiguous"

    if len(state.cart_items) == 1 and interpretation.purchase_action == "remove_cart_item":
        item = state.cart_items[0]
        return [(str(item.product_id), item.variant_id)], "single_item"

    return [], "not_found"


async def rebuild_cart_without(
    state: CommerceConversationState,
    targets: list[tuple[str | None, str | None]],
    execute: ToolExecutor,
) -> tuple[CommerceConversationState, dict[str, Any]]:
    old_session_id = state.cart_session_id
    if not old_session_id or not targets:
        return state, {
            "success": False,
            "reason": "invalid_parameters",
        }

    # A. get_cart_complete(sessão antiga) -> falhou? aborta, nada apagado
    cart_result = await execute("get_cart_complete", {"session_id": old_session_id})
    if "error" in cart_result:
        return state, {
            "success": False,
            "reason": "cart_read_failed",
            "error": cart_result.get("error"),
        }

    current_items = cart_result.get("items", [])
    target_set = {
        (str(product_id) if product_id is not None else None,
         normalize_variant_identity(variant_id))
        for product_id, variant_id in targets
    }

    # B. separar itens que saem / que ficam
    items_to_remove = []
    items_to_keep = []
    for item in current_items:
        if not isinstance(item, dict):
            continue
        item_product_id = str(item.get("product_id")) if item.get("product_id") is not None else None
        item_variant_id = normalize_variant_identity(item.get("variant_id"))
        item_key = (item_product_id, item_variant_id)
        if item_key in target_set:
            items_to_remove.append(item_key)
        else:
            items_to_keep.append(item)

    # C. item alvo não existe -> aborta, nada apagado
    if len(items_to_remove) != len(targets):
        missing_count = len(targets) - len(items_to_remove)
        return state, {
            "success": False,
            "reason": "item_not_found",
            "missing_count": missing_count,
        }

    # Caso especial: carrinho vazio após remoção
    if not items_to_keep:
        delete_result = await execute("delete_cart", {"session_id": old_session_id})
        if "error" in delete_result:
            return state, {
                "success": False,
                "reason": "delete_failed",
                "error": delete_result.get("error"),
            }

        new_state = state.model_copy(deep=True)
        new_state.cart_session_id = None
        new_state.cart_items = []
        new_state.shipping_quotes = []
        new_state.shipping_quote_zipcode = None
        new_state.selected_shipping = None
        new_state.selected_payment_option = None
        new_state.selected_payment_option_id = None
        new_state.selected_payment_method = None
        new_state.pending_action = None
        new_state.order_confirmation_status = "not_ready"
        new_state.order_review_version = None
        new_state.confirmed_order_review_version = None

        return new_state, {
            "success": True,
            "empty_cart": True,
            "items": [],
        }

    # D. PREÇO: para cada item que fica, extrair o preço factual do snapshot.
    #    Se QUALQUER item ficar sem preço utilizável -> aborta, nada apagado
    items_with_price = []
    for item in items_to_keep:
        if not isinstance(item, dict):
            continue
        unit_price = item.get("unit_price") or item.get("price")
        if not unit_price:
            return state, {
                "success": False,
                "reason": "price_missing",
                "item": {
                    "product_id": str(item.get("product_id")) if item.get("product_id") is not None else None,
                    "variant_id": normalize_variant_identity(item.get("variant_id")),
                },
            }
        items_with_price.append((item, unit_price))

    # E. gerar new_session_id
    new_session_id = secrets.token_hex(16)
    recovered_items = []
    failed_items = []

    # F. create_cart de cada item que fica, no novo session_id, COM price
    #    qualquer falha -> aborta, nada apagado, motivo "rebuild_failed"
    for item, unit_price in items_with_price:
        product_id = item.get("product_id")
        variant_id = normalize_variant_identity(item.get("variant_id"))
        quantity = item.get("quantity", 1)

        create_result = await execute(
            "create_cart",
            {
                "session_id": new_session_id,
                "product_id": str(product_id) if product_id is not None else None,
                "variant_id": str(variant_id) if variant_id is not None else None,
                "quantity": quantity,
                "price": str(unit_price),
            },
        )

        if "error" in create_result:
            # best-effort cleanup de carrinho novo parcial (sem propagar erro)
            await execute("delete_cart", {"session_id": new_session_id})
            return state, {
                "success": False,
                "reason": "rebuild_failed",
                "error": create_result.get("error"),
            }

        recovered_items.append({
            "product_id": str(product_id) if product_id is not None else None,
            "variant_id": variant_id,
            "quantity": quantity,
            "unit_price": unit_price,
        })

    # G. get_cart_complete(new_session_id) e conferir contra o snapshot esperado:
    #    contagem, product_id, variant_id e quantity de cada item
    #    divergiu -> aborta, nada apagado, motivo "verification_failed"
    verification_result = await execute("get_cart_complete", {"session_id": new_session_id})

    if "error" in verification_result:
        # best-effort cleanup
        await execute("delete_cart", {"session_id": new_session_id})
        return state, {
            "success": False,
            "reason": "verification_failed",
            "error": verification_result.get("error"),
        }

    final_items = verification_result.get("items", [])
    if len(final_items) != len(recovered_items):
        # best-effort cleanup
        await execute("delete_cart", {"session_id": new_session_id})
        return state, {
            "success": False,
            "reason": "verification_failed",
            "expected_count": len(recovered_items),
            "actual_count": len(final_items),
        }

    # Verificar conteúdo: product_id, variant_id e quantity de cada item
    for recovered, actual in zip(recovered_items, final_items):
        if not isinstance(actual, dict):
            await execute("delete_cart", {"session_id": new_session_id})
            return state, {
                "success": False,
                "reason": "verification_failed",
            }
        actual_product_id = str(actual.get("product_id")) if actual.get("product_id") is not None else None
        actual_variant_id = normalize_variant_identity(actual.get("variant_id"))
        actual_quantity = actual.get("quantity", 1)

        if (actual_product_id != recovered["product_id"] or
            actual_variant_id != recovered["variant_id"] or
            actual_quantity != recovered["quantity"]):
            await execute("delete_cart", {"session_id": new_session_id})
            return state, {
                "success": False,
                "reason": "verification_failed",
            }

    # H. trocar o estado para a nova sessão
    new_state = state.model_copy(deep=True)
    new_state.cart_session_id = new_session_id
    new_state.cart_items = [
        CommerceCartItem(
            product_id=str(item.get("product_id")) if item.get("product_id") is not None else None,
            variant_id=normalize_variant_identity(item.get("variant_id")),
            quantity=item.get("quantity", 1),
            unit_price=item.get("unit_price") or item.get("price"),
            original_price=item.get("original_price") or item.get("unit_price") or item.get("price"),
            name=item.get("name"),
        )
        for item in final_items
        if isinstance(item, dict)
    ]
    new_state.shipping_quotes = []
    new_state.shipping_quote_zipcode = None
    new_state.selected_shipping = None
    new_state.selected_payment_option = None
    new_state.selected_payment_option_id = None
    new_state.selected_payment_method = None
    new_state.pending_action = None
    new_state.order_confirmation_status = "not_ready"
    new_state.order_review_version = None
    new_state.confirmed_order_review_version = None

    # I. delete_cart(sessão antiga) em BEST-EFFORT: se falhar, apenas registre
    delete_result = await execute("delete_cart", {"session_id": old_session_id})
    # Não propagamos erro; o carrinho novo é válido e o estado já aponta para ele

    return new_state, {
        "success": True,
        "removal_supported": True,
        "mutation_success": True,
        "items": [
            {
                "product_id": str(item.get("product_id")) if item.get("product_id") is not None else None,
                "variant_id": normalize_variant_identity(item.get("variant_id")),
                "quantity": item.get("quantity", 1),
                "unit_price": item.get("unit_price") or item.get("price"),
            }
            for item in final_items
            if isinstance(item, dict)
        ],
        "shipping_reset": True,
        "payment_reset": True,
    }
