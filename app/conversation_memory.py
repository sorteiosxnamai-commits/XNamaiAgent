"""Shared, bounded conversation summaries after a final successful reply.

This service performs no model call and never learns company facts from the
reply. Model interpretation is treated as untrusted input and sanitized alongside
the final state; it never authorizes operations or supplies commercial facts.
"""
from __future__ import annotations

from typing import Any

from .conversation_summary_policy import sanitize_summary_delta, text_has_summary_safety_violation
from .conversation_summary_repository import apply_summary_delta, get_conversation_summary, merge_summary_state
from .conversation_summary_scope import summary_conversation_key
from .memory_models import ConversationSummaryDelta
from .memory_scope import trusted_workspace


_PREFERENCE_KEYS = {
    "brand", "marca", "color", "cor", "material", "size", "tamanho", "style", "estilo",
    "occasion", "ocasiao", "purpose", "uso", "category", "categoria", "product_type",
    "power", "potencia", "connectivity", "conectividade", "technology", "attributes", "budget", "budget_min",
    "budget_max", "orcamento", "min_price", "max_price", "audience", "publico",
}
_KEY_ALIASES = {"marca": "brand", "cor": "color", "tamanho": "size", "estilo": "style",
                "ocasiao": "occasion", "uso": "purpose", "categoria": "category",
                "potencia": "power", "conectividade": "connectivity", "orcamento": "budget",
                "publico": "audience", "technology": "connectivity"}
_SUMMARY_FIELDS = {"current_goal", "resolved_points", "open_questions", "user_corrections",
                   "commitments", "last_failure"}
_COMPARABLE_FIELDS = ("current_goal", "resolved_points", "open_questions", "user_corrections",
                      "commitments", "last_failure")


def _state_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return value if isinstance(value, dict) else {}


def _failed_reply(result, metadata: dict[str, Any]) -> bool:
    if getattr(result, "handoff_required", False) or not str(getattr(result, "reply_text", "") or "").strip():
        return True
    if metadata.get("fallback_reason") or metadata.get("response_rejected"):
        return True
    source = str(metadata.get("response_source") or "").casefold()
    if "fallback" in source and source != "deterministic_fallback":
        return True
    reason = str(getattr(result, "safety_reason", "") or "").casefold()
    if any(token in reason for token in ("unavailable", "timeout", "failed", "fallback", "rejected", "unsafe", "blocked")):
        return True
    final_validation = metadata.get("factual_validation") or {}
    initial_validation = metadata.get("factual_validation_initial") or {}
    repaired = metadata.get("claim_repair") or {}
    if isinstance(final_validation, dict) and final_validation.get("valid") is False:
        return True
    if isinstance(initial_validation, dict) and initial_validation.get("valid") is False:
        if not (isinstance(repaired, dict) and repaired.get("accepted") is True
                and isinstance(final_validation, dict) and final_validation.get("valid") is True):
            return True
    return False


