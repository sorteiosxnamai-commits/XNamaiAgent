"""Presentation rules for channel-aware outbound replies (Etapa 7).

Only TECHNICAL presentation lives here: factual "similar product" disclosure,
channel block folding, URL preservation and the zero-question rail for
handoff/out_of_scope (safety). Voice and commercial style — openers, closings,
emoji, how many questions, CTAs — belong to the published persona, never to a
regex editor.

Modes (`AGENT_PRESENTER_MODE`, kept for configuration compatibility):
- thin — the technical pipeline (default)
- full — alias of the same technical pipeline; it used to rewrite style
  (opener/CTA/question surgery) and the emergency rollback forces it, so it
  must not behave as a second persona
- shadow — outbound uses full; metadata records thin preview + diff
"""

from __future__ import annotations

import re
from typing import Any, Literal

from .channel_profiles import ChannelProfile, get_channel_profile
from .config import get_settings
from .models import AgentResult, IncomingMessage


PresenterMode = Literal["full", "thin", "shadow"]

_URL_RE = re.compile(r"https?://[^\s<>()]+", flags=re.IGNORECASE)


def resolve_presenter_mode(
    mode: PresenterMode | str | None = None,
) -> PresenterMode:
    if mode in {"full", "thin", "shadow"}:
        return mode  # type: ignore[return-value]
    from .rollout import resolve_effective_presenter_mode

    configured = resolve_effective_presenter_mode(get_settings())
    if configured in {"full", "thin", "shadow"}:
        return configured  # type: ignore[return-value]
    return "thin"


def limit_questions(text: str, *, max_questions: int = 1) -> str:
    if max_questions < 0:
        return text or ""
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", (text or "").strip())
        if part.strip()
    ]
    kept: list[str] = []
    questions = 0
    for sentence in sentences:
        is_question = "?" in sentence
        if is_question:
            if questions >= max_questions:
                continue
            questions += 1
        kept.append(sentence)
    return " ".join(kept).strip() if kept else (text or "").strip()


def split_whatsapp_blocks(text: str, *, max_blocks: int = 3) -> str:
    chunks = [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]
    if len(chunks) <= max_blocks:
        return text or ""
    head = chunks[: max_blocks - 1]
    tail = " ".join(chunks[max_blocks - 1 :])
    return "\n\n".join([*head, tail]).strip()


def preserve_urls(original: str, rewritten: str) -> str:
    """Ensure presentation rewrites do not truncate URLs present originally."""
    original_urls = _URL_RE.findall(original or "")
    if not original_urls:
        return rewritten
    out = rewritten or ""
    for url in original_urls:
        cleaned = url.rstrip(".,;:!?)]}\"'" )
        if cleaned and cleaned not in out:
            out = f"{out.rstrip()}\n{cleaned}".strip()
    return out


def mark_similar_product_language(text: str, metadata: dict[str, Any]) -> str:
    similar = bool(
        metadata.get("similar_product")
        or metadata.get("match_kind") == "similar"
        or (metadata.get("retrieval") or {}).get("match_kind") == "similar"
    )
    if not similar:
        return text
    lowered = (text or "").casefold()
    if "semelhante" in lowered or "parecido" in lowered or "similar" in lowered:
        return text
    prefix = "Encontrei opções semelhantes (não é o modelo exato).\n"
    return prefix + (text or "").lstrip()


def _max_blocks(profile: ChannelProfile) -> int:
    if profile.channel == "whatsapp":
        return 3
    if profile.channel in {"instagram", "facebook"}:
        return 2
    return 3


