"""Conversation summary persistence (opt-in via feature flag)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import get_conn, to_jsonb
from .memory_models import ConversationSummaryDelta


def merge_summary_state(
    existing: dict[str, Any] | None,
    delta: ConversationSummaryDelta,
    *,
    replace_open_questions: bool = False,
    replace_corrections: bool = False,
    reset_context: bool = False,
    max_chars: int = 2500,
) -> dict[str, Any]:
    """Merge durable context, replacing current questions and keyed preferences.

    Lists are bounded; a new correction supersedes previous interpretations.
    Empty question snapshots intentionally clear answered questions.
    """
    old = {} if reset_context else dict(existing or {})

    def merge(previous, incoming, limit=12):
        values = []
        for item in list(previous or []) + list(incoming or []):
            value = str(item).strip()[:240]
            if value.startswith("preference:") and "=" in value:
                prefix = value.split("=", 1)[0] + "="
                values = [old_value for old_value in values if not old_value.startswith(prefix)]
            if value and value not in values:
                values.append(value)
        return values[-limit:]

    # Preferences use a controlled key, so "color: blue" cannot survive a
    # later "color: black". Other resolved points are continuity facts only.
    preference_prefixes = {
        value.split("=", 1)[0] + "=" for value in delta.resolved_points
        if value.startswith("preference:") and "=" in value
    }
    previous_resolved = [] if delta.user_corrections else old.get("resolved_points", [])
    previous_resolved = [value for value in previous_resolved
                         if not any(str(value).startswith(prefix) for prefix in preference_prefixes)]
    resolved = merge(previous_resolved, delta.resolved_points)
    open_q = merge([] if replace_open_questions else old.get("open_questions"), delta.open_questions)
    corrections = merge([] if replace_corrections else old.get("user_corrections"), delta.user_corrections)
    commitments = merge([] if delta.user_corrections else old.get("commitments"), delta.commitments)
    current_goal = (delta.current_goal or old.get("current_goal") or "")[:240] or None
    last_failure = delta.last_failure or old.get("last_failure")
    parts = [f"goal={current_goal}" if current_goal else "",
             f"open={'; '.join(open_q[:4])}" if open_q else "",
             f"resolved={'; '.join(resolved[-6:])}" if resolved else ""]
    summary = "; ".join(part for part in parts if part)[:max(0, max_chars)]
    return {"current_goal": current_goal, "summary": summary, "resolved_points": resolved,
            "open_questions": open_q, "user_corrections": corrections,
            "commitments": commitments, "last_failure": last_failure,
            "approximate_token_count": max(1, len(summary) // 4) if summary else 0}


def get_conversation_summary(
    *,
    tenant_id: str,
    conversation_key: str,
) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_conversation_summaries
                WHERE tenant_id = %s
                  AND conversation_key = %s
                LIMIT 1
                """,
                (tenant_id, conversation_key),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def apply_summary_delta(
    *,
    tenant_id: str,
    conversation_key: str,
    delta: ConversationSummaryDelta,
    inbound_id: int | None = None,
    response_id: int | None = None,
    max_chars: int = 2500,
    replace_open_questions: bool = False,
    replace_corrections: bool = False,
    reset_context: bool = False,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    existing = get_conversation_summary(
        tenant_id=tenant_id,
        conversation_key=conversation_key,
    )

    if inbound_id is not None and (existing or {}).get("last_inbound_id") is not None:
        if int(inbound_id) <= int(existing["last_inbound_id"]):
            return existing
    state = merge_summary_state(existing, delta,
                                replace_open_questions=replace_open_questions,
                                replace_corrections=replace_corrections,
                                reset_context=reset_context, max_chars=max_chars)
    if existing and all(existing.get(key) == value for key, value in state.items()
                        if key != "approximate_token_count"):
        return existing
    current_goal, summary = state["current_goal"], state["summary"]
    resolved, open_q = state["resolved_points"], state["open_questions"]
    corrections, commitments = state["user_corrections"], state["commitments"]
    last_failure, token_approx = state["last_failure"], state["approximate_token_count"]

    with get_conn() as conn:
        with conn.cursor() as cur:
            if existing:
                cur.execute(
                    """
                    UPDATE public.ai_conversation_summaries
                    SET version = version + 1,
                        current_goal = %s,
                        summary = %s,
                        resolved_points = %s,
                        open_questions = %s,
                        user_corrections = %s,
                        commitments = %s,
                        last_failure = %s,
                        last_inbound_id = %s,
                        last_response_id = %s,
                        approximate_token_count = %s,
                        updated_at = %s
                    WHERE tenant_id = %s
                      AND conversation_key = %s
                    RETURNING *
                    """,
                    (
                        current_goal,
                        summary,
                        to_jsonb(resolved),
                        to_jsonb(open_q),
                        to_jsonb(corrections),
                        to_jsonb(commitments),
                        last_failure,
                        inbound_id,
                        response_id,
                        token_approx,
                        now,
                        tenant_id,
                        conversation_key,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO public.ai_conversation_summaries (
                        tenant_id, conversation_key, current_goal, summary,
                        resolved_points, open_questions, user_corrections,
                        commitments, last_failure, last_inbound_id,
                        last_response_id, approximate_token_count
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        tenant_id,
                        conversation_key,
                        current_goal,
                        summary,
                        to_jsonb(resolved),
                        to_jsonb(open_q),
                        to_jsonb(corrections),
                        to_jsonb(commitments),
                        last_failure,
                        inbound_id,
                        response_id,
                        token_approx,
                    ),
                )
            row = cur.fetchone()
    return dict(row or {})
