"""Aggregated per-turn quality metrics without sensitive payloads (Phase 14)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from .config import get_settings
from .turn_runtime import TurnRuntimeContext


def hash_conversation_key(conversation_key: str | None) -> str | None:
    value = (conversation_key or "").strip()
    if not value or value == "unresolved":
        return None
    settings = get_settings()
    secret = str(getattr(settings, "agent_obs_hash_secret", "") or "").strip()
    if not secret:
        secret = "ns-agent-obs-local"
    digest = hmac.new(
        secret.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


def build_turn_quality_event(
    runtime: TurnRuntimeContext,
    *,
    result_metadata: dict[str, Any] | None = None,
    intent: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    metadata = result_metadata or {}
    validation = metadata.get("factual_validation") or {}
    judge = metadata.get("quality_judge") or {}
    cache = runtime.context_snapshot.get("cache") or {}
    if not isinstance(cache, dict):
        cache = {}
    tray_latency = 0.0
    for item in runtime.tray_calls:
        try:
            tray_latency += float(item.get("elapsed_ms") or 0)
        except (TypeError, ValueError):
            continue
    prompt_tokens = int(runtime.openai_input_tokens or 0)
    completion_tokens = int(runtime.openai_output_tokens or 0)
    fallback = None
    if runtime.fallback_reasons:
        fallback = runtime.fallback_reasons[0]
    elif metadata.get("fallback_reason"):
        fallback = metadata.get("fallback_reason")
    settings = get_settings()
    return {
        "event": "turn.quality",
        "conversation_key_hash": hash_conversation_key(runtime.conversation_key),
        "channel": runtime.channel,
        "domain": metadata.get("domain"),
        "intent": intent or metadata.get("goal"),
        "execution_path": runtime.execution_path,
        "model": model or getattr(settings, "openai_model", None),
        "openai_api_route": runtime.openai_api_route,
        "openai_calls": runtime.openai_call_count,
        "tool_calls": runtime.tray_call_count,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "latency_ms": round(
            (runtime.stage_durations_ms.get("request") or 0.0)
            or sum(runtime.stage_durations_ms.values()),
            2,
        ),
        "stage_durations_ms": dict(runtime.stage_durations_ms),
        "tray_latency_ms": round(tray_latency, 2),
        "judge_triggered": bool(runtime.judge_triggered or judge.get("triggered")),
        "factual_valid": bool(validation.get("valid", True)),
        "fallback_reason": fallback,
        "handoff_required": bool(
            metadata.get("handoff", {}).get("required")
            if isinstance(metadata.get("handoff"), dict)
            else metadata.get("handoff_required")
        ),
        "response_source": metadata.get("response_source"),
        "cache_hits": int(cache.get("hits") or 0),
        "cache_misses": int(cache.get("misses") or 0),
        "cache_stale": int(cache.get("stale") or 0),
        "llm_calls_avoided": runtime.llm_calls_avoided,
        "risk_score": runtime.risk_score,
    }
