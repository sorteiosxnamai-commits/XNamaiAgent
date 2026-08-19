from __future__ import annotations

import re
from typing import Any

from .config import get_settings
from .guardrails import detect_last_participation_inquiry
from .models import AgentResult, IncomingMessage
from .repository import (
    find_coupon_balance_by_phone,
    find_available_numbers_for_open_draw,
    find_current_raffle,
    find_last_payment_participation,
    find_user_coupon_code,
    find_user_raffle_participation,
    format_cents_to_brl,
)
from .user_preferences import (
    detect_preferred_name_update,
    get_user_preferences,
    mark_preferred_name_prompted,
    resolve_display_name,
    save_preferred_name,
)
from .vip_profiles import (
    build_vip_balance_reply,
    build_vip_coupon_reply,
    build_vip_general_reply,
    get_vip_profile,
    pick_vip_nickname,
)
from .site_knowledge import (
    REGISTER_PHONE_MESSAGE,
    SITE_URL,
    STORE_URL,
    THIRD_PARTY_REFUSAL,
    NS_SALES_WHATSAPP,
    build_rules_reply,
)

LOCAL_LOOKUP_UNAVAILABLE = "N\u00e3o consegui consultar seus dados neste momento. Tente novamente em instantes."


def _greeting(display_name: str | None) -> str:
    cleaned = (display_name or "").strip()
    return f"Olá, {cleaned}!" if cleaned else "Olá!"


def _format_last_participation(user_id: int) -> str | None:
    last_payment = find_last_payment_participation(user_id)
    if not last_payment.get("found"):
        return None

    parts: list[str] = ["Última participação:"]
    if last_payment.get("raffle_title"):
        parts.append(str(last_payment["raffle_title"]))
    if last_payment.get("participated_at"):
        parts.append(f"em {last_payment['participated_at'][:10]}")
    if last_payment.get("numbers"):
        parts.append(f"números {last_payment['numbers']}")
    if last_payment.get("amount_brl"):
        parts.append(f"({last_payment['amount_brl']})")
    return " ".join(parts)


def _build_last_participation_reply(last_payment: dict[str, Any]) -> str:
    title = last_payment.get("raffle_title") or "sorteio"
    date_label = (last_payment.get("participated_at") or "")[:10]
    parts = [f"Sua última participação foi em *{title}*"]
    if date_label:
        parts[0] += f", em {date_label}"
    parts[0] += "."
    if last_payment.get("numbers"):
        parts.append(f"Seus números: {last_payment['numbers']}.")
    if last_payment.get("amount_brl"):
        parts.append(f"Valor: {last_payment['amount_brl']}.")
    if last_payment.get("winning_number"):
        parts.append(f"Número sorteado: {last_payment['winning_number']}.")
    return " ".join(parts)


def _personalized_suffix(
    user_id: int,
    preferences: dict[str, Any],
    phone: str | None = None,
    display_name: str | None = None,
) -> str:
    if get_vip_profile(phone):
        return ""
    if (display_name or "").strip():
        return ""
    if preferences.get("preferred_name"):
        return ""
    if not preferences.get("ask_preferred_name", False):
        return ""
    mark_preferred_name_prompted(user_id)
    return " Prefere ser chamado por outro nome? É só me dizer."


def build_preferred_name_reply(message: IncomingMessage, account: dict[str, Any]) -> AgentResult | None:
    preferred_name = detect_preferred_name_update(message.text)
    if not preferred_name or not account.get("found"):
        return None

    save_preferred_name(int(account["user_id"]), preferred_name)
    vip = get_vip_profile(message.sender_phone)
    if vip:
        nickname = pick_vip_nickname(vip, preferred_name)
        return AgentResult(
            reply_text=(
                f"Anotei, {nickname}! De hoje em diante você é '{preferred_name}'. "
                f"O {vip.title} manda — quem somos nós para discutir?"
            ),
            intent="preferred_name_update",
            handoff_required=False,
        )
    return AgentResult(
        reply_text=f"Perfeito! A partir de agora vou te chamar de {preferred_name}.",
        intent="preferred_name_update",
        handoff_required=False,
    )


