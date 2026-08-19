"""Detect Story-related customer questions."""

from __future__ import annotations

import re
from typing import Any

from .instagram_story_models import InstagramStoryContext, StoryQuestionType
from .models import IncomingMessage


_PRICE_RE = re.compile(
    r"\b(valor|pre[cç]o|custa|quanto(?:\s+est[aá])?|qto|\$|r\$)\b",
    re.I,
)
_AVAIL_RE = re.compile(
    r"\b(dispon[ií]vel|tem(?:\s+ainda)?|estoque|pronta\s+entrega|em\s+loja)\b",
    re.I,
)
_LINK_RE = re.compile(r"\b(link|url|site|manda\s+o\s+link|envia\s+o\s+link)\b", re.I)
_COLOR_RE = re.compile(r"\b(outra\s+cor|outras\s+cores|cor\s+diferente|cores?)\b", re.I)
_MODEL_RE = re.compile(
    r"\b(modelo|refer[eê]ncia|qual\s+(?:e|é)\s+(?:esse|este)|que\s+rel[oó]gio)\b",
    re.I,
)
_MECH_RE = re.compile(r"\b(autom[aá]tico|quartz|mecanismo|cron[oó]grafo)\b", re.I)
_STORY_HINT_RE = re.compile(r"\b(story|storie|stories)\b", re.I)


def detect_story_question_type(text: str | None) -> StoryQuestionType:
    value = str(text or "").strip()
    if not value:
        return StoryQuestionType.GENERIC
    if _LINK_RE.search(value):
        return StoryQuestionType.PRODUCT_LINK
    if _COLOR_RE.search(value):
        return StoryQuestionType.COLOR_OPTIONS
    if _PRICE_RE.search(value):
        return StoryQuestionType.PRICE
    if _AVAIL_RE.search(value):
        return StoryQuestionType.AVAILABILITY
    if _MECH_RE.search(value) or _MODEL_RE.search(value):
        return StoryQuestionType.PRODUCT_DETAILS
    if _STORY_HINT_RE.search(value):
        return StoryQuestionType.PRODUCT_IDENTIFICATION
    # Ultra-short deixis common on Instagram replies.
    if value.casefold() in {"esse", "este", "isso", "valor?", "quanto?", "tem?", "link"}:
        return StoryQuestionType.GENERIC
    return StoryQuestionType.GENERIC


def has_recoverable_story_context(
    incoming: IncomingMessage,
    *,
    commerce_state: Any | None = None,
) -> bool:
    story = getattr(incoming, "instagram_story", None)
    if isinstance(story, InstagramStoryContext):
        if story.replied_to_story or story.mentioned_in_story or story.story_media_id:
            return True
    if commerce_state is not None:
        if getattr(commerce_state, "last_story_product", None) is not None:
            return True
        if getattr(commerce_state, "active_product", None) is not None:
            return True
        if getattr(commerce_state, "last_presented_products", None):
            return True
    if (incoming.image_url or "").strip():
        return True
    return False


def should_route_story_question(incoming: IncomingMessage) -> bool:
    story = getattr(incoming, "instagram_story", None)
    if not isinstance(story, InstagramStoryContext):
        return False
    if not (story.replied_to_story or story.mentioned_in_story):
        # Still route if explicit story mention in text + media id.
        if not (_STORY_HINT_RE.search(incoming.text or "") and story.story_media_id):
            return False
    return True
