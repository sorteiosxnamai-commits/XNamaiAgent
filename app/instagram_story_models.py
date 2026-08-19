"""Instagram Story ↔ product association models (v8).

Vision may describe and select among real catalog candidates only.
Tray remains the sole authority for price/stock/URL.
Private media URLs use SecretStr and must never enter logs or admin responses.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, model_serializer


class StoryQuestionType(str, Enum):
    PRICE = "price"
    AVAILABILITY = "availability"
    PRODUCT_IDENTIFICATION = "product_identification"
    PRODUCT_DETAILS = "product_details"
    PRODUCT_LINK = "product_link"
    COLOR_OPTIONS = "color_options"
    COMPARISON = "comparison"
    GENERIC = "generic"


class SafeMediaReference(BaseModel):
    """Observability-only media reference — never includes signed query params."""

    present: bool = False
    host: str | None = None
    path_hash: str | None = None


class VisualProductRegion(BaseModel):
    position: Literal[
        "left",
        "center",
        "right",
        "top",
        "bottom",
        "unknown",
    ] = "unknown"
    label: str = ""
    dial_color: str | None = None
    strap_color: str | None = None
    brand_hypothesis: str | None = None
    reference_hypothesis: str | None = None


class StoryMediaItem(BaseModel):
    index: int = 0
    media_id: str | None = None
    media_type: str = "unknown"
    media_url_private: SecretStr | None = Field(default=None, exclude=True, repr=False)
    thumbnail_url_private: SecretStr | None = Field(default=None, exclude=True, repr=False)
    media_log_reference: SafeMediaReference | None = None
    thumbnail_log_reference: SafeMediaReference | None = None
    sha256: str | None = None
    analysis: dict[str, Any] | None = None

    def operational_media_url(self) -> str | None:
        if self.media_url_private is None:
            return None
        return self.media_url_private.get_secret_value()

    def operational_thumbnail_url(self) -> str | None:
        if self.thumbnail_url_private is None:
            return None
        return self.thumbnail_url_private.get_secret_value()


class InstagramStoryContext(BaseModel):
    provider: str = "brevo"
    instagram_account_id: str = ""
    story_media_id: str | None = None
    story_message_id: str | None = None
    story_permalink: str | None = None

    # Operational URLs — preserve signed query; never log / dump / return to admin.
    story_media_url_private: SecretStr | None = Field(default=None, exclude=True, repr=False)
    story_thumbnail_url_private: SecretStr | None = Field(
        default=None, exclude=True, repr=False
    )
    story_media_log_reference: SafeMediaReference | None = None
    story_thumbnail_log_reference: SafeMediaReference | None = None

    media_type: Literal["image", "video", "carousel", "unknown"] = "unknown"
    replied_to_story: bool = False
    mentioned_in_story: bool = False
    source_timestamp: datetime | None = None
    expires_at: datetime | None = None
    media_items: list[StoryMediaItem] = Field(default_factory=list)
    raw_reference: dict[str, Any] = Field(default_factory=dict)

    # Backward-compatible aliases used by older call sites (never expose secrets).
    @property
    def story_media_url(self) -> str | None:
        return self.operational_media_url()

    @property
    def story_thumbnail_url(self) -> str | None:
        return self.operational_thumbnail_url()

    def operational_media_url(self) -> str | None:
        if self.story_media_url_private is None:
            return None
        return self.story_media_url_private.get_secret_value()

    def operational_thumbnail_url(self) -> str | None:
        if self.story_thumbnail_url_private is None:
            return None
        return self.story_thumbnail_url_private.get_secret_value()

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        # Hard strip any accidental private keys.
        if isinstance(data, dict):
            data.pop("story_media_url_private", None)
            data.pop("story_thumbnail_url_private", None)
            data.pop("story_media_url", None)
            data.pop("story_thumbnail_url", None)
        return data


class StoryVisualUnderstanding(BaseModel):
    media_type: str = "image"
    detected_product_category: str | None = None
    visible_brands: list[str] = Field(default_factory=list)
    visible_references: list[str] = Field(default_factory=list)
    visible_skus: list[str] = Field(default_factory=list)
    visible_eans: list[str] = Field(default_factory=list)
    visible_text: list[str] = Field(default_factory=list)
    watch_count: int = 0
    multiple_products: bool = False
    product_regions: list[VisualProductRegion] = Field(default_factory=list)
    dial_colors: list[str] = Field(default_factory=list)
    strap_colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    strap_types: list[str] = Field(default_factory=list)
    case_shapes: list[str] = Field(default_factory=list)
    mechanisms_suggested: list[str] = Field(default_factory=list)
    logo_hypotheses: list[str] = Field(default_factory=list)
    collection_hypotheses: list[str] = Field(default_factory=list)
    model_hypotheses: list[str] = Field(default_factory=list)
    visual_description: str = ""
    readable_text_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    product_identity_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    image_quality: Literal["poor", "usable", "good"] = "usable"
    ambiguity_reasons: list[str] = Field(default_factory=list)
    visible_advertised_price: str | None = None


class StoryCandidateScore(BaseModel):
    catalog_item_key: str
    product_id: str
    variant_id: str | None = None
    exact_identifier_score: float = 0.0
    visual_similarity_score: float = 0.0
    lexical_score: float = 0.0
    brand_score: float = 0.0
    color_score: float = 0.0
    model_score: float = 0.0
    image_quality_penalty: float = 0.0
    conflict_penalty: float = 0.0
    final_score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    source: str = "catalog"


class StoryProductCandidate(BaseModel):
    catalog_item_key: str
    product_id: str
    variant_id: str | None = None
    score: float = 0.0
    match_reasons: list[str] = Field(default_factory=list)
    mismatch_reasons: list[str] = Field(default_factory=list)
    source: str = "catalog"
    score_components: StoryCandidateScore | None = None


class StoryProductAssociation(BaseModel):
    id: int | None = None
    tenant_id: str
    provider: str = "brevo"
    instagram_account_id: str
    story_media_id: str
    story_message_id: str | None = None
    story_permalink: str | None = None
    media_type: str = "unknown"
    source_timestamp: datetime | None = None
    story_expires_at: datetime | None = None
    media_storage_path: str | None = None
    media_sha256: str | None = None
    thumbnail_sha256: str | None = None
    analysis_version: str | None = None
    catalog_item_key: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    match_source: str = "pending"
    match_status: str = "pending"
    match_confidence: float = 0.0
    visual_analysis: dict[str, Any] = Field(default_factory=dict)
    candidate_products: list[dict[str, Any]] = Field(default_factory=list)
    match_explanation: dict[str, Any] = Field(default_factory=dict)
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    processing_started_at: datetime | None = None
    processing_owner: str | None = None
    processing_attempts: int = 0
    processing_expires_at: datetime | None = None
    last_failure_code: str | None = None
    next_retry_at: datetime | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class StoryConversationReference(BaseModel):
    story_media_id: str
    tenant_id: str | None = None
    catalog_item_key: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    match_status: str = "pending"
    confidence: float = 0.0
    resolved_at: datetime | None = None


class StoryResolutionResult(BaseModel):
    resolved: bool = False
    tenant_id: str = ""
    story_media_id: str = ""
    match_status: str = "pending"
    catalog_item_key: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    confidence: float = 0.0
    candidates: list[StoryProductCandidate] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_options: list[str] = Field(default_factory=list)
    factual_evidence: list[dict[str, Any]] = Field(default_factory=list)
    failure_reason: str | None = None
    question_type: StoryQuestionType = StoryQuestionType.GENERIC
    reply_hint: str | None = None
    product_payload: dict[str, Any] | None = None
    shadow_only: bool = False
    metrics: dict[str, Any] = Field(default_factory=dict)
    resolved_at: datetime | None = None
