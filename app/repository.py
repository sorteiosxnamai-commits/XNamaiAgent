from __future__ import annotations

import re
from typing import Any


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits or None


def phones_match(stored: str | None, incoming: str | None) -> bool:
    stored_norm = normalize_phone(stored)
    incoming_norm = normalize_phone(incoming)
    if not stored_norm or not incoming_norm:
        return False
    if len(stored_norm) < 9 or len(incoming_norm) < 9:
        return False
    return stored_norm[-9:] == incoming_norm[-9:]


def extract_phone_candidates(text: str | None) -> list[str]:
    if not text:
        return []
    candidates: list[str] = []
    for match in re.findall(r"\d[\d\s().-]{8,}\d", text):
        normalized = normalize_phone(match)
        if normalized and len(normalized) >= 10:
            candidates.append(normalized)
    return candidates


def detect_third_party_account_inquiry(text: str | None, sender_phone: str | None) -> bool:
    normalized_text = (text or "").lower()
    blocked_phrases = (
        "saldo do ",
        "saldo de ",
        "saldo da ",
        "cupom do ",
        "cupom de ",
        "telefone de ",
        "telefone do ",
        "outra pessoa",
        "outro usuario",
        "outro usuário",
        "cpf de ",
        "cpf do ",
    )
    if any(phrase in normalized_text for phrase in blocked_phrases):
        return True

    sender_norm = normalize_phone(sender_phone)
    for candidate in extract_phone_candidates(text):
        if sender_norm and candidate[-9:] != sender_norm[-9:]:
            return True
    return False


def format_cents_to_brl(cents: int | None) -> str:
    value = max(0, int(cents or 0))
    reais = value // 100
    centavos = value % 100
    reais_str = f"{reais:,}".replace(",", ".")
    return f"R$ {reais_str},{centavos:02d}"


def format_payment_numbers(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        cleaned = [str(item).strip() for item in raw if str(item).strip()]
        return ", ".join(cleaned) if cleaned else None
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    return str(raw)


# --- Dominio de sorteio desativado fora do runtime (Parte 1) ---
# As funcoes de sorteio (saldo, cupom pessoal, rodadas, pagamentos, numeros,
# participacoes) e os utilitarios de grade de numeros foram REMOVIDOS junto com
# a feature: nao havia mais chamador de runtime, so testes da propria feature.
# Sobra o perfil de cliente, que Task L preserva como contrato observavel, e os
# utilitarios puros acima (normalize_phone, format_cents_to_brl, ...).


def _resolve_account(phone: str | None, message_text: str | None) -> dict[str, Any]:
    # As duas guardas abaixo sao puras (normalize_phone e a heuristica de texto
    # detect_third_party_account_inquiry) e NAO pertencem ao dominio de sorteio:
    # validacao de telefone e recusa de consulta a conta de terceiro sao
    # comportamento generico do agente. Ordem preservada do original.
    normalized = normalize_phone(phone)
    if not normalized:
        return {"found": False, "error": "phone_missing"}
    if detect_third_party_account_inquiry(message_text, phone):
        return {"found": False, "error": "third_party_inquiry"}
    return {"found": False, "error": "database_not_configured"}


def find_customer_profile_by_phone(phone: str | None) -> dict[str, Any]:
    """Perfil do cliente. Sem fonte configurada: sempre "nao encontrado".

    Contrato de retorno preservado (Task L): mesmas chaves do baseline, para que
    o ingress nao precise mudar de formato por causa da neutralizacao.
    """
    account = _resolve_account(phone, None)
    if not account.get("found"):
        if account.get("lookup_error"):
            return {"found": False, "lookup_error": account.get("lookup_error")}
        return {"found": False}

    return {
        "found": True,
        "user_id": account.get("user_id"),
        "name": account.get("name"),
        "email_present": True,
        "phone_present": True,
    }