def present_reply_text_thin(
    text: str,
    *,
    channel: str | None,
    intent: str | None = None,
    metadata: dict[str, Any] | None = None,
    profile: ChannelProfile | None = None,
) -> str:
    """Minimal presenter: similar mark + blocks + URLs (+ handoff question rail)."""
    profile = profile or get_channel_profile(channel)
    metadata = metadata or {}
    original = text or ""
    value = mark_similar_product_language(original, metadata)
    if intent in {"handoff", "out_of_scope"}:
        value = limit_questions(value, max_questions=0)
    max_blocks = _max_blocks(profile)
    if profile.channel in {"whatsapp", "instagram", "facebook"}:
        value = split_whatsapp_blocks(value, max_blocks=max_blocks)
    return preserve_urls(original, value).strip()


def present_reply_text_full(
    text: str,
    *,
    channel: str | None,
    intent: str | None = None,
    metadata: dict[str, Any] | None = None,
    profile: ChannelProfile | None = None,
) -> str:
    """Same technical pipeline as ``thin`` — no style rewriting (see module doc)."""
    return present_reply_text_thin(
        text,
        channel=channel,
        intent=intent,
        metadata=metadata,
        profile=profile,
    )



def present_reply_text(
    text: str,
    *,
    channel: str | None,
    intent: str | None = None,
    metadata: dict[str, Any] | None = None,
    profile: ChannelProfile | None = None,
    mode: PresenterMode | str | None = None,
) -> str:
    resolved = resolve_presenter_mode(mode)
    # shadow at text-level behaves like full (outbound path); dual-run is in present_agent_result
    if resolved == "thin":
        return present_reply_text_thin(
            text,
            channel=channel,
            intent=intent,
            metadata=metadata,
            profile=profile,
        )
    return present_reply_text_full(
        text,
        channel=channel,
        intent=intent,
        metadata=metadata,
        profile=profile,
    )


def _presentation_diff(full_text: str, thin_text: str) -> dict[str, Any]:
    full_q = (full_text or "").count("?")
    thin_q = (thin_text or "").count("?")
    return {
        "chars_full": len(full_text or ""),
        "chars_thin": len(thin_text or ""),
        "questions_full": full_q,
        "questions_thin": thin_q,
        "questions_dropped_by_full": max(0, thin_q - full_q),
        "texts_differ": (full_text or "").strip() != (thin_text or "").strip(),
    }


def present_agent_result(
    incoming: IncomingMessage,
    result: AgentResult,
    *,
    mode: PresenterMode | str | None = None,
) -> AgentResult:
    profile = get_channel_profile(incoming.channel)
    metadata = dict(result.response_metadata or {})
    resolved = resolve_presenter_mode(mode)
    original = result.reply_text or ""

    thin_text = present_reply_text_thin(
        original,
        channel=incoming.channel,
        intent=result.intent,
        metadata=metadata,
        profile=profile,
    )
    full_text = present_reply_text_full(
        original,
        channel=incoming.channel,
        intent=result.intent,
        metadata=metadata,
        profile=profile,
    )

    if resolved == "thin":
        outbound = thin_text
        applied = "thin"
    elif resolved == "shadow":
        outbound = full_text
        applied = "full"
    else:
        outbound = full_text
        applied = "full"

    result.reply_text = outbound
    max_blocks = _max_blocks(profile)
    presentation: dict[str, Any] = {
        "channel": profile.channel,
        "tone": profile.tone,
        "mode": resolved,
        "applied": applied,
        "max_blocks": max_blocks,
        "max_questions": 0 if result.intent in {"handoff", "out_of_scope"} else 1,
        "rules": (
            ["similar_mark", "preserve_urls", "channel_blocks", "handoff_zero_questions"]
            if applied == "thin"
            else [
                "answer_first",
                "no_generic_opener",
                "one_main_question",
                "preserve_urls",
                "controlled_cta",
            ]
        ),
    }
    if resolved == "shadow":
        presentation["thin_preview"] = thin_text
        presentation["diff"] = _presentation_diff(full_text, thin_text)
        print(
            "[agent.presenter.shadow]",
            {
                "channel": profile.channel,
                "intent": result.intent,
                **presentation["diff"],
            },
        )
    result.response_metadata["presentation"] = presentation
    return result