def _safe_preferences(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    entries = {}
    for key, item in list(value.items())[:32]:
        key = str(key).strip().casefold()
        if key == "attributes" and isinstance(item, list):
            item = ", ".join(str(x).strip()[:80] for x in item[:8]
                             if isinstance(x, str) and not text_has_summary_safety_violation(x))
        if key not in _PREFERENCE_KEYS or not isinstance(item, (str, int, float, bool)):
            continue
        key = _KEY_ALIASES.get(key, key)
        text = str(item).strip()
        if text and not text_has_summary_safety_violation(text):
            entries[key] = f"preference:{key}={text[:180]}"
    return list(entries.values())[:12]


def _safe_optional_id(value: Any) -> int | None:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def build_conversation_summary_delta(incoming, result, previous_state=None):
    """Return sanitized continuity and merge options, without loading persistence."""
    metadata = dict(getattr(result, "response_metadata", {}) or {})
    state = _state_payload(metadata.get("commerce_state"))
    provided = metadata.get("conversation_summary_delta")
    if hasattr(provided, "model_dump"):
        provided = provided.model_dump(mode="json", exclude_unset=True)
    provided = provided if isinstance(provided, dict) else {}
    payload = {key: value for key, value in provided.items() if key in _SUMMARY_FIELDS}
    if not payload.get("current_goal"):
        payload["current_goal"] = metadata.get("conversation_goal") or state.get("conversation_goal")

    preferences = dict(state.get("active_preferences") or {})
    if isinstance(provided.get("preferences"), dict):
        preferences.update(provided["preferences"])
    resolved = payload.get("resolved_points") or []
    payload["resolved_points"] = list(resolved) if isinstance(resolved, list) else []
    payload["resolved_points"] += _safe_preferences(preferences)

    # A final pending question is a snapshot, including explicit null/empty.
    # Never carry a previous question merely because a list merge retained it.
    replace_questions = "open_questions" in provided or "pending_followup" in state or "pending_followup" in metadata
    if "pending_followup" in state or "pending_followup" in metadata:
        pending = metadata.get("pending_followup", state.get("pending_followup"))
        question = str(pending.get("question") or "").strip() if isinstance(pending, dict) else ""
        payload["open_questions"] = [question] if question else []

    delta = ConversationSummaryDelta.model_validate(payload)
    # Known contact names are also excluded; their raw messages are never copied.
    name = str(getattr(incoming, "sender_name", "") or "").strip().casefold()
    if len(name) > 2:
        for key, value in delta.model_dump().items():
            if isinstance(value, str) and name in value.casefold():
                setattr(delta, key, None)
            elif isinstance(value, list):
                setattr(delta, key, [item for item in value if name not in item.casefold()])
    cleaned, codes = sanitize_summary_delta(delta)
    return cleaned or ConversationSummaryDelta(), {
        "replace_open_questions": replace_questions,
        "replace_corrections": bool(delta.user_corrections),
        "reset_context": provided.get("reset_context") is True,
    }, codes


def update_conversation_memory(incoming, result, settings, previous_state=None) -> dict[str, Any]:
    """Apply safe context for any reply route. Exceptions never affect delivery.

    ``off`` and a disabled flag perform no I/O; ``shadow`` sanitizes/evaluates
    without writing. Call only after the final response is accepted/delivered.
    """
    mode = str(getattr(settings, "agent_conversation_summary_mode", "off") or "off").casefold()
    report = {"mode": mode, "applied": False, "reason": "disabled", "rejection_codes": []}
    if mode not in {"shadow", "enforce"} or not getattr(settings, "agent_conversation_summary_enabled", True):
        return report
    if not getattr(settings, "database_url", None):
        return {**report, "reason": "database_unavailable"}
    if not trusted_workspace(settings, getattr(incoming, "workspace_id", None)):
        return {**report, "reason": "workspace_unavailable"}
    key = summary_conversation_key(incoming, settings)
    tenant = str(getattr(settings, "agent_persona_tenant_id", "") or "").strip()
    if not key or not tenant:
        return {**report, "reason": "conversation_unavailable"}
    metadata = dict(getattr(result, "response_metadata", {}) or {})
    if _failed_reply(result, metadata):
        return {**report, "reason": "response_not_accepted"}
    try:
        delta, options, codes = build_conversation_summary_delta(incoming, result, previous_state)
        report["rejection_codes"] = codes[:8]
        existing = get_conversation_summary(tenant_id=tenant, conversation_key=key)
        inbound_id = _safe_optional_id((getattr(incoming, "raw", {}) or {}).get("inbound_id"))
        if inbound_id and (existing or {}).get("last_inbound_id"):
            if inbound_id <= int(existing["last_inbound_id"]):
                return {**report, "reason": "already_processed"}
        maximum = max(0, min(int(getattr(settings, "agent_max_conversation_summary_chars", 2500)), 6000))
        candidate = merge_summary_state(existing, delta, **options, max_chars=maximum)
        if not any(candidate.get(field) for field in _COMPARABLE_FIELDS) and not existing:
            return {**report, "reason": "no_safe_context"}
        if existing and all((existing.get(field) or None) == (candidate.get(field) or None) for field in _COMPARABLE_FIELDS):
            return {**report, "reason": "unchanged"}
        if mode == "shadow":
            return {**report, "reason": "shadow_evaluated"}
        apply_summary_delta(tenant_id=tenant, conversation_key=key, delta=delta,
                            inbound_id=inbound_id,
                            response_id=_safe_optional_id(metadata.get("response_id")),
                            max_chars=maximum, **options)
        return {**report, "applied": True, "reason": "updated"}
    except Exception as exc:
        # No raw text, identifiers, SQL parameters, or exception strings in logs.
        print("[conversation.memory.update_failed]", {"error_type": type(exc).__name__})
        return {**report, "reason": "update_failed"}
