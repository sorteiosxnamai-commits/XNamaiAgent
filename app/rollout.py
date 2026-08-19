"""Progressive canary rollout + emergency rollback (Etapa 12).

Ops drive traffic with a single profile (or kill switch). Individual feature
env vars remain authoritative when profile is ``full``. Emergency never
disables factual enforce — only shrinks generative surface area.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from .config import get_settings

RolloutProfile = Literal[
    "shadow",
    "full",
    "canary_5",
    "canary_25",
    "canary_50",
    "canary_100",
    "emergency",
]

CANARY_TRAFFIC: dict[str, float] = {
    "canary_5": 0.05,
    "canary_25": 0.25,
    "canary_50": 0.50,
    "canary_100": 1.0,
}

VALID_PROFILES = frozenset(
    {
        "shadow",
        "full",
        "canary_5",
        "canary_25",
        "canary_50",
        "canary_100",
        "emergency",
    }
)

_alert_lock = threading.Lock()
_alert_window: deque[dict[str, bool]] = deque(maxlen=200)
_last_alerts: list[dict[str, Any]] = []


def resolve_rollout_profile(settings: Any | None = None) -> RolloutProfile:
    cfg = settings or get_settings()
    if bool(getattr(cfg, "agent_emergency_rollback", False)):
        return "emergency"
    raw = str(getattr(cfg, "agent_rollout_profile", "full") or "full").strip().casefold()
    if raw in VALID_PROFILES:
        return raw  # type: ignore[return-value]
    return "full"


def is_emergency(settings: Any | None = None) -> bool:
    return resolve_rollout_profile(settings) == "emergency"


def resolve_openai_api_mode(settings: Any | None = None) -> str:
    cfg = settings or get_settings()
    profile = resolve_rollout_profile(cfg)
    if profile == "emergency":
        if bool(getattr(cfg, "openai_chat_completions_primary_allowed", False)):
            return "chat_completions"
        return "canary"
    if profile == "shadow":
        return "shadow"
    if profile.startswith("canary_"):
        return "canary"
    return str(getattr(cfg, "openai_api_mode", "responses") or "responses").strip()


def resolve_responses_traffic_percent(settings: Any | None = None) -> float:
    cfg = settings or get_settings()
    profile = resolve_rollout_profile(cfg)
    if profile == "emergency":
        return 0.0
    if profile == "shadow":
        # Observe new path without forcing Chat primary; traffic remains configured.
        return float(getattr(cfg, "openai_responses_traffic_percent", 1.0) or 0.0)
    if profile in CANARY_TRAFFIC:
        return CANARY_TRAFFIC[profile]
    return float(getattr(cfg, "openai_responses_traffic_percent", 1.0) or 0.0)


def is_turn_understanding_enabled(settings: Any | None = None) -> bool:
    cfg = settings or get_settings()
    if is_emergency(cfg):
        return False
    return bool(getattr(cfg, "agent_turn_understanding_enabled", True))


def resolve_effective_presenter_mode(settings: Any | None = None) -> str:
    cfg = settings or get_settings()
    if is_emergency(cfg):
        return "full"
    configured = str(getattr(cfg, "agent_presenter_mode", "thin") or "thin").strip().casefold()
    if configured in {"full", "thin", "shadow"}:
        return configured
    return "thin"


def resolve_effective_critique_mode(settings: Any | None = None) -> str:
    cfg = settings or get_settings()
    if is_emergency(cfg):
        return "off"
    return str(getattr(cfg, "agent_critique_mode", "shadow") or "shadow").strip().casefold()


def resolve_effective_judge_mode(settings: Any | None = None) -> str:
    cfg = settings or get_settings()
    if is_emergency(cfg):
        return "off"
    return str(getattr(cfg, "agent_quality_judge_mode", "off") or "off").strip().casefold()


def rollback_env_checklist() -> dict[str, str]:
    """Documented one-shot rollback knobs (set on Vercel; no runtime mutate)."""
    return {
        "AGENT_EMERGENCY_ROLLBACK": "true",
        "AGENT_ROLLOUT_PROFILE": "emergency",
        "OPENAI_API_MODE": "canary",
        "OPENAI_RESPONSES_TRAFFIC_PERCENT": "0",
        "AGENT_TURN_UNDERSTANDING_ENABLED": "false",
        "AGENT_PRESENTER_MODE": "full",
        "AGENT_CRITIQUE_MODE": "off",
        "AGENT_QUALITY_JUDGE_MODE": "off",
        "AGENT_CONVERSATION_SUMMARY_ENABLED": "false",
        "AGENT_CONVERSATION_SUMMARY_IN_PROMPT_ENABLED": "false",
        "AGENT_LEARNING_AUTO_PROMOTE": "false",
        "AGENT_LEARNING_AUTO_ACTIVATE": "false",
        "AGENT_FACTUAL_VALIDATION_MODE": "enforce",
    }


def build_rollout_status(settings: Any | None = None) -> dict[str, Any]:
    cfg = settings or get_settings()
    profile = resolve_rollout_profile(cfg)
    with _alert_lock:
        window_size = len(_alert_window)
        recent_alerts = list(_last_alerts[-5:])
    return {
        "profile": profile,
        "emergency": profile == "emergency",
        "emergency_flag": bool(getattr(cfg, "agent_emergency_rollback", False)),
        "effective_openai_api_mode": resolve_openai_api_mode(cfg),
        "effective_responses_traffic_percent": resolve_responses_traffic_percent(cfg),
        "configured_openai_api_mode": getattr(cfg, "openai_api_mode", None),
        "configured_responses_traffic_percent": getattr(
            cfg, "openai_responses_traffic_percent", None
        ),
        "turn_understanding_enabled": is_turn_understanding_enabled(cfg),
        "presenter_mode": resolve_effective_presenter_mode(cfg),
        "critique_mode": resolve_effective_critique_mode(cfg),
        "quality_judge_mode": resolve_effective_judge_mode(cfg),
        "factual_validation_mode": getattr(
            cfg, "agent_factual_validation_mode", "enforce"
        ),
        "alert_window_samples": window_size,
        "recent_alerts": recent_alerts,
        "rollback_checklist": rollback_env_checklist(),
        "canary_steps": [
            "shadow",
            "canary_5",
            "canary_25",
            "canary_50",
            "canary_100",
            "full",
        ],
    }


@dataclass(frozen=True)
class RolloutAlert:
    code: str
    rate: float
    threshold: float
    samples: int


def reset_rollout_alert_window() -> None:
    with _alert_lock:
        _alert_window.clear()
        _last_alerts.clear()


def observe_turn_for_rollout_alerts(
    quality_event: dict[str, Any] | None,
    *,
    settings: Any | None = None,
) -> list[dict[str, Any]]:
    """Record turn.quality signals; emit alert dicts when rates breach thresholds."""
    cfg = settings or get_settings()
    if not bool(getattr(cfg, "agent_rollout_alert_enabled", True)):
        return []
    event = quality_event or {}
    sample = {
        "fallback": bool(event.get("fallback_reason") or event.get("openai_api_fallback")),
        "factual_invalid": event.get("factual_valid") is False,
        "handoff": bool(event.get("handoff_required")),
        "invented_product": bool(event.get("invented_product")),
        "false_no_result": bool(event.get("false_no_result")),
        "budget_exceeded": bool(event.get("llm_budget_exceeded")),
        "tenant_isolation_breach": bool(event.get("tenant_isolation_breach")),
    }
    window = int(getattr(cfg, "agent_rollout_alert_window", 40) or 40)
    min_samples = int(getattr(cfg, "agent_rollout_alert_min_samples", 20) or 20)
    fallback_threshold = float(
        getattr(cfg, "agent_rollout_fallback_alert_rate", 0.25) or 0.25
    )
    factual_threshold = float(
        getattr(cfg, "agent_rollout_factual_alert_rate", 0.10) or 0.10
    )
    handoff_threshold = float(
        getattr(cfg, "agent_rollout_handoff_alert_rate", 0.40) or 0.40
    )

    alerts: list[RolloutAlert] = []
    global _alert_window
    with _alert_lock:
        # Resize window if config changed.
        if _alert_window.maxlen != window:
            items = list(_alert_window)
            _alert_window = deque(items[-window:], maxlen=window)
        _alert_window.append(sample)
        n = len(_alert_window)
        if n >= min_samples:
            fallback_rate = sum(1 for s in _alert_window if s["fallback"]) / n
            factual_rate = sum(1 for s in _alert_window if s["factual_invalid"]) / n
            handoff_rate = sum(1 for s in _alert_window if s["handoff"]) / n
            if fallback_rate >= fallback_threshold:
                alerts.append(
                    RolloutAlert(
                        "high_openai_fallback_rate",
                        fallback_rate,
                        fallback_threshold,
                        n,
                    )
                )
            if factual_rate >= factual_threshold:
                alerts.append(
                    RolloutAlert(
                        "high_factual_invalid_rate",
                        factual_rate,
                        factual_threshold,
                        n,
                    )
                )
            if handoff_rate >= handoff_threshold:
                alerts.append(
                    RolloutAlert(
                        "high_handoff_rate",
                        handoff_rate,
                        handoff_threshold,
                        n,
                    )
                )
            invented_rate = sum(1 for s in _alert_window if s.get("invented_product")) / n
            if invented_rate >= 0.02:
                alerts.append(
                    RolloutAlert(
                        "invented_product_rate",
                        invented_rate,
                        0.02,
                        n,
                    )
                )
            budget_rate = sum(1 for s in _alert_window if s.get("budget_exceeded")) / n
            if budget_rate >= 0.15:
                alerts.append(
                    RolloutAlert(
                        "llm_budget_exceeded_rate",
                        budget_rate,
                        0.15,
                        n,
                    )
                )
            if any(s.get("tenant_isolation_breach") for s in _alert_window):
                alerts.append(
                    RolloutAlert(
                        "tenant_isolation_breach",
                        1.0,
                        0.0,
                        n,
                    )
                )
        payload = [
            {
                "code": a.code,
                "rate": round(a.rate, 4),
                "threshold": a.threshold,
                "samples": a.samples,
                "profile": resolve_rollout_profile(cfg),
            }
            for a in alerts
        ]
        if payload:
            _last_alerts.extend(payload)
            del _last_alerts[:-20]
    for item in payload:
        print("[rollout.alert]", item)
    return payload
