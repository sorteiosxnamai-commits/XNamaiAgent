from __future__ import annotations

import json
from typing import Any

from .guardrails import (
    detect_commerce_inquiry,
    detect_human_support_request,
)
from .models import IncomingMessage
from .user_preferences import get_user_preferences, resolve_display_name


# Parte 1: os intents do dominio de sorteio (simulation, balance, coupon_code,
# raffle_history, current_raffle, rules) sairam do runtime junto com a feature.
INTENT_PRIORITY = (
    "human_support",
    "commerce",
    "general",
)


def detect_customer_intents(text: str | None) -> list[str]:
    normalized = text or ""
    intents: list[str] = []
    commerce = detect_commerce_inquiry(normalized)

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

    # Parte 1: a conta de saldo/Cartao Presente vinha do banco de sorteio, que
    # saiu do runtime. Nao ha fonte para consultar: "nao encontrado", sempre.
    account: dict[str, Any] = {"found": False}
    facts: dict[str, Any] = {
        "primary_intent": primary_intent,
        "intents": intents,
        "input_modality": message.input_modality,
        "transcribed_from_audio": message.input_modality == "audio",
        "account": {"found": False},
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

    # Parte 1: participacoes, cupom pessoal, simulacao de Cartao Presente,
    # historico e rodada aberta eram o dominio de sorteio. A feature saiu do
    # runtime — nenhum desses fatos e montado, e nenhuma fonte e consultada.

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
        "- working_memory é memória interna: use para continuidade, sem despejar pedido/link/PII "
        "se o cliente não pediu.\n"
        "- Nunca pergunte como prefere ser chamado.\n"
        "- Resposta curta para WhatsApp, em português do Brasil."
    )


def build_template_fallback(message: IncomingMessage, facts: dict[str, Any]) -> str | None:
    primary = facts.get("primary_intent")
    account = facts.get("account") or {}
    display_name = facts.get("display_name")

    if primary == "general":
        return "Ol\u00e1! Como posso ajudar?"

    return None
