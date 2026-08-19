from __future__ import annotations

import json
from typing import Any

from .guardrails import (
    detect_balance_inquiry,
    detect_commerce_inquiry,
    detect_coupon_code_inquiry,
    detect_current_raffle_inquiry,
    detect_human_support_request,
    detect_raffle_history_inquiry,
    detect_rules_inquiry,
    detect_simulation_inquiry,
)
from .models import IncomingMessage
from .repository import (
    find_coupon_balance_by_phone,
    find_current_raffle,
    find_last_payment_participation,
    find_open_draw_context,
    find_user_coupon_code,
    find_user_raffle_participation,
    format_cents_to_brl,
)
from .simulation import (
    build_purchase_simulation_reply,
    detect_payment_method,
    parse_product_price_cents,
    resolve_simulation_credit_cents,
    simulate_purchase,
)
from .user_preferences import get_user_preferences, resolve_display_name


INTENT_PRIORITY = (
    "simulation",
    "balance",
    "coupon_code",
    "raffle_history",
    "current_raffle",
    "rules",
    "human_support",
    "commerce",
    "general",
)


def detect_customer_intents(text: str | None) -> list[str]:
    normalized = text or ""
    intents: list[str] = []
    commerce = detect_commerce_inquiry(normalized)

    # "quanto fica no Pix?" is a commercial question; retain simulation only
    # when the customer is actually discussing personal credit/saldo.
    has_personal_credit_signal = detect_balance_inquiry(normalized) or detect_coupon_code_inquiry(normalized) or any(
        phrase in normalized.lower() for phrase in ("meu saldo", "meu cartao", "meu cartão", "cartão presente")
    )
    if detect_simulation_inquiry(normalized) and not (commerce and not has_personal_credit_signal):
        intents.append("simulation")
    if detect_balance_inquiry(normalized):
        intents.append("balance")
    if detect_coupon_code_inquiry(normalized):
        intents.append("coupon_code")
    if detect_raffle_history_inquiry(normalized):
        intents.append("raffle_history")
    if detect_current_raffle_inquiry(normalized):
        intents.append("current_raffle")
    if detect_rules_inquiry(normalized):
        intents.append("rules")
    if detect_human_support_request(normalized):
        intents.append("human_support")
    if commerce:
        intents.append("commerce")
    if not intents:
        intents.append("general")
    return intents


def _primary_intent(intents: list[str]) -> str:
    for candidate in INTENT_PRIORITY:
        if candidate in intents:
            return candidate
    return intents[0] if intents else "general"


def detect_primary_intent(text: str | None) -> str:
    """Classify intent without loading account, raffle, or customer data."""
    return _primary_intent(detect_customer_intents(text))


def _serialize_account(account: dict[str, Any]) -> dict[str, Any]:
    if not account.get("found"):
        return {
            "found": False,
            "error": account.get("error"),
            "lookup_error": account.get("lookup_error"),
        }

    return {
        "found": True,
        "user_id": account.get("user_id"),
        "name": account.get("name"),
        "balance_brl": account.get("balance_brl"),
        "balance_cents": account.get("coupon_value_cents"),
    }


