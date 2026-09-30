"""Separate operational conversation history from the model prompt window."""

from __future__ import annotations

from typing import Any


def conversation_messages(customer_context: dict, *, limit: int) -> list[dict[str, str]]:
    """Only conversational roles may cross from stored history into model input."""
    turns = customer_context.get("_model_conversation_turns")
    if turns is None:
        turns = customer_context.get("_conversation_turns") or []
    clean = [{"role": turn["role"], "content": turn["content"].strip()}
             for turn in turns if isinstance(turn, dict)
             and turn.get("role") in {"user", "assistant"}
             and isinstance(turn.get("content"), str) and turn["content"].strip()]
    return select_model_history_turns(clean, limit=limit)


def select_model_history_turns(
    turns: list[dict[str, Any]] | None,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return the newest ``limit`` turns for LLM prompts.

    Operational recovery should keep the full loaded window (hard cap).
    The model only receives this sliced list.
    """
    if not turns:
        return []
    safe_limit = max(0, int(limit))
    if safe_limit <= 0:
        return []
    return list(turns)[-safe_limit:]


def count_user_assistant_turns(turns: list[dict[str, Any]] | None) -> dict[str, int]:
    values = turns or []
    return {
        "total": len(values),
        "user": sum(1 for turn in values if turn.get("role") == "user"),
        "assistant": sum(1 for turn in values if turn.get("role") == "assistant"),
    }
