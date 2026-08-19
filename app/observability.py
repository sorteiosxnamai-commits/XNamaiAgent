from __future__ import annotations

import json
import re
import traceback
from typing import Any

from .runtime_context import get_current_turn


_CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_CNPJ_RE = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
_PHONE_RE = re.compile(r"\b(?:\+?55)?\s*\(?\d{2}\)?\s*\d{4,5}-?\d{4}\b")
_TOKEN_RE = re.compile(r"(?i)(bearer\s+)\S+|(sk-(?:proj-)?[A-Za-z0-9_-]+)")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def full_obs_enabled() -> bool:
    try:
        from .config import get_settings

        return bool(getattr(get_settings(), "agent_full_obs_logs", False))
    except Exception:
        # Fail closed: never dump full prompts/history on config errors.
        return False


def obs_limits() -> dict[str, int]:
    if full_obs_enabled():
        return {
            "text_chars": 1200,
            "openai_preview_chars": 900,
            "openai_max_messages": 40,
            "history_preview_chars": 500,
            "history_max_turns": 60,
            "payload_keys": 80,
            "dict_items": 80,
            "list_items": 40,
            "depth": 6,
        }
    return {
        "text_chars": 400,
        "openai_preview_chars": 280,
        "openai_max_messages": 12,
        "history_preview_chars": 160,
        "history_max_turns": 6,
        "payload_keys": 40,
        "dict_items": 40,
        "list_items": 20,
        "depth": 4,
    }


def redact_text(value: str | None, *, max_chars: int | None = None) -> str:
    limits = obs_limits()
    if max_chars is None:
        max_chars = limits["text_chars"]
    text = str(value or "")
    text = _TOKEN_RE.sub(lambda m: (m.group(1) or "") + "***", text)
    text = _CPF_RE.sub("[CPF]", text)
    text = _CNPJ_RE.sub("[CNPJ]", text)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _PHONE_RE.sub("[PHONE]", text)
    text = _CARD_RE.sub("[CARD]", text)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def redact_value(value: Any, *, depth: int = 0) -> Any:
    limits = obs_limits()
    max_depth = limits["depth"]
    if depth > max_depth:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value, max_chars=limits["text_chars"])
    if isinstance(value, list):
        return [
            redact_value(item, depth=depth + 1)
            for item in value[: limits["list_items"]]
        ]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in list(value.items())[: limits["dict_items"]]:
            key_l = str(key).casefold()
            if any(
                token in key_l
                for token in (
                    "token",
                    "secret",
                    "password",
                    "authorization",
                    "api_key",
                    "cpf",
                    "cnpj",
                    "email",
                    "phone",
                    "tax_document",
                )
            ):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_value(item, depth=depth + 1)
        return redacted
    return redact_text(str(value), max_chars=120)


def summarize_openai_messages(
    messages: list[dict[str, Any]] | None,
    *,
    max_messages: int | None = None,
) -> list[dict[str, Any]]:
    limits = obs_limits()
    if max_messages is None:
        max_messages = limits["openai_max_messages"]
    preview_chars = limits["openai_preview_chars"]
    summarized: list[dict[str, Any]] = []
    for message in (messages or [])[:max_messages]:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            text_parts = []
            part_types: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    part_types.append(str(part.get("type") or "unknown"))
                    if part.get("type") == "text":
                        text_parts.append(str(part.get("text") or ""))
                    elif part.get("type") == "image_url":
                        text_parts.append("[image]")
                    else:
                        text_parts.append(str(part.get("type") or part))
                else:
                    text_parts.append(str(part))
            content_text = "\n".join(text_parts)
            content_kind = "multipart"
        else:
            content_text = str(content or "")
            content_kind = "text"
            part_types = []
        entry: dict[str, Any] = {
            "role": message.get("role"),
            "chars": len(content_text),
            "content_kind": content_kind,
            "preview": redact_text(content_text, max_chars=preview_chars),
        }
        if part_types:
            entry["part_types"] = part_types
        name = message.get("name")
        if name:
            entry["name"] = name
        summarized.append(entry)
    total = len(messages or [])
    if total > max_messages:
        summarized.append(
            {
                "role": "system",
                "chars": 0,
                "content_kind": "meta",
                "preview": f"[truncated {total - max_messages} older messages]",
            }
        )
    return summarized


def summarize_history_turns(
    turns: list[dict[str, Any]] | None,
    *,
    max_turns: int | None = None,
) -> list[dict[str, Any]]:
    limits = obs_limits()
    if max_turns is None:
        max_turns = limits["history_max_turns"]
    preview_chars = limits["history_preview_chars"]
    items = list(turns or [])
    window = items[-max_turns:] if max_turns > 0 else items
    summarized: list[dict[str, Any]] = []
    for turn in window:
        if not isinstance(turn, dict):
            continue
        content = str(turn.get("content") or turn.get("text") or "")
        summarized.append(
            {
                "role": turn.get("role"),
                "chars": len(content),
                "preview": (
                    redact_text(content, max_chars=preview_chars)
                    if full_obs_enabled()
                    else None
                ),
                "inbound_id": turn.get("inbound_id"),
                "created_at": turn.get("created_at"),
            }
        )
    if len(items) > len(window):
        summarized.insert(
            0,
            {
                "role": "system",
                "chars": 0,
                "preview": f"[omitted {len(items) - len(window)} older turns]",
            },
        )
    return summarized


