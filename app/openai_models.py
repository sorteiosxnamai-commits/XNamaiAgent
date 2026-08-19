"""Model selection helpers for the OpenAI gateway.

Business code should prefer ``resolve_openai_model(role)`` over hardcoding
model IDs. Roles map to configurable env vars with safe fallbacks.
"""

from __future__ import annotations

from typing import Literal

from .config import get_settings

ModelRole = Literal["main", "fast"]


def resolve_openai_model(role: ModelRole = "main") -> str:
    """Return the configured model for a functional role.

    - ``main``: comprehension + grounded final reply (OPENAI_MAIN_MODEL / OPENAI_MODEL)
    - ``fast``: simple structured tasks (OPENAI_FAST_MODEL → main → OPENAI_MODEL)
    """
    settings = get_settings()
    base = (getattr(settings, "openai_model", None) or "").strip()
    main = (getattr(settings, "openai_main_model", None) or "").strip() or base
    fast = (getattr(settings, "openai_fast_model", None) or "").strip() or main
    if role == "fast":
        chosen = fast
    else:
        chosen = main
    if not chosen:
        raise ValueError("openai_model_not_configured")
    return chosen


def configured_models() -> dict[str, str]:
    """Expose configured model IDs for observability / availability checks."""
    settings = get_settings()
    return {
        "default": (getattr(settings, "openai_model", None) or "").strip(),
        "main": resolve_openai_model("main"),
        "fast": resolve_openai_model("fast"),
        "transcribe": (getattr(settings, "openai_transcribe_model", None) or "").strip(),
        "tts": (getattr(settings, "openai_tts_model", None) or "").strip(),
    }
