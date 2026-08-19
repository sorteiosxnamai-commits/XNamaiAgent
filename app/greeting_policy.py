"""Avoid redundant replies — especially repeated greetings to the same person."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

GREETING_REPLY = "Olá! Como posso ajudar?"

# Short, distinct follow-ups when a generic greeting was already used recently.
# Order = preference; first unused wins.
_GREETING_VARIANTS = (
    GREETING_REPLY,
    "Oi! Em que posso te ajudar?",
    "Olá! Me conta o que você procura.",
    "Oi! Pode falar, estou aqui.",
    "Olá! Como posso te ajudar agora?",
)

_GREETING_BODY_RE = re.compile(
    r"^\s*(ol[aá]|oi|bom dia|boa tarde|boa noite)[!.,\s]*"
    r"(como posso (te )?ajudar|em que posso (te )?ajudar|"
    r"me conta o que (você|voce) procura|pode falar|"
    r"estou aqui|tudo bem)[!.?\s]*$",
    flags=re.IGNORECASE,
)


def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def is_generic_greeting_reply(text: str | None) -> bool:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return False
    folded = _fold(cleaned)
    if any(folded == _fold(variant) for variant in _GREETING_VARIANTS):
        return True
    return bool(_GREETING_BODY_RE.match(cleaned))


def recent_assistant_replies(
    recent_turns: list[dict[str, Any]] | None,
    *,
    limit: int = 8,
) -> list[str]:
    """Most recent assistant texts for this conversation (newest last)."""
    replies: list[str] = []
    for turn in recent_turns or []:
        if not isinstance(turn, dict) or turn.get("role") != "assistant":
            continue
        content = str(turn.get("content") or "").strip()
        if content:
            replies.append(content)
    return replies[-limit:]


def last_assistant_content(recent_turns: list[dict[str, Any]] | None) -> str | None:
    replies = recent_assistant_replies(recent_turns, limit=1)
    return replies[-1] if replies else None


def already_said(
    candidate: str,
    recent_turns: list[dict[str, Any]] | None,
    *,
    lookback: int = 8,
) -> bool:
    """True if this exact reply (normalized) was already sent to this person."""
    folded = _fold(candidate)
    if not folded:
        return False
    for previous in recent_assistant_replies(recent_turns, limit=lookback):
        if _fold(previous) == folded:
            return True
    return False


def choose_greeting_reply(recent_turns: list[dict[str, Any]] | None = None) -> str:
    """Pick a greeting that was not already sent in this conversation.

    Goal: do not re-send the same phrase to the same person. Prefer a fresh
    variant; if every canned greeting was used, fall back to a short nudge
    that is not in the recent set.
    """
    for variant in _GREETING_VARIANTS:
        if not already_said(variant, recent_turns):
            return variant

    # All canned greetings already used — still avoid repeating the last one.
    fallback = "Pode me dizer o que você precisa?"
    if not already_said(fallback, recent_turns):
        return fallback
    return "Estou aqui — o que você procura?"
