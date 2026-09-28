"""Persona content policy: tone/identity only — no volatile commercial facts."""

from __future__ import annotations

import re

_VOLATILE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "price_amount",
        re.compile(r"R\$\s*\d", flags=re.IGNORECASE),
    ),
    (
        "stock_quantity",
        re.compile(
            r"\b(?:estoque|disponibilidade)\s*[:=]\s*\d+",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "payment_status_fact",
        re.compile(
            r"\b(?:pedido|pagamento)\s+(?:pago|aprovado|confirmado)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "checkout_url",
        re.compile(
            r"https?://[^\s]+(?:checkout|pagamento|cart|pedido)",
            flags=re.IGNORECASE,
        ),
    ),
)

_APPROVED_STATIC_URLS = frozenset(
    {
        "https://xnamai.meuspedidos.com.br",
        "https://xnamai.meuspedidos.com.br/",
    }
)
_URL_PATTERN = re.compile(r"https?://[^\s]+", flags=re.IGNORECASE)
_VOLATILE_URL_TERM = re.compile(
    r"(?:checkout|pagamento|cart|pedido)", flags=re.IGNORECASE
)


def _contains_unapproved_checkout_url(text: str, pattern: re.Pattern[str]) -> bool:
    del pattern
    for match in _URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}\"'")
        if url not in _APPROVED_STATIC_URLS and _VOLATILE_URL_TERM.search(url):
            return True
    return False


def find_volatile_persona_claims(instructions: str) -> list[str]:
    """Return policy violation keys if persona text embeds volatile commerce facts."""
    text = instructions or ""
    hits: list[str] = []
    for key, pattern in _VOLATILE_PATTERNS:
        matched = (
            _contains_unapproved_checkout_url(text, pattern)
            if key == "checkout_url"
            else bool(pattern.search(text))
        )
        if matched:
            hits.append(key)
    return hits


def assert_persona_instructions_safe(instructions: str) -> None:
    hits = find_volatile_persona_claims(instructions)
    if hits:
        raise ValueError(
            "persona_volatile_facts_forbidden:" + ",".join(hits)
        )
