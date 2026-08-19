from __future__ import annotations

import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .models import AgentResult, PurchaseItem, SalesInterpretation

def normalize_variant_identity(value: Any) -> str | None:
    """Return the canonical NSAgent identity for an optional Tray variant."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("variant_id must be a positive numeric identifier")
    text = str(value).strip()
    if text in {"", "0"}:
        return None
    if not text.isascii() or not text.isdecimal() or int(text) < 1:
        raise ValueError("variant_id must be a positive numeric identifier")
    return str(int(text))


class CommerceProductReference(BaseModel):
    product_id: str
    reference: str | None = None
    variant_id: str | None = None

    @field_validator("variant_id", mode="before")
    @classmethod
    def normalize_variant_id(cls, value: Any) -> str | None:
        return normalize_variant_identity(value)

    name: str | None = None
    ean: str | None = None
    brand: str | None = None
    product_url: str | None = None


class PresentedCommerceProduct(CommerceProductReference):
    position: int


class CommerceCartItem(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(ge=1)
    unit_price: str | None = None
    original_price: str | None = None
    name: str | None = None

    @field_validator("variant_id", mode="before")
    @classmethod
    def normalize_variant_id(cls, value: Any) -> str | None:
        return normalize_variant_identity(value)


class ShippingQuote(BaseModel):
    shipping_id: int | str
    quotation_id: str | None = None
    name: str
    price: str
    min_period: int | None = None
    max_period: int | None = None
    estimated_delivery_date: str | None = None
    information: str | None = None
    identifier: str | None = None
    tax_name: str | None = None
    tax_value: str | None = None


class SelectedPaymentOption(BaseModel):
    id: str | None = None
    name: str
    method: Literal["pix", "card", "boleto", "other"] | None = None
    integration_code: str | None = None
    installments: list[dict[str, Any]] = Field(default_factory=list)
    discount_value: float | None = None
    increase_value: float | None = None
    application_value: float | None = None
    total_base: float | None = None
    tax_value: float | None = None


class CheckoutCustomer(BaseModel):
    type: str = "0"
    name: str | None = None
    cpf: str | None = None
    email: str | None = None
    phone: str | None = None
    rg: str | None = None
    gender: str | None = None


class CheckoutAddress(BaseModel):
    address: str | None = None
    zip_code: str | None = None
    number: str | None = None
    complement: str | None = None
    neighborhood: str | None = None
    city: str | None = None
    state: str | None = None
    country: str = "BRA"
    type: str = "1"


class CheckoutDraft(BaseModel):
    customer: CheckoutCustomer = Field(default_factory=CheckoutCustomer)
    address: CheckoutAddress = Field(default_factory=CheckoutAddress)


CHECKOUT_REQUIRED_FIELDS = (
    "name", "cpf", "email", "phone", "address", "zipcode", "number",
    "neighborhood", "city", "state",
)


def checkout_fields_view(draft: CheckoutDraft) -> dict[str, bool]:
    customer = draft.customer
    address = draft.address
    return {
        "name": bool(customer.name),
        "cpf": bool(customer.cpf),
        "email": bool(customer.email),
        "phone": bool(customer.phone),
        "address": bool(address.address),
        "zipcode": bool(address.zip_code),
        "number": bool(address.number),
        "complement": bool(address.complement),
        "neighborhood": bool(address.neighborhood),
        "city": bool(address.city),
        "state": bool(address.state),
    }


def checkout_missing_fields(draft: CheckoutDraft) -> list[str]:
    fields = checkout_fields_view(draft)
    return [field for field in CHECKOUT_REQUIRED_FIELDS if not fields[field]]


class CommerceConversationState(BaseModel):
    active_domain: Literal["commerce", "raffle"] | None = None
    active_topic: str | None = None
    active_product: CommerceProductReference | None = None
    last_presented_products: list[PresentedCommerceProduct] = Field(default_factory=list)
    last_story_product: dict[str, Any] | None = None
    # found_available | found_unknown | found_unavailable | plausible_matches | None
    product_resolution_state: str | None = None
    active_preferences: dict[str, Any] = Field(default_factory=dict)
    purchase_stage: str | None = None
    cart_id: str | None = None
    cart_session_id: str | None = None
    cart_url: str | None = None
    cart_product_id: str | None = None
    cart_variant_id: str | None = None
    cart_quantity: int | None = None
    cart_items: list[CommerceCartItem] = Field(default_factory=list)

    @field_validator("cart_variant_id", mode="before")
    @classmethod
    def normalize_cart_variant_id(cls, value: Any) -> str | None:
        return normalize_variant_identity(value)

    selected_payment_method: Literal[
        "pix",
        "card",
        "boleto",
        "other",
    ] | None = None
    payment_method_preference: Literal["pix", "card", "boleto", "other"] | None = None
    selected_payment_option_id: str | None = None
    selected_payment_option: SelectedPaymentOption | None = None
    checkout_channel_preference: Literal["whatsapp", "site"] | None = None
    shipping_quote_zipcode: str | None = None
    shipping_quotes: list[ShippingQuote] = Field(default_factory=list)
    selected_shipping: ShippingQuote | None = None
    checkout_draft: CheckoutDraft = Field(default_factory=CheckoutDraft)
    order_confirmation_status: Literal["not_ready", "pending", "confirmed"] = "not_ready"
    order_review_version: str | None = None
    confirmed_order_review_version: str | None = None
    order_id: str | None = None
    order_status: str | None = None
    order_status_group: str | None = None
    order_session_id: str | None = None
    order_created_at: str | None = None
    order_creation_ambiguous: bool = False
    order_lookup_id: str | None = None
    order_payment_method_id: str | None = None
    order_payment_method: str | None = None
    order_payment_type: str | None = None
    order_payment_url: str | None = None
    order_payment_status: Literal[
        "not_available", "pending", "confirmed", "unknown"
    ] = "not_available"
    order_has_payment: bool | None = None
    order_payment_date: str | None = None
    order_payment_checked_at: str | None = None
    order_payment_revalidation_status: Literal[
        "not_checked", "confirmed", "unavailable", "ambiguous"
    ] = "not_checked"
    # Direct Mercado Pago PIX in chat (before Tray order exists).
    pix_payment_id: str | None = None
    pix_payment_status: str | None = None
    pix_copy_paste_code: str | None = None
    pix_amount_label: str | None = None
    pix_order_review_version: str | None = None
    pending_action: Literal[
        "send_product_link",
        "create_cart",
        "show_images",
        "show_payment_options",
        "confirm_purchase",
        "choose_checkout_channel",
        "awaiting_shipping_zipcode",
        "awaiting_shipping_selection",
        "awaiting_checkout_data",
        "awaiting_order_confirmation",
        "awaiting_payment",
        "awaiting_order_customer_document",
    ] | None = None
    pending_action_product_ids: list[str] = Field(default_factory=list)

    @property
    def order_confirmation_pending(self) -> bool:
        return self.order_confirmation_status == "pending"

    @classmethod
    def from_payload(cls, value: Any) -> "CommerceConversationState":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return cls()
        try:
            return cls.model_validate(value)
        except (TypeError, ValueError):
            return cls()

    def interpreter_payload(self) -> dict[str, Any]:
        """Expose semantic identity without asking the model to handle internal IDs."""
        active = self.active_product
        return {
            "active_domain": self.active_domain,
            "active_topic": self.active_topic,
            "active_product": (
                {
                    "name": active.name,
                    "reference": active.reference,
                    "ean": active.ean,
                    "brand": active.brand,
                }
                if active
                else None
            ),
            "last_presented_products": [
                {
                    "position": product.position,
                    "name": product.name,
                    "reference": product.reference,
                    "brand": product.brand,
                }
                for product in self.last_presented_products
            ],
            "active_preferences": self.active_preferences,
            "purchase_stage": self.purchase_stage,
            "has_cart": bool(self.cart_session_id and self.cart_url),
            "cart_item_count": len(self.cart_items),
            "cart_items": [
                {
                    "product_id": item.product_id,
                    "variant_id": item.variant_id,
                    "name": item.name,
                    "quantity": item.quantity,
                }
                for item in self.cart_items
            ],
            "selected_payment_method": self.selected_payment_method,
            "payment_method_preference": self.payment_method_preference,
            "selected_payment": (
                self.selected_payment_option.model_dump(mode="json")
                if self.selected_payment_option else None
            ),
            "checkout_channel_preference": self.checkout_channel_preference,
            "payment_method": {
                "type": self.selected_payment_method,
                "name": (
                    self.selected_payment_option.name
                    if self.selected_payment_option else None
                ),
                "available": bool(self.selected_payment_option),
            },
            "hosted_payment": {
                "order_created": bool(self.order_id),
                "payment_url_available": bool(
                    self.order_id and self.order_payment_url
                ),
            },
            "shipping_quote_available": bool(self.shipping_quotes),
            "shipping_quote_count": len(self.shipping_quotes),
            "selected_shipping": (
                {
                    "shipping_id": self.selected_shipping.shipping_id,
                    "quotation_id": self.selected_shipping.quotation_id,
                    "name": self.selected_shipping.name,
                }
                if self.selected_shipping else None
            ),
            "checkout_fields": checkout_fields_view(self.checkout_draft),
            "required_fields": list(CHECKOUT_REQUIRED_FIELDS),
            "missing_fields": checkout_missing_fields(self.checkout_draft),
            "order_confirmation_status": self.order_confirmation_status,
            "order_confirmation_pending": self.order_confirmation_pending,
            "order_ready": bool(
                self.cart_session_id
                and self.checkout_channel_preference == "whatsapp"
                and self.selected_shipping
                and self.selected_payment_option
                and not checkout_missing_fields(self.checkout_draft)
            ),
            "has_order": bool(self.order_id),
            "order_id": self.order_id,
            "order_payment": {
                "status": self.order_payment_status,
                "method": self.order_payment_method,
                "type": self.order_payment_type,
                "has_payment": self.order_has_payment,
                "payment_url_available": bool(self.order_payment_url),
                "revalidation_status": self.order_payment_revalidation_status,
            },
            "pending_action": self.pending_action,
            "pending_action_product_count": len(self.pending_action_product_ids),
        }


def _fold(value: Any) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or "").lower())
        if not unicodedata.combining(char)
    ).strip()


def product_reference_from_product(product: dict[str, Any]) -> CommerceProductReference | None:
    product_id = product.get("id") or product.get("product_id")
    if product_id is None:
        return None
    return CommerceProductReference(
        product_id=str(product_id),
        reference=str(product["reference"]) if product.get("reference") is not None else None,
        variant_id=normalize_variant_identity(product.get("variant_id")),
        name=str(product["name"]) if product.get("name") is not None else None,
        ean=str(product["ean"]) if product.get("ean") is not None else None,
        brand=str(product["brand"]) if product.get("brand") is not None else None,
        product_url=(
            str(product.get("product_url") or product.get("url"))
            if product.get("product_url") or product.get("url")
            else None
        ),
    )


def _explicit_product_match(
    interpretation: SalesInterpretation,
    products: list[PresentedCommerceProduct],
) -> CommerceProductReference | None:
    subject = interpretation.subject
    expected_reference = _fold(subject.reference)
    expected_ean = _fold(subject.ean)
    expected_brand = _fold(subject.brand)
    expected_model = _fold(subject.model)
    if not any((expected_reference, expected_ean, expected_brand, expected_model)):
        return None

    scored: list[tuple[int, PresentedCommerceProduct]] = []
    for product in products:
        if expected_reference and _fold(product.reference) == expected_reference:
            return CommerceProductReference.model_validate(product.model_dump(exclude={"position"}))
        if expected_ean and _fold(product.ean) == expected_ean:
            return CommerceProductReference.model_validate(product.model_dump(exclude={"position"}))
        text = _fold(" ".join(filter(None, (product.name, product.reference, product.brand))))
        score = 0
        if expected_brand:
            if expected_brand not in text:
                continue
            score += 2
        if expected_model:
            tokens = [token for token in expected_model.split() if token]
            matched = sum(1 for token in tokens if token in text)
            if tokens and matched != len(tokens):
                continue
            score += matched * 3
        if score:
            scored.append((score, product))
    scored.sort(key=lambda item: (-item[0], item[1].position))
    if not scored:
        return None
    winner = scored[0][1]
    return CommerceProductReference.model_validate(winner.model_dump(exclude={"position"}))


def resolve_commerce_reference(
    interpretation: SalesInterpretation,
    state: CommerceConversationState,
) -> tuple[CommerceProductReference | None, str]:
    reference_type = interpretation.reference_type
    if reference_type == "list_position" and interpretation.reference_position is not None:
        match = next(
            (
                product
                for product in state.last_presented_products
                if product.position == interpretation.reference_position
            ),
            None,
        )
        if match:
            return (
                CommerceProductReference.model_validate(match.model_dump(exclude={"position"})),
                "product_id",
            )
        return None, "none"
    if reference_type == "last_presented_product" and state.last_presented_products:
        match = state.last_presented_products[-1]
        return CommerceProductReference.model_validate(match.model_dump(exclude={"position"})), "product_id"
    if reference_type == "previous_recommendation" and state.last_presented_products:
        match = state.last_presented_products[0]
        return CommerceProductReference.model_validate(match.model_dump(exclude={"position"})), "product_id"
    if reference_type == "explicit_product":
        match = _explicit_product_match(interpretation, state.last_presented_products)
        return (match, "product_id" if match else "none")
    if reference_type == "current_product" and state.active_product:
        return state.active_product, "product_id"
    return None, "none"


def resolve_purchase_item_reference(
    item: PurchaseItem,
    state: CommerceConversationState,
) -> tuple[CommerceProductReference | None, str]:
    reference_type = item.reference_type
    if reference_type == "list_position" and item.reference_position is not None:
        match = next(
            (
                product
                for product in state.last_presented_products
                if product.position == item.reference_position
            ),
            None,
        )
        if match:
            return (
                CommerceProductReference.model_validate(
                    match.model_dump(exclude={"position"})
                ),
                "list_position",
            )
        return None, "none"
    if reference_type == "current_product" and state.active_product:
        return state.active_product, "active_product"
    if reference_type == "previous_recommendation" and state.last_presented_products:
        match = state.last_presented_products[0]
        return (
            CommerceProductReference.model_validate(
                match.model_dump(exclude={"position"})
            ),
            "previous_recommendation",
        )
    if reference_type == "last_presented_product" and state.last_presented_products:
        match = state.last_presented_products[-1]
        return (
            CommerceProductReference.model_validate(
                match.model_dump(exclude={"position"})
            ),
            "last_presented_product",
        )
    if reference_type == "explicit_product" and item.explicit_product_name:
        expected_tokens = [
            token
            for token in _fold(item.explicit_product_name).split()
            if token
        ]
        matches = []
        for product in state.last_presented_products:
            text = _fold(
                " ".join(
                    filter(None, (product.name, product.reference, product.brand))
                )
            )
            if expected_tokens and all(token in text for token in expected_tokens):
                matches.append(product)
        if len(matches) == 1:
            return (
                CommerceProductReference.model_validate(
                    matches[0].model_dump(exclude={"position"})
                ),
                "explicit_product",
            )
        return None, "ambiguous" if len(matches) > 1 else "none"
    return None, "none"


def apply_commerce_domain_context(
    interpretation: SalesInterpretation,
    state: CommerceConversationState,
) -> tuple[SalesInterpretation, bool]:
    if interpretation._source == "openai":
        return interpretation, False
    previous_domain = state.active_domain
    if (
        previous_domain == "commerce"
        and interpretation.domain != "commerce"
        and interpretation.domain != "greeting"
        and not (
            interpretation._source != "openai"
            and interpretation.domain == "raffle"
        )
        and not interpretation.domain_change_explicit
    ):
        return interpretation.model_copy(update={"domain": "commerce"}), True
    return interpretation, False


def _compact_preferences(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if item not in (None, "", [], {})
    }


def _cart_material_signature(items: Any) -> tuple[tuple[str, str | None, int], ...] | None:
    """Compare only checkout-relevant cart identity, never reconciled prices."""
    if not isinstance(items, list):
        return None
    signature: list[tuple[str, str | None, int]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        product_id = item.get("product_id") or item.get("id")
        if product_id is None:
            return None
        try:
            quantity = int(item.get("quantity") or 1)
        except (TypeError, ValueError):
            return None
        signature.append((
            str(product_id),
            normalize_variant_identity(item.get("variant_id")),
            quantity,
        ))
    return tuple(sorted(signature))


def _cart_state_materially_changed(
    state: CommerceConversationState,
    cart_state: Any,
    metadata: dict[str, Any],
) -> bool:
    """Distinguish cart reconciliation from a mutation that obsoletes checkout."""
    if metadata.get("cart_materially_changed") is True:
        return True
    if not isinstance(cart_state, dict):
        return False
    next_session_id = cart_state.get("cart_session_id")
    if next_session_id and str(next_session_id) != str(state.cart_session_id or ""):
        return True
    incoming = _cart_material_signature(cart_state.get("cart_items"))
    current = _cart_material_signature(
        [item.model_dump(mode="json") for item in state.cart_items]
    )
    return incoming is not None and incoming != current


def _selected_payment_matches_preference(
    state: CommerceConversationState,
    preference: str,
) -> bool:
    """Only retain a factual selection when its semantic method is still current."""
    if preference == "other":
        return False
    selected_method = (
        state.selected_payment_option.method
        if state.selected_payment_option and state.selected_payment_option.method
        else state.selected_payment_method
    )
    return selected_method == preference


def evolve_commerce_state(
    previous: CommerceConversationState,
    result: AgentResult,
) -> CommerceConversationState:
    state = previous.model_copy(deep=True)
    metadata = result.response_metadata or {}
    domain = metadata.get("domain")
    if domain in {"commerce", "raffle"}:
        state.active_domain = domain
    if domain != "commerce":
        return state

    cart_state = metadata.get("cart_state")
    cart_materially_changed = _cart_state_materially_changed(
        state,
        cart_state,
        metadata,
    )
    next_cart_session_id = (
        cart_state.get("cart_session_id")
        if isinstance(cart_state, dict) else None
    )
    starts_new_checkout = bool(
        state.order_id
        and state.order_session_id
        and next_cart_session_id
        and str(next_cart_session_id) != str(state.order_session_id)
        # Never wipe an unpaid order just because another cart mutation arrived.
        and state.pending_action != "awaiting_payment"
        and state.order_payment_status != "pending"
        and not state.order_payment_url
    )
    if starts_new_checkout:
        state.order_id = None
        state.order_status = None
        state.order_status_group = None
        state.order_session_id = None
        state.order_created_at = None
        state.order_creation_ambiguous = False
        state.order_lookup_id = None
        state.order_payment_method_id = None
        state.order_payment_method = None
        state.order_payment_type = None
        state.order_payment_url = None
        state.order_payment_status = "not_available"
        state.order_has_payment = None
        state.order_payment_date = None
        state.order_payment_checked_at = None
        state.order_payment_revalidation_status = "not_checked"
        state.pix_payment_id = None
        state.pix_payment_status = None
        state.pix_copy_paste_code = None
        state.pix_amount_label = None
        state.pix_order_review_version = None

    material_checkout_change = any(
        key in metadata
        for key in (
            "cart_state",
            "selected_payment_option",
            "shipping_state",
            "checkout_state",
            "checkout_channel_preference",
        )
    )
    if (
        material_checkout_change
        and (not state.order_id or starts_new_checkout)
        and "order_state" not in metadata
    ):
        state.order_confirmation_status = "not_ready"
        state.order_review_version = None
        state.confirmed_order_review_version = None
    if cart_materially_changed and (not state.order_id or starts_new_checkout):
        state.shipping_quote_zipcode = None
        state.shipping_quotes = []
        state.selected_shipping = None
        state.selected_payment_method = None
        state.selected_payment_option_id = None
        state.selected_payment_option = None
        state.pix_payment_id = None
        state.pix_payment_status = None
        state.pix_copy_paste_code = None
        state.pix_amount_label = None
        state.pix_order_review_version = None

    if metadata.get("active_topic"):
        state.active_topic = str(metadata["active_topic"])
    if metadata.get("purchase_stage"):
        state.purchase_stage = str(metadata["purchase_stage"])
    if metadata.get("clear_pending_action"):
        state.pending_action = None
        state.pending_action_product_ids = []
    pending_action = metadata.get("pending_action")
    if pending_action in {
        "send_product_link",
        "create_cart",
        "show_images",
        "show_payment_options",
        "confirm_purchase",
        "choose_checkout_channel",
        "awaiting_shipping_zipcode",
        "awaiting_shipping_selection",
        "awaiting_checkout_data",
        "awaiting_order_confirmation",
        "awaiting_payment",
        "awaiting_order_customer_document",
    }:
        state.pending_action = pending_action
        pending_ids = metadata.get("pending_action_product_ids")
        state.pending_action_product_ids = [
            str(item)
            for item in pending_ids
            if item is not None
        ] if isinstance(pending_ids, list) else []
    cart_state = metadata.get("cart_state")
    if isinstance(cart_state, dict):
        for field in (
            "cart_id",
            "cart_session_id",
            "cart_url",
            "cart_product_id",
            "cart_variant_id",
            "cart_quantity",
            "cart_items",
        ):
            if field in cart_state:
                if field == "cart_items" and isinstance(cart_state[field], list):
                    parsed_items: list[CommerceCartItem] = []
                    for item in cart_state[field]:
                        try:
                            parsed_items.append(CommerceCartItem.model_validate(item))
                        except (TypeError, ValueError):
                            continue
                    state.cart_items = parsed_items
                else:
                    setattr(state, field, cart_state[field])
    selected_payment_method = metadata.get("selected_payment_method")
    if selected_payment_method in {"pix", "card", "boleto", "other"}:
        state.selected_payment_method = selected_payment_method
    payment_method_preference = metadata.get("payment_method_preference")
    preference_changed = False
    if payment_method_preference in {"pix", "card", "boleto", "other"}:
        preference_changed = payment_method_preference != state.payment_method_preference
        state.payment_method_preference = payment_method_preference
    if metadata.get("selected_payment_option_id") is not None:
        state.selected_payment_option_id = str(
            metadata["selected_payment_option_id"]
        )
    selected_payment = metadata.get("selected_payment_option")
    if isinstance(selected_payment, dict):
        try:
            state.selected_payment_option = SelectedPaymentOption.model_validate(
                selected_payment
            )
        except (TypeError, ValueError):
            pass
    if (
        preference_changed
        and payment_method_preference is not None
        and not _selected_payment_matches_preference(state, payment_method_preference)
    ):
        state.selected_payment_method = None
        state.selected_payment_option_id = None
        state.selected_payment_option = None
    checkout_channel = metadata.get("checkout_channel_preference")
    if checkout_channel in {"whatsapp", "site"}:
        state.checkout_channel_preference = checkout_channel
    shipping_state = metadata.get("shipping_state")
    if isinstance(shipping_state, dict):
        if "shipping_quote_zipcode" in shipping_state:
            state.shipping_quote_zipcode = shipping_state.get("shipping_quote_zipcode")
        if isinstance(shipping_state.get("shipping_quotes"), list):
            parsed_quotes: list[ShippingQuote] = []
            for quote in shipping_state["shipping_quotes"]:
                try:
                    parsed_quotes.append(ShippingQuote.model_validate(quote))
                except (TypeError, ValueError):
                    continue
            state.shipping_quotes = parsed_quotes
        if "selected_shipping" in shipping_state:
            selected = shipping_state.get("selected_shipping")
            try:
                state.selected_shipping = (
                    ShippingQuote.model_validate(selected)
                    if isinstance(selected, dict) else None
                )
            except (TypeError, ValueError):
                pass
    checkout_state = metadata.get("checkout_state")
    if isinstance(checkout_state, dict):
        draft = checkout_state.get("checkout_draft")
        if isinstance(draft, dict):
            try:
                state.checkout_draft = CheckoutDraft.model_validate(draft)
            except (TypeError, ValueError):
                pass
    order_state = metadata.get("order_state")
    if isinstance(order_state, dict):
        for field in (
            "order_confirmation_status", "order_review_version",
            "confirmed_order_review_version", "order_id", "order_status",
            "order_status_group", "order_session_id", "order_created_at",
            "order_creation_ambiguous", "order_lookup_id",
        ):
            if field in order_state:
                setattr(state, field, order_state[field])
    payment_state = metadata.get("payment_state")
    if isinstance(payment_state, dict):
        for field in (
            "order_payment_method_id", "order_payment_method",
            "order_payment_type", "order_payment_url",
            "order_payment_status", "order_has_payment",
            "order_payment_date", "order_payment_checked_at",
            "order_payment_revalidation_status",
        ):
            if field in payment_state:
                setattr(state, field, payment_state[field])
    pix_state = metadata.get("pix_state")
    if isinstance(pix_state, dict):
        for field in (
            "pix_payment_id",
            "pix_payment_status",
            "pix_copy_paste_code",
            "pix_amount_label",
            "pix_order_review_version",
        ):
            if field in pix_state:
                setattr(state, field, pix_state[field])
    active_preferences = _compact_preferences(metadata.get("active_preferences"))
    if active_preferences:
        state.active_preferences = active_preferences

    if metadata.get("clear_active_product"):
        state.active_product = None
    resolved = metadata.get("active_product")
    if isinstance(resolved, dict):
        try:
            state.active_product = CommerceProductReference.model_validate(resolved)
        except (TypeError, ValueError):
            pass

    products = (result.commercial_data or {}).get("products")
    compact_products: list[PresentedCommerceProduct] = []
    if isinstance(products, list):
        for position, product in enumerate(products[:3], start=1):
            if not isinstance(product, dict):
                continue
            identity = product_reference_from_product(product)
            if identity:
                compact_products.append(
                    PresentedCommerceProduct(position=position, **identity.model_dump())
                )
    if metadata.get("presented_products") and compact_products:
        state.last_presented_products = compact_products
    story_ref = metadata.get("last_story_product")
    if isinstance(story_ref, dict) and story_ref.get("story_media_id"):
        state.last_story_product = story_ref
    if metadata.get("activate_first_product") and compact_products:
        state.active_product = CommerceProductReference.model_validate(
            compact_products[0].model_dump(exclude={"position"})
        )
    resolution_state = metadata.get("product_resolution_state")
    if isinstance(resolution_state, str) and resolution_state.strip():
        state.product_resolution_state = resolution_state.strip()
    elif metadata.get("clear_active_product") and metadata.get("presented_products"):
        # Soft nearby / disambiguation: never treat siblings as confirmed.
        state.product_resolution_state = (
            state.product_resolution_state or "plausible_matches"
        )
    return state
