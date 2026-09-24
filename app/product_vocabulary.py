"""Category cues for Xnamai messages when semantic interpretation is unavailable."""

import re

_CATEGORY = re.compile(
    r"\b(?:fones?|cabos?|carregadores?|carregador|capas?|suportes?|rel[oó]gios?|"
    r"adaptadores?|adaptador|hubs?|pel[ií]culas?|power\s*banks?|"
    r"eletr[oô]nicos?|acess[oó]rios?|caixas?\s+de\s+som)\b",
    re.IGNORECASE,
)


def mentions_product_category(text: str | None) -> bool:
    return bool(_CATEGORY.search(text or ""))


def extract_product_category(text: str | None) -> str | None:
    """Return the category phrase present in a customer message."""
    match = _CATEGORY.search(text or "")
    return match.group(0).casefold() if match else None
