"""Per-turn LLM spend policy (Etapa 6).

Normal turns: interpret + grounded reply (≤2). Extra LLM (critique/judge)
only when risk signals justify it — never as a substitute for deterministic
factual validation.
"""

from __future__ import annotations

import random
from typing import Any, Literal

from .config import get_settings
from .models import AgentResult, IncomingMessage
from .quality_judge import collect_judge_risk_signals, is_low_risk_judge_skip


# Signals that justify an extra LLM critique/judge call.
_CRITIQUE_TRIGGER_SIGNALS = frozenset(
    {
        "factual_validation_failed",
        "high_risk_score",
        "low_interpretation_confidence",
        "side_effect_action",
        "order_prepared_or_confirmed",
        "payment_consulted",
        "unsupported_or_missing_evidence",
        "conflicting_tool_or_fact_claims",
        "tool_or_safety_failure",
        "fallback_after_partial_tooling",
        "reply_contains_payment_link",
    }
)


def critique_risk_signals(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    risk_score: int = 0,
    factual_valid: bool = True,
    openai_call_count: int = 0,
) -> list[str]:
    skip, _reason = is_low_risk_judge_skip(incoming, result)
    if skip:
        return []
    settings = get_settings()
    threshold = int(
        getattr(settings, "agent_quality_judge_risk_threshold", 70) or 70
    )
    return collect_judge_risk_signals(
        result=result,
        risk_score=risk_score,
        factual_valid=factual_valid,
        openai_call_count=openai_call_count,
        threshold=threshold,
    )


def should_run_llm_critique(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    critique_mode: str,
    risk_score: int = 0,
    factual_valid: bool = True,
    openai_call_count: int = 0,
) -> tuple[bool, str, list[str]]:
    """Decide whether to spend an LLM call on response critique.

    Returns (run, reason, signals).
    """
    if critique_mode == "off":
        return False, "critique_mode_off", []

    settings = get_settings()
    risk_only = bool(
        getattr(settings, "agent_critique_llm_on_risk_only", True)
    )
    signals = critique_risk_signals(
        incoming=incoming,
        result=result,
        risk_score=risk_score,
        factual_valid=factual_valid,
        openai_call_count=openai_call_count,
    )
    trigger_hits = [s for s in signals if s in _CRITIQUE_TRIGGER_SIGNALS]

    if not risk_only:
        # Legacy: always LLM after fast path (expensive).
        return True, "critique_always", signals

    if trigger_hits:
        return True, f"risk:{trigger_hits[0]}", signals

    # Controlled sampling (shadow/enforce) without risk.
    try:
        sample_rate = float(
            getattr(settings, "agent_critique_shadow_sample_rate", 0.0) or 0.0
        )
    except (TypeError, ValueError):
        sample_rate = 0.0
    sample_rate = max(0.0, min(1.0, sample_rate))
    if sample_rate > 0 and random.random() < sample_rate:
        return True, "shadow_sample", signals

    return False, "risk_gate_skip", signals


def should_run_quality_judge(
    *,
    incoming: IncomingMessage | None,
    result: AgentResult,
    judge_mode: str,
    risk_score: int = 0,
    factual_valid: bool = True,
    openai_call_count: int = 0,
) -> tuple[bool, str, list[str]]:
    """Judge stays off by default; only risk/sample can turn it on when mode ≠ off."""
    if judge_mode == "off":
        return False, "judge_mode_off", []
    if incoming is None:
        incoming = IncomingMessage(text="", channel="unknown")
    signals = critique_risk_signals(
        incoming=incoming,
        result=result,
        risk_score=risk_score,
        factual_valid=factual_valid,
        openai_call_count=openai_call_count,
    )
    trigger_hits = [s for s in signals if s in _CRITIQUE_TRIGGER_SIGNALS]
    if trigger_hits:
        return True, f"risk:{trigger_hits[0]}", signals
    settings = get_settings()
    try:
        sample_rate = float(
            getattr(settings, "agent_quality_judge_sample_rate", 0.0) or 0.0
        )
    except (TypeError, ValueError):
        sample_rate = 0.0
    if sample_rate > 0 and random.random() < sample_rate:
        return True, "judge_sample", signals
    return False, "judge_risk_gate_skip", signals


def resolve_turn_llm_budget(*, complex_turn: bool = False) -> dict[str, Any]:
    settings = get_settings()
    base = int(getattr(settings, "agent_max_llm_calls_per_turn", 2) or 2)
    complex_cap = int(
        getattr(settings, "agent_max_llm_calls_per_turn_complex", 4) or 4
    )
    max_calls = max(base, complex_cap) if complex_turn else base
    return {
        "max_calls": max(0, max_calls),
        "enforce": bool(getattr(settings, "agent_llm_budget_enabled", True)),
        "complex_turn": complex_turn,
    }


ExecutionPath = Literal["fast", "normal", "complex", "critical"]


def build_llm_call_budget(
    *,
    execution_path: ExecutionPath | str = "normal",
    risk_signals: list[str] | None = None,
    complex_turn: bool | None = None,
) -> dict[str, Any]:
    """Central budget for middleware + turn promotion (Etapa 7 / v6 fixes)."""
    path = str(execution_path or "normal").strip().casefold()
    signals = list(risk_signals or [])
    promote = bool(complex_turn)
    if any(
        s in signals
        for s in (
            "image",
            "multi_product",
            "comparison",
            "ambiguity",
            "checkout_resume",
            "integration_failure",
            "revalidation_conflict",
        )
    ):
        promote = True
    if path in {"complex", "critical"}:
        promote = True
    if path == "fast" and not promote:
        settings = get_settings()
        return {
            "max_calls": 1 if bool(getattr(settings, "agent_llm_budget_enabled", True)) else 0,
            "enforce": bool(getattr(settings, "agent_llm_budget_enabled", True)),
            "complex_turn": False,
            "execution_path": "fast",
            "logical_llm_calls": 0,
            "openai_transport_attempts": 0,
        }
    budget = resolve_turn_llm_budget(complex_turn=promote)
    budget["execution_path"] = "complex" if promote else (path or "normal")
    budget["logical_llm_calls"] = 0
    budget["openai_transport_attempts"] = 0
    return budget


def should_promote_to_complex(
    *,
    has_image: bool = False,
    product_count: int = 0,
    comparison: bool = False,
    ambiguity: bool = False,
    checkout_resume: bool = False,
    integration_failure: bool = False,
    revalidation_conflict: bool = False,
) -> tuple[bool, list[str]]:
    signals: list[str] = []
    if has_image:
        signals.append("image")
    if product_count >= 2:
        signals.append("multi_product")
    if comparison:
        signals.append("comparison")
    if ambiguity:
        signals.append("ambiguity")
    if checkout_resume:
        signals.append("checkout_resume")
    if integration_failure:
        signals.append("integration_failure")
    if revalidation_conflict:
        signals.append("revalidation_conflict")
    return bool(signals), signals
