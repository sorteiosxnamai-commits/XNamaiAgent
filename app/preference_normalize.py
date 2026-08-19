"""Normalize interpreter preferences (gender, budget) before catalog retrieval."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .models import ProductPreferences, SalesInterpretation

_FEMALE_TERMS = frozenset(
    {
        "feminino",
        "feminina",
        "femininos",
        "femininas",
        "mulher",
        "mulheres",
        "dama",
        "damas",
        "lady",
        "ladies",
        "women",
        "woman",
        "ela",
        "esposa",
        "namorada",
        "mae",
        "mãe",
        "filha",
        "senhora",
        "senhoras",
    }
)
_MALE_TERMS = frozenset(
    {
        "masculino",
        "masculina",
        "masculinos",
        "masculinas",
        "homem",
        "homens",
        "men",
        "man",
        "ele",
        "marido",
        "namorado",
        "pai",
        "filho",
        "senhor",
        "senhores",
    }
)
_UNISEX_TERMS = frozenset({"unissex", "unisex", "neutro", "neutra"})

_GENDER_ALIASES: dict[str, tuple[str, ...]] = {
    "feminino": (
        "feminino",
        "feminina",
        "lady",
        "ladies",
        "women",
        "woman",
        "dama",
        "damas",
    ),
    "masculino": (
        "masculino",
        "masculina",
        "men",
        "man",
        "homem",
        "homens",
    ),
    "unissex": ("unissex", "unisex"),
}


def _fold(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _tokens(value: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", _fold(value))


def detect_gender_label(*parts: Any) -> str | None:
    """Return canonical feminino|masculino|unissex from free text fragments."""
    found: set[str] = set()
    for part in parts:
        for token in _tokens(part):
            if token in _FEMALE_TERMS or token.startswith("femin"):
                found.add("feminino")
            elif token in _MALE_TERMS or token.startswith("mascul"):
                found.add("masculino")
            elif token in _UNISEX_TERMS:
                found.add("unissex")
    if "feminino" in found and "masculino" not in found:
        return "feminino"
    if "masculino" in found and "feminino" not in found:
        return "masculino"
    if found == {"unissex"}:
        return "unissex"
    return None


def gender_search_aliases(label: str | None) -> tuple[str, ...]:
    if not label:
        return ()
    return _GENDER_ALIASES.get(label, (label,))


def preference_gender_label(interpretation: SalesInterpretation) -> str | None:
    preferences = interpretation.preferences
    return detect_gender_label(
        preferences.recipient,
        preferences.style,
        preferences.occasion,
        *preferences.attributes,
        interpretation.subject.model,
        interpretation.subject.product_type,
    )


def _strip_gender_tokens(value: str | None) -> str | None:
    if not value:
        return None
    kept = [
        token
        for token in re.findall(r"[A-Za-zÀ-ú0-9]+", value)
        if _fold(token) not in (_FEMALE_TERMS | _MALE_TERMS | _UNISEX_TERMS)
        and not _fold(token).startswith(("femin", "mascul"))
    ]
    cleaned = " ".join(kept).strip(" ,-")
    return cleaned or None


def is_gender_only_label(value: str | None) -> bool:
    """True when the whole string is only a gender cue (never a product model)."""
    if not value or not str(value).strip():
        return False
    return detect_gender_label(value) is not None and _strip_gender_tokens(value) is None


def _ensure_attribute(preferences: ProductPreferences, label: str) -> None:
    existing = {_fold(item) for item in preferences.attributes}
    if _fold(label) not in existing:
        preferences.attributes = [*preferences.attributes, label]


def normalize_sales_interpretation(
    interpretation: SalesInterpretation,
    *,
    message_text: str | None = None,
) -> SalesInterpretation:
    """Fix gender misclassified as model/style and keep recommendation mode."""
    preferences = interpretation.preferences
    subject = interpretation.subject
    gender = detect_gender_label(
        message_text,
        preferences.recipient,
        preferences.style,
        preferences.occasion,
        *preferences.attributes,
        subject.model,
        subject.product_type,
    )
    if gender:
        preferences.recipient = gender
        _ensure_attribute(preferences, gender)
        # Gender is never a catalog model identity — that forces exact mode and fails.
        if detect_gender_label(subject.model) == gender and not _strip_gender_tokens(
            subject.model
        ):
            subject.model = None
        else:
            subject.model = _strip_gender_tokens(subject.model)
        style_gender = detect_gender_label(preferences.style)
        if style_gender == gender:
            preferences.style = _strip_gender_tokens(preferences.style)

    # Pull budget from free text when interpreter missed it.
    if preferences.budget_max is None and message_text:
        budget = _extract_budget_max(message_text)
        if budget is not None:
            preferences.budget_max = budget

    # After gender + budget, discovery answers are usually ready to search.
    if (
        interpretation.domain == "commerce"
        and subject.product_type
        and gender
        and interpretation.goal in {None, "discover", "recommend", "find"}
        and not interpretation.needs_clarification
    ):
        interpretation.enough_information_to_search = True
        interpretation.ready_for_retrieval = True
        if interpretation.goal in {None, "discover"}:
            interpretation.goal = "recommend"

    if (
        interpretation.domain == "commerce"
        and subject.product_type
        and (preferences.budget_max is not None or preferences.budget_min is not None)
        and gender
    ):
        interpretation.enough_information_to_search = True
        interpretation.ready_for_retrieval = True
        interpretation.needs_clarification = False
        if interpretation.goal in {None, "discover"}:
            interpretation.goal = "recommend"

    return interpretation


def _extract_budget_max(text: str) -> float | None:
    match = re.search(
        r"(?:at[eé]|ate|menos de|no m[aá]ximo|at[eé] uns?|por at[eé])\s*"
        r"(?:r\$\s*)?([\d.,]+)\s*(mil|k)?",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(?:r\$\s*)?([\d.,]+)\s*(mil|k)?\s*(?:reais|real)?",
            text or "",
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        # Avoid treating bare years/codes as budget without currency cue.
        raw_text = (text or "").lower()
        if "real" not in raw_text and "r$" not in raw_text and "mil" not in raw_text:
            if not re.search(r"at[eé]|ate|menos|m[aá]ximo", raw_text):
                return None
    raw = match.group(1).replace(".", "").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    if match.group(2):
        value *= 1000
    if value <= 0 or value > 1_000_000:
        return None
    return value
