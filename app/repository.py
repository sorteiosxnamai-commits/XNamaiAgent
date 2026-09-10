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


def _format_participated_at(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _draw_label(draw_id: Any, title: Any) -> str:
    if title:
        return str(title)
    if draw_id is not None:
        return f"Sorteio #{draw_id}"
    return "Sorteio"


def normalize_draw_number(value: str, width: int = 2) -> str:
    cleaned = str(value).strip()
    if cleaned.isdigit():
        return cleaned.zfill(width)
    return cleaned


def default_draw_number_pool(width: int = 2, total: int = 100) -> list[str]:
    return [str(number).zfill(width) for number in range(total)]


def expand_payment_number_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1]
            return [part.strip().strip('"').strip("'") for part in inner.split(",") if part.strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    return [str(raw).strip()]


def parse_draw_number_pool(draw: dict[str, Any]) -> list[str]:
    for key in ("numbers", "number_pool", "all_numbers", "draw_numbers"):
        raw_numbers = draw.get(key)
        if isinstance(raw_numbers, list) and raw_numbers:
            expanded = expand_payment_number_list(raw_numbers)
            width = max((len(item) for item in expanded if item.isdigit()), default=2)
            return [normalize_draw_number(item, width) for item in expanded]

    max_number = draw.get("max_number")
    min_number = draw.get("min_number")
    start_number = draw.get("start_number")
    total = (
        draw.get("total_numbers")
        or draw.get("total_spots")
        or draw.get("quota_count")
        or draw.get("number_count")
    )

    if max_number is not None:
        begin = int(min_number if min_number is not None else (start_number if start_number is not None else 0))
        end = int(max_number)
        width = 2 if end <= 99 else max(2, len(str(end)))
        if end >= begin:
            return [str(number).zfill(width) for number in range(begin, end + 1)]

    if total:
        try:
            count = int(total)
            begin = int(min_number if min_number is not None else (start_number if start_number is not None else 0))
            if count == 100 and begin in (0, 1):
                return default_draw_number_pool()
            end = begin + count - 1
            width = 2 if end <= 99 else max(2, len(str(end)))
            return [str(number).zfill(width) for number in range(begin, end + 1)]
        except (TypeError, ValueError):
            pass

    if draw.get("id") is not None:
        return default_draw_number_pool()
    return []


def _draw_number_width(pool: list[str], payment_rows: list[dict[str, Any]]) -> int:
    lengths: list[int] = [len(item) for item in pool if item.isdigit()]
    for row in payment_rows:
        for item in expand_payment_number_list(row.get("numbers")):
            if item.isdigit():
                lengths.append(len(item))
    return max(lengths) if lengths else 2


def compute_available_numbers(pool: list[str], taken: set[str], width: int = 2) -> list[str]:
    normalized_pool = [normalize_draw_number(number, width) for number in pool]
    taken_normalized = {normalize_draw_number(number, width) for number in taken}
    available = [number for number in normalized_pool if number not in taken_normalized]
    try:
        return sorted(set(available), key=lambda value: int(value))
    except ValueError:
        return sorted(set(available))


def collect_taken_numbers(payment_rows: list[dict[str, Any]], width: int = 2) -> set[str]:
    taken: set[str] = set()
    for row in payment_rows:
        if (row.get("status") or "").lower() != "approved":
            continue
        for number in expand_payment_number_list(row.get("numbers")):
            taken.add(normalize_draw_number(number, width))
    return taken


def resolve_available_numbers(draw: dict[str, Any], payment_rows: list[dict[str, Any]]) -> list[str]:
    pool = parse_draw_number_pool(draw)
    width = _draw_number_width(pool, payment_rows)
    taken = collect_taken_numbers(payment_rows, width=width)
    return compute_available_numbers(pool, taken, width=width)


def _normalize_draw_row(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    if not data.get("title"):
        data["title"] = data.get("name") or data.get("draw_name")
    return data


# --- Dominio de sorteio/NewStore fora do runtime (Parte 1) ---
# As funcoes abaixo consultavam o banco de sorteio (users, draw/draws, raffles,
# payments, app_config_new). O acesso ao banco foi removido: assinatura e tipo de
# retorno sao preservados e cada uma devolve imediatamente o valor "nao encontrado"
# que ja retornava quando o banco nao estava configurado. Sem conexao, sem fallback.
# Os utilitarios puros acima (normalize_phone, format_cents_to_brl, ...) seguem ativos.


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


def find_coupon_balance_by_phone(phone: str | None, message_text: str | None = None) -> dict[str, Any]:
    return _resolve_account(phone, message_text)


def find_user_coupon_code(phone: str | None, message_text: str | None = None) -> dict[str, Any]:
    return _resolve_account(phone, message_text)


def find_customer_profile_by_phone(phone: str | None) -> dict[str, Any]:
    account = _resolve_account(phone, None)
    if not account.get("found"):
        return {"found": False, "lookup_error": account.get("lookup_error")} if account.get("lookup_error") else {"found": False}

    return {
        "found": True,
        "user_id": account.get("user_id"),
        "name": account.get("name"),
        "email_present": True,
        "phone_present": True,
    }


def find_draw_app_config(draw_id: int) -> dict[str, Any]:
    return {"found": False, "error": "database_not_configured"}


def find_current_raffle() -> dict[str, Any]:
    context = find_open_draw_context()
    if not context.get("found"):
        return context

    return {
        "found": True,
        "id": context["draw_id"],
        "draw_id": context["draw_id"],
        "title": context.get("title"),
        "prize_name": context.get("prize_name"),
        "status": context.get("status"),
        "quota_price_brl": context.get("price_brl"),
        "available_numbers": context.get("available_numbers") or [],
        "available_count": len(context.get("available_numbers") or []),
    }


def find_open_draw() -> dict[str, Any]:
    return {"found": False, "error": "database_not_configured"}


def find_open_draw_context() -> dict[str, Any]:
    draw = find_open_draw()
    if draw.get("error") == "database_not_configured":
        return {"found": False, "error": "database_not_configured"}
    if draw.get("lookup_error"):
        return {"found": False, "lookup_error": draw["lookup_error"]}
    if not draw.get("found"):
        return {"found": False, "error": "no_open_draw"}

    draw_id = int(draw["id"])
    config = find_draw_app_config(draw_id)
    payments = find_draw_payments(draw_id)
    if payments.get("lookup_error"):
        return {"found": False, "lookup_error": payments["lookup_error"]}

    payment_rows = payments.get("items", [])
    pool = parse_draw_number_pool(draw)
    width = _draw_number_width(pool, payment_rows)
    available = resolve_available_numbers(draw, payment_rows)
    taken = collect_taken_numbers(payment_rows, width=width)

    prize_name = config.get("prize_name") if config.get("found") else None
    price_brl = config.get("price_brl") if config.get("found") else None

    return {
        "found": True,
        "draw_id": draw_id,
        "title": draw.get("title") or _draw_label(draw_id, None),
        "status": draw.get("status"),
        "prize_name": prize_name,
        "price_brl": price_brl,
        "available_numbers": available,
        "taken_count": len(taken),
        "total_count": len(pool) if pool else None,
    }


def find_draw_payments(draw_id: int) -> dict[str, Any]:
    return {"found": False, "error": "database_not_configured"}


def find_available_numbers_for_open_draw() -> dict[str, Any]:
    return find_open_draw_context()


def find_last_payment_participation(user_id: int) -> dict[str, Any]:
    return {"found": False, "error": "database_not_configured"}


def find_user_raffle_participation(user_id: int) -> dict[str, Any]:
    return {"found": False, "error": "database_not_configured"}
