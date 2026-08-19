"""Etapa 12 — progressive canary + emergency rollback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.openai_gateway import CanaryOpenAIGateway, build_openai_gateway, reset_openai_gateway
from app.openai_routing import select_api_route
from app.response_presenter import resolve_presenter_mode
from app.rollout import (
    build_rollout_status,
    is_turn_understanding_enabled,
    observe_turn_for_rollout_alerts,
    reset_rollout_alert_window,
    resolve_openai_api_mode,
    resolve_responses_traffic_percent,
    resolve_rollout_profile,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_openai_gateway()
    reset_rollout_alert_window()
    yield
    reset_openai_gateway()
    reset_rollout_alert_window()


def _cfg(**kwargs):
    base = dict(
        agent_rollout_profile="full",
        agent_emergency_rollback=False,
        openai_api_mode="responses",
        openai_responses_traffic_percent=1.0,
        openai_chat_completions_primary_allowed=False,
        agent_turn_understanding_enabled=True,
        agent_presenter_mode="thin",
        agent_critique_mode="shadow",
        agent_quality_judge_mode="off",
        agent_factual_validation_mode="enforce",
        agent_rollout_alert_enabled=True,
        agent_rollout_alert_window=40,
        agent_rollout_alert_min_samples=20,
        agent_rollout_fallback_alert_rate=0.25,
        agent_rollout_factual_alert_rate=0.10,
        agent_rollout_handoff_alert_rate=0.40,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_full_profile_uses_configured_mode():
    cfg = _cfg(openai_api_mode="responses", openai_responses_traffic_percent=1.0)
    assert resolve_rollout_profile(cfg) == "full"
    assert resolve_openai_api_mode(cfg) == "responses"
    assert resolve_responses_traffic_percent(cfg) == 1.0


def test_canary_5_forces_canary_mode_and_traffic():
    cfg = _cfg(agent_rollout_profile="canary_5", openai_api_mode="responses")
    assert resolve_openai_api_mode(cfg) == "canary"
    assert resolve_responses_traffic_percent(cfg) == 0.05


@pytest.mark.parametrize(
    "profile,traffic",
    [
        ("canary_5", 0.05),
        ("canary_25", 0.25),
        ("canary_50", 0.50),
        ("canary_100", 1.0),
    ],
)
def test_canary_steps(profile, traffic):
    cfg = _cfg(agent_rollout_profile=profile)
    assert resolve_responses_traffic_percent(cfg) == traffic


def test_emergency_flag_overrides_profile():
    cfg = _cfg(agent_rollout_profile="canary_50", agent_emergency_rollback=True)
    assert resolve_rollout_profile(cfg) == "emergency"
    assert resolve_responses_traffic_percent(cfg) == 0.0
    assert is_turn_understanding_enabled(cfg) is False


def test_emergency_presenter_full(monkeypatch):
    monkeypatch.setattr(
        "app.response_presenter.get_settings",
        lambda: _cfg(agent_emergency_rollback=True, agent_presenter_mode="thin"),
    )
    monkeypatch.setattr(
        "app.rollout.get_settings",
        lambda: _cfg(agent_emergency_rollback=True, agent_presenter_mode="thin"),
    )
    assert resolve_presenter_mode() == "full"


def test_emergency_prefers_chat_when_allowed():
    cfg = _cfg(
        agent_emergency_rollback=True,
        openai_chat_completions_primary_allowed=True,
    )
    assert resolve_openai_api_mode(cfg) == "chat_completions"


def test_select_api_route_uses_rollout_traffic(monkeypatch):
    monkeypatch.setattr(
        "app.openai_routing.get_settings",
        lambda: _cfg(agent_rollout_profile="canary_5", openai_canary_sticky_routing=True),
    )
    monkeypatch.setattr(
        "app.rollout.get_settings",
        lambda: _cfg(agent_rollout_profile="canary_5", openai_canary_sticky_routing=True),
    )
    # Explicit override still wins.
    assert select_api_route(routing_key="k", traffic_percent=0.0) == "chat_completions"
    assert select_api_route(routing_key="k", traffic_percent=1.0) == "responses"


def test_build_gateway_respects_canary_profile(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: _cfg(
            agent_rollout_profile="canary_25",
            openai_api_mode="responses",
            openai_responses_fallback_to_chat=True,
        ),
    )
    monkeypatch.setattr(
        "app.rollout.get_settings",
        lambda: _cfg(agent_rollout_profile="canary_25"),
    )
    assert isinstance(build_openai_gateway(), CanaryOpenAIGateway)


def test_rollout_status_includes_checklist():
    status = build_rollout_status(_cfg())
    assert status["profile"] == "full"
    assert status["emergency"] is False
    assert "AGENT_EMERGENCY_ROLLBACK" in status["rollback_checklist"]
    assert status["canary_steps"][0] == "shadow"
    assert "canary_5" in status["canary_steps"]
    assert status["canary_steps"][-1] == "full"


def test_fallback_alert_fires_when_window_breached():
    cfg = _cfg(
        agent_rollout_alert_window=20,
        agent_rollout_alert_min_samples=20,
        agent_rollout_fallback_alert_rate=0.25,
    )
    alerts = []
    for _ in range(20):
        alerts = observe_turn_for_rollout_alerts(
            {"fallback_reason": "openai_responses_canary_fallback", "factual_valid": True},
            settings=cfg,
        )
    assert any(a["code"] == "high_openai_fallback_rate" for a in alerts)


def test_no_alert_below_min_samples():
    cfg = _cfg(
        agent_rollout_alert_window=40,
        agent_rollout_alert_min_samples=20,
        agent_rollout_fallback_alert_rate=0.25,
    )
    for _ in range(10):
        alerts = observe_turn_for_rollout_alerts(
            {"fallback_reason": "x", "factual_valid": True},
            settings=cfg,
        )
    assert alerts == []