def summarize_customer_context(customer_context: dict[str, Any] | None) -> dict[str, Any]:
    payload = customer_context if isinstance(customer_context, dict) else {}
    keys = sorted(str(key) for key in payload.keys() if not str(key).startswith("_"))
    return {
        "found": bool(payload.get("found")),
        "user_id_present": bool(payload.get("user_id")),
        "name_present": bool(payload.get("name") or payload.get("display_name")),
        "channel": payload.get("channel"),
        "sender_key_present": bool(payload.get("sender_key")),
        "keys": keys[:40],
        "preferred_name_present": bool(payload.get("preferred_name")),
        "has_commerce_state": bool(payload.get("_commerce_state")),
        "has_working_memory": bool(payload.get("_working_memory")),
        "model_turns": len(payload.get("_model_conversation_turns") or []),
        "recovery_turns": len(payload.get("_conversation_turns") or []),
    }


def summarize_commerce_state(state: Any) -> dict[str, Any]:
    if state is None:
        return {}
    payload = (
        state.model_dump(mode="json")
        if hasattr(state, "model_dump")
        else (state if isinstance(state, dict) else {})
    )
    active = payload.get("active_product") if isinstance(payload, dict) else None
    active_name = None
    active_id = None
    if isinstance(active, dict):
        active_name = active.get("name")
        active_id = active.get("product_id") or active.get("id")
    presented = payload.get("last_presented_products") or []
    presented_names: list[str] = []
    if isinstance(presented, list):
        for item in presented[:12]:
            if isinstance(item, dict):
                name = item.get("name") or item.get("title")
                if name:
                    presented_names.append(redact_text(str(name), max_chars=80))
    summary: dict[str, Any] = {
        "active_domain": payload.get("active_domain"),
        "purchase_stage": payload.get("purchase_stage"),
        "pending_action": payload.get("pending_action"),
        "has_cart_session": bool(payload.get("cart_session_id")),
        "cart_item_count": len(payload.get("cart_items") or []),
        "presented_product_count": len(presented) if isinstance(presented, list) else 0,
        "presented_product_names": presented_names,
        "active_product_id": active_id,
        "active_product_name": redact_text(active_name, max_chars=80) or None,
        "has_order_id": bool(payload.get("order_id")),
        "order_id": payload.get("order_id"),
        "has_payment_url": bool(payload.get("order_payment_url")),
        "order_payment_status": payload.get("order_payment_status"),
        "has_checkout_draft": bool(payload.get("checkout_draft")),
        "checkout_customer_fields": _checkout_customer_fields(payload),
        "active_topic_present": bool(payload.get("active_topic")),
        "pending_action_product_ids": list(payload.get("pending_action_product_ids") or [])[
            :12
        ],
    }
    if full_obs_enabled():
        summary["working_keys"] = sorted(
            str(key)
            for key, value in payload.items()
            if value not in (None, "", [], {})
        )[:40]
    return summary


def _checkout_customer_fields(payload: dict[str, Any]) -> list[str]:
    draft = payload.get("checkout_draft") if isinstance(payload, dict) else None
    customer = draft.get("customer") if isinstance(draft, dict) else None
    if not isinstance(customer, dict):
        return []
    return sorted(
        key
        for key, value in customer.items()
        if value not in (None, "", [], {})
    )


def summarize_tray_result(result: dict[str, Any] | None) -> dict[str, Any]:
    payload = result if isinstance(result, dict) else {}
    summary: dict[str, Any] = {
        "ok": "error" not in payload,
        "status_code": payload.get("status_code"),
        "error": payload.get("error"),
        "keys": sorted(str(key) for key in payload.keys())[:20],
    }
    for key in (
        "order_id",
        "id",
        "status",
        "status_group",
        "success",
        "match_status",
        "product_count",
        "count",
    ):
        if key in payload and payload.get(key) not in (None, "", [], {}):
            summary[key] = redact_value(payload.get(key))
    payment = payload.get("payment")
    if isinstance(payment, dict):
        summary["payment"] = {
            "status": payment.get("status"),
            "has_payment": payment.get("has_payment"),
            "payment_url_present": bool(payment.get("payment_url")),
        }
    products = payload.get("products")
    if isinstance(products, list):
        summary["products_count"] = len(products)
        if full_obs_enabled():
            names: list[str] = []
            for item in products[:8]:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("title")
                    if name:
                        names.append(redact_text(str(name), max_chars=80))
            if names:
                summary["product_names"] = names
    orders = payload.get("orders")
    if isinstance(orders, list):
        summary["orders_count"] = len(orders)
    return summary