def _account_missing_reply(intent: str) -> AgentResult:
    return AgentResult(
        reply_text=REGISTER_PHONE_MESSAGE,
        intent=intent,
        handoff_required=False,
        safety_reason="account_phone_not_registered",
    )


def _third_party_reply() -> AgentResult:
    return AgentResult(
        reply_text=THIRD_PARTY_REFUSAL,
        intent="security_refusal",
        handoff_required=False,
        safety_reason="third_party_account_inquiry",
    )


def build_balance_reply(message: IncomingMessage) -> AgentResult:
    account = find_coupon_balance_by_phone(message.sender_phone, message.text)

    if account.get("error") == "third_party_inquiry":
        return _third_party_reply()

    if account.get("error") == "phone_missing":
        return AgentResult(
            reply_text=REGISTER_PHONE_MESSAGE,
            intent="balance_inquiry",
            handoff_required=False,
            safety_reason="phone_missing",
        )

    if account.get("error") == "database_not_configured":
        return AgentResult(
            reply_text=LOCAL_LOOKUP_UNAVAILABLE,
            intent="balance_inquiry",
            handoff_required=False,
            safety_reason="database_not_configured",
        )

    if account.get("lookup_error"):
        return AgentResult(
            reply_text=LOCAL_LOOKUP_UNAVAILABLE,
            intent="balance_inquiry",
            handoff_required=False,
            safety_reason="balance_lookup_failed",
        )

    if account.get("error") == "phone_not_registered":
        return _account_missing_reply("balance_inquiry")

    if not account.get("found"):
        return AgentResult(
            reply_text=REGISTER_PHONE_MESSAGE,
            intent="balance_inquiry",
            handoff_required=False,
        )

    user_id = int(account["user_id"])
    vip = get_vip_profile(message.sender_phone)
    if vip:
        nickname = pick_vip_nickname(vip, message.text)
        extra = _format_last_participation(user_id) or ""
        return AgentResult(
            reply_text=build_vip_balance_reply(vip, nickname, account["balance_brl"], extra),
            intent="balance_inquiry",
            handoff_required=False,
        )

    preferences = get_user_preferences(user_id)
    display_name = resolve_display_name(account.get("name"), preferences)
    parts = [f"{_greeting(display_name)} Seu saldo disponível é {account['balance_brl']}."]

    last_participation = _format_last_participation(user_id)
    if last_participation:
        parts.append(last_participation)

    parts.append(
        _personalized_suffix(user_id, preferences, message.sender_phone, display_name).strip()
    )

    return AgentResult(
        reply_text=" ".join(part for part in parts if part),
        intent="balance_inquiry",
        handoff_required=False,
    )


def build_coupon_code_reply(message: IncomingMessage) -> AgentResult:
    account = find_user_coupon_code(message.sender_phone, message.text)

    if account.get("error") == "third_party_inquiry":
        return _third_party_reply()
    if account.get("error") == "phone_missing":
        return AgentResult(reply_text=REGISTER_PHONE_MESSAGE, intent="coupon_code", handoff_required=False)
    if account.get("error") == "phone_not_registered":
        return _account_missing_reply("coupon_code")
    if not account.get("found"):
        return AgentResult(reply_text=REGISTER_PHONE_MESSAGE, intent="coupon_code", handoff_required=False)
    if account.get("lookup_error"):
        return AgentResult(reply_text=LOCAL_LOOKUP_UNAVAILABLE, intent="coupon_code", handoff_required=False, safety_reason="coupon_lookup_failed")

    code = account.get("coupon_code") or "indisponível"
    balance = account.get("balance_brl") or format_cents_to_brl(0)
    vip = get_vip_profile(message.sender_phone)
    if vip:
        nickname = pick_vip_nickname(vip, message.text)
        return AgentResult(
            reply_text=build_vip_coupon_reply(vip, nickname, code, balance),
            intent="coupon_code",
            handoff_required=False,
        )

    user_id = int(account["user_id"])
    preferences = get_user_preferences(user_id)
    display_name = resolve_display_name(account.get("name"), preferences)
    suffix = _personalized_suffix(user_id, preferences, message.sender_phone, display_name)
    return AgentResult(
        reply_text=(
            f"{_greeting(display_name)} Seu Cartão Presente: código *{code}* | saldo {balance}. "
            f"Use em {STORE_URL} no checkout. Código pessoal e intransferível.{suffix}"
        ),
        intent="coupon_code",
        handoff_required=False,
    )


