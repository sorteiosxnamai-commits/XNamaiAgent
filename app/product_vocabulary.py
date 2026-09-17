"""Category cues for Xnamai messages when semantic interpretation is unavailable."""

import re

_CATEGORY = re.compile(
    r"\b(?:fones?|cabos?|carregadores?|carregador|capas?|suportes?|"
    r"adaptadores?|adaptador|pel[ií]culas?|power\s*banks?|"
    r"eletr[oô]nicos?|acess[oó]rios?|caixas?\s+de\s+som)\b",
    re.IGNORECASE,
)


def mentions_product_category(text: str | None) -> bool:
    return bool(_CATEGORY.search(text or ""))