def gather_customer_facts(message: IncomingMessage, customer_context: dict[str, Any]) -> dict[str, Any]:
    text = message.text or ""
    intents = detect_customer_intents(text)
    primary_intent = _primary_intent(intents)

    local_account_intents = {"balance", "coupon_code", "simulation", "raffle_history"}
    account = find_coupon_balance_by_phone(message.sender_phone, text) if primary_intent in local_account_intents else {"found": False}
    facts: dict[str, Any] = {
        "primary_intent": primary_intent,
        "intents": intents,
        "input_modality": message.input_modality,
        "transcribed_from_audio": message.input_modality == "audio",
        "account": _serialize_account(account) if primary_intent in local_account_intents else {"found": False},
    }

    if primary_intent in {"commerce", "general"} and customer_context.get("found"):
        if customer_context.get("name"):
            facts["display_name"] = customer_context["name"]

    if account.get("found"):
        user_id = int(account["user_id"])
        preferences = customer_context.get("preferences") or get_user_preferences(user_id)
        facts["display_name"] = resolve_display_name(account.get("name"), preferences)
        facts["preferences"] = {
            "preferred_name": preferences.get("preferred_name"),
            "speaking_style": preferences.get("speaking_style"),
            "memory_notes": (preferences.get("memory_notes") or [])[-8:],
            "recent_topics": (preferences.get("recent_topics") or [])[-6:],
        }

        last_participation = find_last_payment_participation(user_id)
        if last_participation.get("found"):
            facts["last_participation"] = {
                "raffle_title": last_participation.get("raffle_title"),
                "participated_at": (last_participation.get("participated_at") or "")[:10],
                "numbers": last_participation.get("numbers"),
                "amount_brl": last_participation.get("amount_brl"),
            }

    if "coupon_code" in intents or primary_intent == "coupon_code":
        coupon = find_user_coupon_code(message.sender_phone, text)
        if coupon.get("found"):
            facts["coupon"] = {
                "code": coupon.get("coupon_code"),
                "balance_brl": coupon.get("balance_brl"),
            }

    if "simulation" in intents or primary_intent == "simulation":
        account_credit = int(account.get("coupon_value_cents") or 0) if account.get("found") else 0
        credit_cents = resolve_simulation_credit_cents(text, account_credit or None)
        product_cents = parse_product_price_cents(text, credit_cents=credit_cents or None)
        payment_method = detect_payment_method(text)
        simulation_payload: dict[str, Any] = {
            "credit_brl": format_cents_to_brl(credit_cents) if credit_cents else None,
            "credit_cents": credit_cents,
            "account_balance_brl": format_cents_to_brl(account_credit) if account_credit else None,
            "uses_hypothetical_credit": credit_cents != account_credit and credit_cents > 0,
            "product_brl": format_cents_to_brl(product_cents) if product_cents else None,
            "product_cents": product_cents,
            "payment_method": payment_method,
        }
        if credit_cents and product_cents:
            result = simulate_purchase(credit_cents, product_cents)
            simulation_payload.update(
                {
                    "eligible": result["eligible"],
                    "max_applicable_brl": format_cents_to_brl(result["max_applicable_cents"]),
                    "applied_brl": format_cents_to_brl(result["applied_cents"]),
                    "final_brl": format_cents_to_brl(result["final_cents"]),
                    "remaining_balance_brl": format_cents_to_brl(result["remaining_balance_cents"]),
                    "can_apply_full_balance": result["can_apply_full_balance"],
                    "min_purchase_brl": format_cents_to_brl(result["min_purchase_cents"])
                    if result["min_purchase_cents"]
                    else None,
                    "reason": result.get("reason"),
                }
            )
        facts["simulation"] = simulation_payload

    if "raffle_history" in intents or primary_intent == "raffle_history":
        if account.get("found"):
            history = find_user_raffle_participation(int(account["user_id"]))
            if history.get("found"):
                facts["raffle_history"] = {
                    "entries": history.get("entries", [])[:5],
                    "total_entries": history.get("total_entries"),
                }

    if "current_raffle" in intents or primary_intent == "current_raffle":
        draw = find_open_draw_context()
        if draw.get("found"):
            facts["open_draw"] = {
                "draw_id": draw.get("draw_id"),
                "title": draw.get("title"),
                "prize_name": draw.get("prize_name"),
                "price_brl": draw.get("price_brl"),
                "status": draw.get("status"),
            }
        else:
            raffle = find_current_raffle()
            if raffle.get("found"):
                facts["open_draw"] = {
                    "title": raffle.get("title"),
                    "prize_name": raffle.get("prize_name"),
                    "price_brl": raffle.get("price_brl"),
                    "status": raffle.get("status"),
                }

    if customer_context.get("memory_context"):
        facts["memory_context"] = customer_context["memory_context"]
    working_memory = customer_context.get("_working_memory")
    if not working_memory and customer_context.get("_commerce_state"):
        from .working_memory import build_working_memory

        working_memory = build_working_memory(customer_context.get("_commerce_state"))
    if working_memory:
        facts["working_memory"] = working_memory

    return facts


def format_facts_for_prompt(facts: dict[str, Any]) -> str:
    payload = json.dumps(facts, ensure_ascii=False, indent=2, default=str)
    return (
        "Dados consultados no banco (use EXATAMENTE estes valores; não invente números):\n"
        f"{payload}\n\n"
        "Instruções:\n"
        "- Responda PRIMEIRO à pergunta literal do cliente.\n"
        "- Se simulation.uses_hypothetical_credit for true, responda usando credit_brl informado pelo cliente, "
        "não account_balance_brl.\n"
        "- Se simulation.can_apply_full_balance for false, deixe claro que NÃO dá para abater todo o saldo "
        "e use max_applicable_brl, applied_brl e final_brl.\n"
        "- Não repita saldo/última participação se o cliente não pediu isso.\n"
        "- working_memory é memória interna: use para continuidade, sem despejar pedido/link/PII "
        "se o cliente não pediu.\n"
        "- Nunca pergunte como prefere ser chamado.\n"
        "- Resposta curta para WhatsApp, em português do Brasil."
    )


def build_template_fallback(message: IncomingMessage, facts: dict[str, Any]) -> str | None:
    primary = facts.get("primary_intent")
    account = facts.get("account") or {}
    display_name = facts.get("display_name")

    if primary == "simulation":
        simulation = facts.get("simulation") or {}
        credit_cents = int(simulation.get("credit_cents") or 0)
        product_cents = simulation.get("product_cents")
        if credit_cents > 0:
            return build_purchase_simulation_reply(
                credit_cents=credit_cents,
                product_cents=product_cents,
                payment_method=simulation.get("payment_method"),
                display_name=display_name,
            )

    if primary == "balance" and account.get("found"):
        greeting = f"Olá, {display_name}!" if display_name else "Olá!"
        parts = [f"{greeting} Seu saldo disponível é {account['balance_brl']}."]
        last = facts.get("last_participation")
        if last:
            parts.append(
                "Última participação: "
                f"{last.get('raffle_title')} em {last.get('participated_at')} "
                f"números {last.get('numbers')} ({last.get('amount_brl')})."
            )
        return " ".join(parts)

    if primary == "general":
        return "Ol\u00e1! Como posso ajudar?"

    return None