def build_simulation_reply(message: IncomingMessage) -> AgentResult:
    from .simulation import (
        build_purchase_simulation_reply,
        detect_payment_method,
        parse_all_brl_cents,
        parse_product_price_cents,
    )

    account = find_coupon_balance_by_phone(message.sender_phone, message.text)
    if account.get("error") == "third_party_inquiry":
        return _third_party_reply()

    if account.get("found"):
        user_id = int(account["user_id"])
        preferences = get_user_preferences(user_id)
        display_name = resolve_display_name(account.get("name"), preferences)
        credit_cents = int(account.get("coupon_value_cents") or 0)
    else:
        user_id = None
        preferences = {}
        display_name = None
        credit_cents = 0
        amounts = parse_all_brl_cents(message.text)
        if len(amounts) == 1:
            credit_cents = amounts[0]
        elif len(amounts) >= 2:
            credit_cents = min(amounts)

    product_cents = parse_product_price_cents(message.text, credit_cents=credit_cents if credit_cents else None)
    payment_method = detect_payment_method(message.text)

    if not account.get("found") and credit_cents <= 0 and product_cents is None:
        return AgentResult(
            reply_text=(
                f"Para simular o uso do Cartão Presente, informe o valor do relógio (ex.: de R$ 10 mil) "
                f"ou cadastre seu telefone em {SITE_URL} para eu usar seu saldo real."
            ),
            intent="simulation",
            handoff_required=False,
        )

    if not account.get("found") and credit_cents <= 0 and product_cents is not None:
        return AgentResult(
            reply_text=(
                f"Consigo simular o desconto no produto de {format_cents_to_brl(product_cents)}, "
                f"mas preciso do seu saldo. Cadastre seu telefone em {SITE_URL} ou informe quanto quer aplicar "
                f"(ex.: R$ 800 de Cartão Presente)."
            ),
            intent="simulation",
            handoff_required=False,
        )

    reply_text = build_purchase_simulation_reply(
        credit_cents=credit_cents,
        product_cents=product_cents,
        payment_method=payment_method,
        display_name=display_name,
    )

    vip = get_vip_profile(message.sender_phone)
    if vip:
        nickname = pick_vip_nickname(vip, message.text)
        reply_text = build_vip_general_reply(
            vip,
            nickname,
            reply_text,
        )

    return AgentResult(
        reply_text=reply_text,
        intent="simulation",
        handoff_required=False,
    )