def summarize_webhook_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    visitor = data.get("visitor") if isinstance(data.get("visitor"), dict) else {}
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    return {
        "event_name": data.get("eventName") or data.get("event") or data.get("eventType"),
        "payload_keys": list(data.keys())[: obs_limits()["payload_keys"]],
        "conversation_id_present": bool(data.get("conversationId") or data.get("conversation_id")),
        "visitor_id_present": bool(visitor.get("id")),
        "visitor_source": visitor.get("source"),
        "source_conversation_ref_present": bool(visitor.get("sourceConversationRef")),
        "message_id_present": bool(message.get("id")),
        "messages_count": len(messages),
        "message_text_preview": redact_text(
            str(message.get("text") or ""),
            max_chars=obs_limits()["text_chars"],
        )
        or None,
    }


def log_event(event: str, payload: dict[str, Any] | None = None) -> None:
    """Structured turn-aware log line for Vercel/runtime debugging."""
    runtime = get_current_turn()
    body: dict[str, Any] = {
        "event": event,
        "trace_id": runtime.trace_id if runtime else None,
        "inbound_id": runtime.inbound_id if runtime else None,
        "channel": runtime.channel if runtime else None,
        "conversation_key_present": bool(
            runtime
            and runtime.conversation_key
            and runtime.conversation_key != "unresolved"
        ),
        "full_obs": full_obs_enabled(),
    }
    if runtime is not None and full_obs_enabled():
        body["openai_call_count"] = runtime.openai_call_count
        body["tray_call_count"] = runtime.tray_call_count
        body["execution_path"] = runtime.execution_path
    if payload:
        body.update(
            redact_value(payload)
            if not isinstance(payload, dict)
            else {key: redact_value(value) for key, value in payload.items()}
        )
    # Keep JSON one-line for Vercel log search.
    print(f"[agent.obs] {json.dumps(body, ensure_ascii=False, default=str)}")


def log_exception(event: str, exc: BaseException, payload: dict[str, Any] | None = None) -> None:
    details = {
        "error_type": type(exc).__name__,
        "error_message": redact_text(str(exc), max_chars=400),
    }
    if full_obs_enabled():
        details["traceback"] = redact_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            max_chars=2500,
        )
    if payload:
        details.update(payload)
    log_event(event, details)


def record_tray_observation(
    *,
    tool: str,
    arguments: dict[str, Any] | None,
    result: dict[str, Any] | None,
    elapsed_ms: float,
) -> None:
    runtime = get_current_turn()
    observation = {
        "tool": tool,
        "ok": isinstance(result, dict) and "error" not in result,
        "elapsed_ms": round(elapsed_ms, 2),
        "arguments": redact_value(arguments or {}),
        "result": summarize_tray_result(result if isinstance(result, dict) else {}),
    }
    if runtime is not None:
        runtime.tray_calls.append(observation)
    log_event("tray.call", observation)


def record_openai_observation(
    *,
    call_type: str,
    model: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    response_preview: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    elapsed_ms: float | None = None,
    ok: bool = True,
    error_type: str | None = None,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> None:
    runtime = get_current_turn()
    limits = obs_limits()
    observation = {
        "call_type": call_type,
        "model": model,
        "ok": ok,
        "error_type": error_type,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "elapsed_ms": elapsed_ms,
        "messages": (
            summarize_openai_messages(messages) if full_obs_enabled() else []
        ),
        "message_count": len(messages or []),
        "response_preview": (
            redact_text(
                response_preview,
                max_chars=limits["openai_preview_chars"],
            )
            if full_obs_enabled()
            else None
        ),
        "response_chars": len(str(response_preview or "")),
    }
    if runtime is not None:
        runtime.openai_calls.append(
            {
                "call_type": call_type,
                "model": model,
                "ok": ok,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
                "reasoning_tokens": reasoning_tokens,
                "message_count": len(messages or []),
            }
        )
    log_event("openai.call", observation)
    # Split large GPT payloads into a dedicated searchable event when full obs.
    if full_obs_enabled() and messages:
        log_event(
            "openai.prompt",
            {
                "call_type": call_type,
                "model": model,
                "message_count": len(messages),
                "messages": summarize_openai_messages(messages),
            },
        )


def record_brevo_send(
    *,
    channel: str,
    ok: bool,
    dry_run: bool,
    status_code: int | None = None,
    error: str | None = None,
    reply_preview: str | None = None,
    reply_modality: str | None = None,
    visitor_id_present: bool = False,
    sender_phone_present: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "send_channel": channel,
        "ok": ok,
        "dry_run": dry_run,
        "status_code": status_code,
        "error": error,
        "reply_modality": reply_modality,
        "reply_chars": len(str(reply_preview or "")),
        "reply_preview": (
            redact_text(reply_preview) if full_obs_enabled() else None
        ),
        "visitor_id_present": visitor_id_present,
        "sender_phone_present": sender_phone_present,
    }
    if extra:
        payload.update(extra)
    log_event("brevo.send", payload)
