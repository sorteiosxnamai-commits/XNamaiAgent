from pydantic import BaseModel, Field, PrivateAttr
from typing import Any, Literal


class ProductSubject(BaseModel):
    product_type: str | None = None
    brand: str | None = None
    model: str | None = None
    reference: str | None = None
    ean: str | None = None


class ProductPreferences(BaseModel):
    budget_min: float | None = None
    budget_max: float | None = None
    color: str | None = None
    style: str | None = None
    material: str | None = None
    occasion: str | None = None
    recipient: str | None = None
    attributes: list[str] = Field(default_factory=list)
    explicit_no_preferences: list[
        Literal["budget", "brand", "color", "style", "material", "occasion", "recipient", "attributes"]
    ] = Field(default_factory=list)


class PurchaseItem(BaseModel):
    reference_type: Literal[
        "list_position",
        "current_product",
        "previous_recommendation",
        "last_presented_product",
        "explicit_product",
    ] | None = Field(default_factory=lambda: None)
    reference_position: int | None = Field(default_factory=lambda: None, ge=1)
    explicit_product_name: str | None = Field(default_factory=lambda: None)
    quantity: int = Field(default_factory=lambda: 1, ge=1)


class CheckoutDataInput(BaseModel):
    name: str | None = None
    cpf: str | None = None
    email: str | None = None
    phone: str | None = None
    rg: str | None = None
    gender: str | None = None
    address: str | None = None
    zipcode: str | None = None
    number: str | None = None
    complement: str | None = None
    neighborhood: str | None = None
    city: str | None = None
    state: str | None = None


class SalesInterpretation(BaseModel):
    domain: Literal[
        "commerce",
        "raffle",
        "store_general",
        "greeting",
        "out_of_scope",
    ]
    goal: Literal[
        "discover",
        "find",
        "recommend",
        "compare",
        "inspect",
        "buy",
        "after_sales",
    ] | None = None
    subject: ProductSubject = Field(default_factory=ProductSubject)
    preferences: ProductPreferences = Field(default_factory=ProductPreferences)
    information_needed: list[
        Literal["catalog", "price", "inventory", "coupons", "payment"]
    ] = Field(default_factory=list)
    references_previous_context: bool
    enough_information_to_search: bool = Field(default_factory=bool)
    ready_for_retrieval: bool = Field(default_factory=bool)
    stop_clarification: bool = Field(default_factory=bool)
    needs_clarification: bool
    clarification_question: str | None = None
    reference_type: Literal[
        "list_position",
        "current_product",
        "previous_recommendation",
        "last_presented_product",
        "explicit_product",
    ] | None = None
    reference_position: int | None = None
    purchase_action: Literal[
        "create_cart",
        "show_cart_link",
        "checkout_question",
        "inspect_cart",
        "set_cart_item_quantity",
        "remove_cart_item",
    ] | None = Field(default_factory=lambda: None)
    quantity: int | None = Field(default_factory=lambda: None, ge=1)
    purchase_items: list[PurchaseItem] = Field(default_factory=list)
    image_request: bool = Field(default_factory=bool)
    product_action: Literal[
        "get_product_link",
    ] | None = Field(default_factory=lambda: None)
    payment_action: Literal[
        "payment_options",
        "installment",
        "order_payment",
    ] | None = Field(default_factory=lambda: None)
    payment_request_kind: Literal[
        "informational",
        "checkout",
    ] | None = Field(default_factory=lambda: None)
    payment_method_preference: Literal[
        "pix",
        "card",
        "boleto",
        "other",
    ] | None = Field(default_factory=lambda: None)
    checkout_channel_preference: Literal[
        "whatsapp",
        "site",
    ] | None = Field(default_factory=lambda: None)
    payment_option_id: str | None = None
    shipping_action: Literal[
        "quote",
        "select",
        "list_methods",
    ] | None = Field(default_factory=lambda: None)
    shipping_zipcode: str | None = None
    shipping_selection_id: str | None = None
    shipping_selection_position: int | None = Field(default_factory=lambda: None, ge=1)
    checkout_action: Literal[
        "update_data",
        "prepare_order",
        "create_order",
    ] | None = Field(default_factory=lambda: None)
    checkout_data: CheckoutDataInput | None = None
    order_action: Literal[
        "get_order",
        "get_order_status",
        "get_order_tracking",
    ] | None = Field(default_factory=lambda: None)
    order_id: str | None = None
    confirmation: Literal[
        "confirm",
        "reject",
        "none",
    ] = Field(default_factory=lambda: "none")
    installment_count: int | None = Field(default_factory=lambda: None, ge=1)
    active_topic: str | None = None
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
    ] | None = None
    domain_change_explicit: bool = Field(default_factory=bool)
    confidence: float = Field(ge=0.0, le=1.0)

    _source: str = PrivateAttr(default="openai")
    _fallback_reason: str | None = PrivateAttr(default=None)
    _clear_pending_action: bool = PrivateAttr(default=False)
    _turn_understanding: Any = PrivateAttr(default=None)


class IncomingMessage(BaseModel):
    provider: str = "brevo"
    event_type: str | None = None
    message_id: str | None = None
    channel: str = "unknown"
    sender_key: str | None = None
    sender_external_id: str | None = None
    sender_username: str | None = None
    source_channel_ref: str | None = None
    source_channel_link: str | None = None
    source_conversation_ref: str | None = None
    conversation_id: str | None = None
    visitor_id: str | None = None
    sender_phone: str | None = None
    sender_name: str | None = None
    text: str = ""
    input_modality: str = "text"
    attachment_type: str | None = None
    audio_url: str | None = None
    audio_mime_type: str | None = None
    audio_filename: str | None = None
    image_url: str | None = None
    image_mime_type: str | None = None
    transcription_failed: bool = False
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    instagram_story: Any | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    reply_text: str
    intent: str = "general_support"
    confidence: float | None = None
    handoff_required: bool = False
    safety_reason: str | None = None
    reply_modality: str = "text"
    reply_audio_bytes: bytes | None = None
    reply_audio_mime_type: str | None = None
    reply_audio_url: str | None = None
    commercial_data: dict[str, Any] | None = None
    response_metadata: dict[str, Any] = Field(default_factory=dict, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    def with_response_metadata(self, **metadata: Any) -> "AgentResult":
        self.response_metadata.update({key: value for key, value in metadata.items() if value is not None})
        return self


class BrevoSendResult(BaseModel):
    ok: bool
    dry_run: bool = True
    status_code: int | None = None
    provider_response: dict[str, Any] | None = None
    error: str | None = None