def build_current_raffle_reply(message: IncomingMessage | None = None) -> AgentResult:
    settings = get_settings()
    raffle = find_current_raffle()
    if raffle.get("lookup_error"):
        return AgentResult(
            reply_text=(
                f"Não consegui consultar o sorteio aberto agora. "
                f"Tente novamente em instantes ou acesse {SITE_URL}."
            ),
            intent="current_raffle",
            handoff_required=False,
            safety_reason="current_raffle_lookup_failed",
        )

    if raffle.get("error") == "database_not_configured":
        return AgentResult(
            reply_text=(
                f"Consulta de sorteio indisponível no momento. "
                f"Acompanhe a rodada aberta em {SITE_URL}."
            ),
            intent="current_raffle",
            handoff_required=False,
            safety_reason="database_not_configured",
        )

    if not raffle.get("found"):
        return AgentResult(
            reply_text=(
                f"No momento não há sorteio com status aberto. "
                f"Acompanhe novas rodadas em {SITE_URL}."
            ),
            intent="current_raffle",
            handoff_required=False,
        )

    lines = [
        f"Sorteio aberto: *{raffle.get('title') or 'Rodada aberta'}*.",
        f"Prêmio: {raffle.get('prize_name') or 'consulte o site'}.",
        f"Status: {raffle.get('status') or 'open'}.",
    ]
    if raffle.get("quota_price_brl"):
        lines.append(f"Valor do sorteio: {raffle['quota_price_brl']}.")
    available = raffle.get("available_numbers") or []
    if available:
        preview = _format_available_numbers_list(available, max_chars=280)
        lines.append(f"Números disponíveis ({len(available)}): {preview}.")
    lines.append(f"Participe em {SITE_URL}.")
    reply_text = " ".join(lines)
    if message:
        vip = get_vip_profile(message.sender_phone)
        if vip:
            nickname = pick_vip_nickname(vip, message.text)
            reply_text = build_vip_general_reply(vip, nickname, reply_text)
    return AgentResult(
        reply_text=reply_text[: settings.max_reply_chars],
        intent="current_raffle",
        handoff_required=False,
    )


def _format_available_numbers_list(numbers: list[str], max_chars: int) -> str:
    if not numbers:
        return "Nenhum número disponível no momento."

    joined = ", ".join(numbers)
    if len(joined) <= max_chars:
        return joined

    shown: list[str] = []
    for number in numbers:
        candidate = ", ".join(shown + [number])
        if len(candidate) > max_chars:
            break
        shown.append(number)

    hidden = len(numbers) - len(shown)
    if not shown:
        return f"{numbers[0]}… (+{len(numbers) - 1} números; veja a lista completa em {SITE_URL})"
    if hidden > 0:
        return f"{', '.join(shown)}… (+{hidden} números; lista completa em {SITE_URL})"
    return ", ".join(shown)


def build_available_numbers_reply(message: IncomingMessage) -> AgentResult:
    settings = get_settings()
    result = find_available_numbers_for_open_draw()

    if result.get("error") == "database_not_configured":
        return AgentResult(
            reply_text=(
                f"Consulta de números indisponível no momento. "
                f"Veja a grade em {SITE_URL}."
            ),
            intent="available_numbers",
            handoff_required=False,
            safety_reason="database_not_configured",
        )
    if result.get("lookup_error"):
        return AgentResult(
            reply_text=(
                f"Não consegui listar os números agora. "
                f"Consulte a grade disponível em {SITE_URL}."
            ),
            intent="available_numbers",
            handoff_required=False,
            safety_reason="available_numbers_lookup_failed",
        )
    if result.get("error") == "no_open_draw":
        return AgentResult(
            reply_text=(
                f"No momento não há sorteio aberto. Acompanhe novas rodadas em {SITE_URL} "
                f"ou fale com a equipe no WhatsApp {NS_SALES_WHATSAPP}."
            ),
            intent="available_numbers",
            handoff_required=False,
        )

    title = result.get("title") or "Sorteio atual"
    available = result.get("available_numbers") or []
    numbers_text = _format_available_numbers_list(available, max_chars=650)
    count = len(available)
    prize_line = ""
    if result.get("prize_name"):
        prize_line = f"Prêmio: {result['prize_name']}. "
    if result.get("price_brl"):
        prize_line += f"Valor: {result['price_brl']}. "

    if count == 0:
        if result.get("total_count") is None:
            reply_text = (
                f"Sorteio *{title}* aberto. Consulte a grade de números disponíveis em {SITE_URL}."
            )
        else:
            reply_text = (
                f"No sorteio *{title}*, todos os números já foram confirmados (pagamento aprovado). "
                f"Acompanhe novas rodadas em {SITE_URL}."
            )
    else:
        reply_text = (
            f"Sorteio *{title}*. {prize_line}"
            f"{count} número(s) disponível(is): {numbers_text}. "
            f"Escolha e participe em {SITE_URL}. A vaga só confirma após compensação do pagamento."
        )

    vip = get_vip_profile(message.sender_phone)
    if vip:
        nickname = pick_vip_nickname(vip, message.text)
        reply_text = build_vip_general_reply(vip, nickname, reply_text)

    return AgentResult(
        reply_text=reply_text[: settings.max_reply_chars],
        intent="available_numbers",
        handoff_required=False,
    )


