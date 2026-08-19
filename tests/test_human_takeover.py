from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.human_takeover import (
    _candidate_keys,
    _conversas_has_takeover_signal,
    _human_activity_from_row,
    human_takeover_active,
)
from app.models import IncomingMessage


def test_candidate_keys_dedupes():
    msg = IncomingMessage(
        provider="brevo",
        event_type="conversationFragment",
        channel="whatsapp",
        conversation_id="abc",
        sender_key="abc",
        sender_phone="5511999999999",
        visitor_id="vid",
        text="oi",
    )
    keys = _candidate_keys(msg)
    assert keys[0] == "abc"
    assert "5511999999999" in keys
    assert "vid" in keys
    assert len(keys) == len(set(keys))


def test_takeover_signal_ignores_closed():
    assert _conversas_has_takeover_signal({"status": "closed", "assigned_to": "u1"}) is False
    assert _conversas_has_takeover_signal({"status": "open", "assigned_to": "u1"}) is True
    assert _conversas_has_takeover_signal({"status": "open", "bot_activated": False}) is True
    assert _conversas_has_takeover_signal({"status": "open", "bot_activated": True}) is False


def test_human_activity_ignores_customer_last_message():
    assigned = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    assert (
        _human_activity_from_row(
            {
                "assigned_at": assigned,
                "last_message_at": datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc),
            }
        )
        == assigned
    )
    assert (
        _human_activity_from_row(
            {"last_message_at": datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)}
        )
        is None
    )


def test_stuck_assigned_without_activity_allows_bot_when_persist_fails(monkeypatch):
    """If we cannot store the idle clock, never mute permanently."""
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-stuck",
        sender_key="conv-stuck",
        text="oi",
    )
    with (
        patch(
            "app.human_takeover._fetch_conversas_rows",
            return_value=[
                {"assigned_to": "agent-1", "bot_activated": False, "status": "open"}
            ],
        ),
        patch("app.human_takeover._load_pause_state", return_value=None),
        patch(
            "app.human_takeover._upsert_pause_state",
            side_effect=RuntimeError("no table"),
        ),
    ):
        assert human_takeover_active(incoming) is False

    get_settings.cache_clear()


def test_first_observation_mutes_only_when_persisted(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-first",
        sender_key="conv-first",
        text="oi",
    )
    with (
        patch(
            "app.human_takeover._fetch_conversas_rows",
            return_value=[
                {"assigned_to": "agent-1", "bot_activated": False, "status": "open"}
            ],
        ),
        patch("app.human_takeover._load_pause_state", return_value=None),
        patch("app.human_takeover._upsert_pause_state") as upsert,
    ):
        assert human_takeover_active(incoming) is True
        assert upsert.called

    get_settings.cache_clear()

def test_human_takeover_expires_after_idle(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-idle",
        sender_key="conv-idle",
        text="oi de novo",
    )
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    with (
        patch(
            "app.human_takeover._fetch_conversas_rows",
            return_value=[{"assigned_to": "agent-1", "bot_activated": False, "status": "open"}],
        ),
        patch(
            "app.human_takeover._load_pause_state",
            return_value={"last_human_activity_at": old},
        ),
        patch("app.human_takeover._upsert_pause_state"),
    ):
        assert human_takeover_active(incoming) is False

    get_settings.cache_clear()


def test_human_takeover_active_within_idle(monkeypatch):
    monkeypatch.setenv("HUMAN_TAKEOVER_IDLE_MINUTES", "15")
    from app.config import get_settings

    get_settings.cache_clear()

    incoming = IncomingMessage(
        channel="whatsapp",
        conversation_id="conv-active",
        sender_key="conv-active",
        text="ainda com humano",
    )
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    with (
        patch(
            "app.human_takeover._fetch_conversas_rows",
            return_value=[{"assigned_to": "agent-1", "bot_activated": False, "status": "open"}],
        ),
        patch(
            "app.human_takeover._load_pause_state",
            return_value={"last_human_activity_at": recent},
        ),
        patch("app.human_takeover._upsert_pause_state"),
    ):
        assert human_takeover_active(incoming) is True

    get_settings.cache_clear()
