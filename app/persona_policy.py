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


def find_volatile_persona_claims(instructions: str) -> list[str]:
    """Return policy violation keys if persona text embeds volatile commerce facts."""
    text = instructions or ""
    hits: list[str] = []
    for key, pattern in _VOLATILE_PATTERNS:
        if pattern.search(text):
            hits.append(key)
    return hits


def assert_persona_instructions_safe(instructions: str) -> None:
    hits = find_volatile_persona_claims(instructions)
    if hits:
        raise ValueError(
            "persona_volatile_facts_forbidden:" + ",".join(hits)
        )