def build_raffle_history_reply(message: IncomingMessage) -> AgentResult:
    account = find_coupon_balance_by_phone(message.sender_phone, message.text)
    if account.get("error") == "third_party_inquiry":
        return _third_party_reply()
    if not account.get("found"):
        return AgentResult(reply_text=REGISTER_PHONE_MESSAGE, intent="raffle_history", handoff_required=False)

    user_id = int(account["user_id"])
    last_payment = find_last_payment_participation(user_id)
    history = find_user_raffle_participation(user_id)

    if detect_last_participation_inquiry(message.text) and last_payment.get("found"):
        reply_text = _build_last_participation_reply(last_payment)
        vip = get_vip_profile(message.sender_phone)
        if vip:
            nickname = pick_vip_nickname(vip, message.text)
            reply_text = build_vip_general_reply(vip, nickname, reply_text)
        return AgentResult(
            reply_text=reply_text,
            intent="raffle_history",
            handoff_required=False,
        )

    if history.get("lookup_error") and last_payment.get("lookup_error"):
        return AgentResult(reply_text=LOCAL_LOOKUP_UNAVAILABLE, intent="raffle_history", handoff_required=False, safety_reason="raffle_history_lookup_failed")

    if history.get("found"):
        chunks: list[str] = []
        for item in history.get("items", [])[:5]:
            parts = [item.get("title") or "Sorteio"]
            if item.get("participated_at"):
                parts.append(f"em {item['participated_at'][:10]}")
            if item.get("numbers"):
                parts.append(f"seus números: {item['numbers']}")
            if item.get("amount_brl"):
                parts.append(f"valor {item['amount_brl']}")
            if item.get("winning_number"):
                parts.append(f"número sorteado: {item['winning_number']}")
            chunks.append(" | ".join(parts))

        reply_text = "Suas participações recentes: " + " // ".join(chunks)
        vip = get_vip_profile(message.sender_phone)
        if vip:
            nickname = pick_vip_nickname(vip, message.text)
            reply_text = build_vip_general_reply(vip, nickname, reply_text)

        return AgentResult(
            reply_text=reply_text,
            intent="raffle_history",
            handoff_required=False,
        )

    if last_payment.get("found"):
        return AgentResult(
            reply_text=_build_last_participation_reply(last_payment),
            intent="raffle_history",
            handoff_required=False,
        )

    return AgentResult(
        reply_text=(
            f"Ainda não encontramos participações aprovadas no seu cadastro. "
            f"Confira sorteios passados e resultados em {SITE_URL}."
        ),
        intent="raffle_history",
        handoff_required=False,
    )


def build_rules_reply_result(message: IncomingMessage | None = None) -> AgentResult:
    reply_text = build_rules_reply()
    if message:
        vip = get_vip_profile(message.sender_phone)
        if vip:
            nickname = pick_vip_nickname(vip, message.text)
            reply_text = build_vip_general_reply(
                vip,
                nickname,
                f"As regras oficiais valem até para o fundador: {reply_text}",
            )
    return AgentResult(
        reply_text=reply_text,
        intent="rules_faq",
        handoff_required=False,
    )
