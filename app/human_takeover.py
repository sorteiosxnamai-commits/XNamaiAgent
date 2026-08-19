"""Detecta quando a Central ChatBô assumiu a conversa (humano no comando).

O pause NÃO é permanente: só silencia o bot enquanto houver evidência de
atividade do atendente nos últimos N minutos (default 15). Sem atividade
recente — mesmo com assigned_to preso — o agente volta a atender.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db import ensure_tables, get_conn, to_jsonb
from app.models import IncomingMessage
from app.observability import log_event

logger = logging.getLogger(__name__)


def _candidate_keys(incoming: IncomingMessage) -> list[str]:
    keys: list[str] = []
    for value in (
        incoming.conversation_id,
        incoming.sender_key,
        incoming.sender_phone,
        incoming.visitor_id,
        incoming.source_conversation_ref,
    ):
        text = str(value or "").strip()
        if text and text not in keys:
            keys.append(text)
    return keys


def _primary_state_key(incoming: IncomingMessage) -> str | None:
    keys = _candidate_keys(incoming)
    return keys[0] if keys else None


def _as_aware_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def _idle_minutes() -> int:
    settings = get_settings()
    try:
        minutes = int(getattr(settings, "human_takeover_idle_minutes", 15) or 15)
    except (TypeError, ValueError):
        minutes = 15
    return max(1, min(minutes, 24 * 60))


def _conversas_has_takeover_signal(row: dict[str, Any]) -> bool:
    if row.get("status") == "closed":
        return False
    assigned = row.get("assigned_to")
    if assigned is not None and str(assigned).strip():
        return True
    if row.get("bot_activated") is False:
        return True
    return False


def _fetch_conversas_rows(keys: list[str]) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'conversas'
                LIMIT 1
                """
            )
            if cur.fetchone() is None:
                return []

            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'conversas'
                """
            )
            available = {
                str(row["column_name"])
                for row in (cur.fetchall() or [])
                if row.get("column_name")
            }
            base = ["assigned_to", "bot_activated", "status", "last_message_at"]
            optional = [
                "assigned_at",
                "updated_at",
                "last_human_message_at",
                "human_assumed_at",
                "assumed_at",
            ]
            select_cols = [col for col in base if col in available]
            for col in optional:
                if col in available:
                    select_cols.append(col)
            if "assigned_to" not in select_cols and "bot_activated" not in select_cols:
                return []

            order_col = (
                "last_message_at"
                if "last_message_at" in available
                else ("updated_at" if "updated_at" in available else select_cols[0])
            )
            sql = f"""
                SELECT {", ".join(select_cols)}
                FROM public.conversas
                WHERE
                    external_thread_id = ANY(%s)
                    OR contact_phone = ANY(%s)
                ORDER BY {order_col} DESC NULLS LAST
                LIMIT 8
            """
            cur.execute(sql, (keys, keys))
            return list(cur.fetchall() or [])


def _human_activity_from_row(row: dict[str, Any]) -> datetime | None:
    """Only trust human-specific timestamps — never last_message_at (customer traffic)."""
    now = datetime.now(timezone.utc)
    for key in (
        "last_human_message_at",
        "human_assumed_at",
        "assumed_at",
        "assigned_at",
    ):
        stamped = _as_aware_utc(row.get(key))
        if stamped is not None:
            return min(stamped, now)
    return None


# Back-compat alias used by tests.
_seed_activity_from_row = _human_activity_from_row


def _load_pause_state(state_key: str) -> dict[str, Any] | None:
    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT state_key, last_human_activity_at, takeover_detected_at, metadata
                FROM public.ai_human_takeover_state
                WHERE state_key = %s
                LIMIT 1
                """,
                (state_key,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _upsert_pause_state(
    *,
    state_key: str,
    last_human_activity_at: datetime,
    conversation_key: str | None = None,
    sender_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    ensure_tables()
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_human_takeover_state (
                    state_key, conversation_key, sender_key,
                    last_human_activity_at, takeover_detected_at,
                    updated_at, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (state_key) DO UPDATE SET
                    last_human_activity_at = GREATEST(
                        public.ai_human_takeover_state.last_human_activity_at,
                        EXCLUDED.last_human_activity_at
                    ),
                    conversation_key = COALESCE(
                        EXCLUDED.conversation_key,
                        public.ai_human_takeover_state.conversation_key
                    ),
                    sender_key = COALESCE(
                        EXCLUDED.sender_key,
                        public.ai_human_takeover_state.sender_key
                    ),
                    updated_at = EXCLUDED.updated_at,
                    metadata = COALESCE(public.ai_human_takeover_state.metadata, '{}'::jsonb)
                        || EXCLUDED.metadata
                """,
                (
                    state_key,
                    conversation_key,
                    sender_key,
                    last_human_activity_at,
                    now,
                    now,
                    to_jsonb(metadata or {}),
                ),
            )


def _recent_own_bot_outbound(keys: list[str], *, within_seconds: int = 120) -> bool:
    """True if we just sent an agent reply — avoid treating our echo as human activity."""
    if not keys:
        return False
    ensure_tables()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(5, within_seconds))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM public.ai_agent_responses
                WHERE provider_send_ok = true
                  AND created_at >= %s
                  AND (
                        sender_key = ANY(%s)
                     OR sender_phone = ANY(%s)
                  )
                LIMIT 1
                """,
                (cutoff, keys, keys),
            )
            return cur.fetchone() is not None


def touch_human_activity(incoming: IncomingMessage) -> bool:
    """Refresh idle timer when a Brevo agent/human message arrives.

    Call this on outbound/agent webhooks. Ignores echoes of our own bot sends.
    """
    keys = _candidate_keys(incoming)
    state_key = _primary_state_key(incoming)
    if not state_key:
        return False
    try:
        rows = _fetch_conversas_rows(keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning("human_takeover touch lookup failed: %s", exc)
        return False

    active_rows = [row for row in rows if _conversas_has_takeover_signal(row)]
    if not active_rows:
        return False
    if _recent_own_bot_outbound(keys):
        log_event(
            "human_takeover.touch_skipped",
            {"reason": "own_bot_outbound", "state_key": state_key},
        )
        return False

    now = datetime.now(timezone.utc)
    try:
        _upsert_pause_state(
            state_key=state_key,
            last_human_activity_at=now,
            conversation_key=incoming.conversation_id,
            sender_key=incoming.sender_key,
            metadata={"source": "brevo_agent_webhook"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("human_takeover touch persist failed: %s", exc)
        return False
    log_event(
        "human_takeover.touched",
        {"state_key": state_key, "idle_minutes": _idle_minutes()},
    )
    return True


def human_takeover_active(incoming: IncomingMessage) -> bool:
    """True only while a human is actively handling the thread.

    Requires takeover signal in ChatBô (`assigned_to` / `bot_activated=false`)
    AND recent attendant activity within `human_takeover_idle_minutes`.

    Stuck `assigned_to` without recent human activity does NOT mute the bot.
    """
    keys = _candidate_keys(incoming)
    state_key = _primary_state_key(incoming)
    if not keys or not state_key:
        return False

    try:
        rows = _fetch_conversas_rows(keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning("human_takeover lookup failed: %s", exc)
        return False

    takeover_row = next((row for row in rows if _conversas_has_takeover_signal(row)), None)
    if takeover_row is None:
        return False

    idle = timedelta(minutes=_idle_minutes())
    now = datetime.now(timezone.utc)

    last_activity: datetime | None = None
    activity_source = "none"
    try:
        state = _load_pause_state(state_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("human_takeover state load failed: %s", exc)
        state = None

    if state is not None:
        last_activity = _as_aware_utc(state.get("last_human_activity_at"))
        if last_activity is not None:
            activity_source = "local_state"

    if last_activity is None:
        last_activity = _human_activity_from_row(takeover_row)
        if last_activity is not None:
            activity_source = "conversas"
            try:
                _upsert_pause_state(
                    state_key=state_key,
                    last_human_activity_at=last_activity,
                    conversation_key=incoming.conversation_id,
                    sender_key=incoming.sender_key,
                    metadata={"source": "conversas_seed"},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("human_takeover state seed failed: %s", exc)

    if last_activity is None:
        # First time we observe takeover without human timestamps: start a
        # single 15‑min window — only if we can persist it. If persist fails,
        # fail open so a stuck assigned_to never mutes forever.
        seeded = now
        try:
            _upsert_pause_state(
                state_key=state_key,
                last_human_activity_at=seeded,
                conversation_key=incoming.conversation_id,
                sender_key=incoming.sender_key,
                metadata={"source": "first_observation"},
            )
            last_activity = seeded
            activity_source = "first_observation"
        except Exception as exc:  # noqa: BLE001
            logger.warning("human_takeover first_observation persist failed: %s", exc)
            log_event(
                "human_takeover.allow",
                {
                    "reason": "persist_failed_fail_open",
                    "state_key": state_key,
                    "error_type": type(exc).__name__,
                    "idle_minutes": _idle_minutes(),
                },
            )
            return False

    if last_activity is None:
        log_event(
            "human_takeover.allow",
            {
                "reason": "no_recent_human_activity",
                "state_key": state_key,
                "assigned_to_present": bool(takeover_row.get("assigned_to")),
                "bot_activated": takeover_row.get("bot_activated"),
                "idle_minutes": _idle_minutes(),
            },
        )
        return False

    age = now - last_activity
    if age >= idle:
        log_event(
            "human_takeover.allow",
            {
                "reason": "idle_expired",
                "state_key": state_key,
                "activity_source": activity_source,
                "idle_minutes": _idle_minutes(),
                "age_seconds": int(age.total_seconds()),
            },
        )
        return False

    remaining = int((idle - age).total_seconds())
    log_event(
        "human_takeover.block",
        {
            "state_key": state_key,
            "activity_source": activity_source,
            "assigned_to_present": bool(takeover_row.get("assigned_to")),
            "bot_activated": takeover_row.get("bot_activated"),
            "remaining_seconds": remaining,
            "idle_minutes": _idle_minutes(),
        },
    )
    return True
