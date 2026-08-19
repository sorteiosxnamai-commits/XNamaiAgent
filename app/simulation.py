from __future__ import annotations

import re
from typing import Any

from .repository import format_cents_to_brl
from .site_knowledge import (
    CARD_USAGE_TABLE,
    credit_band_for_amount,
    max_applicable_credit_for_product_cents,
    min_purchase_for_credit_cents,
)


PURCHASE_SIMULATION_PHRASES = (
    "quanto abateria",
    "quanto abate",
    "quanto abater",
    "consigo comprar",
    "consigo usar",
    "quanto fica",
    "valor a pagar",
    "quanto eu pago",
    "quanto pagaria",
    "usar meu saldo",
    "usar o saldo",
    "usar meu cartão",
    "usar meu cartao",
    "usar o cartão",
    "usar o cartao",
    "aplicar o cartão",
    "aplicar o cartao",
    "aplicar meu cartão",
    "aplicar meu cartao",
    "abater do valor",
    "abateria do valor",
    "abater todo o saldo",
    "abater o saldo",
    "desconto do cartão",
    "desconto do cartao",
    "quanto desconta",
    "quanto descontaria",
)


def detect_purchase_simulation_inquiry(text: str | None) -> bool:
    normalized = (text or "").lower()
    if not normalized:
        return False

    if any(phrase in normalized for phrase in PURCHASE_SIMULATION_PHRASES):
        return True

    credit_signals = ("saldo", "cartão presente", "cartao presente", "crédito", "credito", "cupom")
    product_signals = ("relogio", "relógio", "reloj", "compra", "produto", "mil", "r$")
    has_credit_signal = any(signal in normalized for signal in credit_signals)
    has_product_signal = any(signal in normalized for signal in product_signals)
    has_price = parse_product_price_cents(text) is not None

    return has_credit_signal and (has_product_signal or has_price)


def parse_all_brl_cents(text: str | None) -> list[int]:
    if not text:
        return []

    normalized = text.lower()
    amounts: list[int] = []

    for match in re.finditer(r"(\d{1,3}(?:[.,]\d+)?)\s*mil\b", normalized):
        value = float(match.group(1).replace(",", "."))
        amounts.append(int(value * 1000 * 100))

    money_patterns = (
        r"r\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)",
        r"(\d{1,3}(?:\.\d{3})+(?:,\d{2})?)",
        r"(\d+(?:,\d{2})?)",
    )
    for pattern in money_patterns:
        for match in re.finditer(pattern, normalized):
            raw = match.group(1)
            if "mil" in normalized[max(0, match.start() - 6): match.end() + 4]:
                continue
            try:
                if "," in raw and "." in raw:
                    normalized_amount = raw.replace(".", "").replace(",", ".")
                elif "," in raw:
                    normalized_amount = raw.replace(",", ".")
                else:
                    normalized_amount = raw.replace(".", "")
                value = float(normalized_amount)
            except ValueError:
                continue
            cents = int(round(value * 100))
            if cents >= 100:
                amounts.append(cents)

    deduped: list[int] = []
    for cents in amounts:
        if cents not in deduped:
            deduped.append(cents)
    return deduped


def parse_stated_credit_cents(text: str | None, account_credit_cents: int | None = None) -> int | None:
    if not text:
        return account_credit_cents

    normalized = text.lower()
    patterns = (
        r"(?:se eu tiver|se tiver|com|tendo|ter)\s+(\d+(?:[.,]\d+)?)\s*mil\s+de\s+saldo",
        r"(\d+(?:[.,]\d+)?)\s*mil\s+de\s+saldo",
        r"saldo de\s+(\d+(?:[.,]\d+)?)\s*mil",
        r"(?:se eu tiver|com|tendo)\s+r\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)\s+de\s+saldo",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            raw = match.group(1)
            if "mil" in pattern:
                value = float(raw.replace(",", "."))
                return int(value * 1000 * 100)
            if "," in raw and "." in raw:
                value = float(raw.replace(".", "").replace(",", "."))
            elif "," in raw:
                value = float(raw.replace(",", "."))
            else:
                value = float(raw.replace(".", ""))
            return int(round(value * 100))

    if account_credit_cents is not None:
        return account_credit_cents
    return None


def resolve_simulation_credit_cents(text: str | None, account_credit_cents: int | None = None) -> int:
    stated = parse_stated_credit_cents(text, account_credit_cents)
    return max(int(stated or 0), 0)


def parse_product_price_cents(text: str | None, credit_cents: int | None = None) -> int | None:
    if text:
        normalized = text.lower()
        product_patterns = (
            r"(?:relogio|relógio|reloj)(?:\s+\w+){0,3}\s+de\s+(\d+(?:[.,]\d+)?)\s*mil",
            r"comprar(?:\s+\w+){0,4}\s+de\s+(\d+(?:[.,]\d+)?)\s*mil",
        )
        for pattern in product_patterns:
            match = re.search(pattern, normalized)
            if match:
                value = float(match.group(1).replace(",", "."))
                return int(value * 1000 * 100)

    amounts = parse_all_brl_cents(text)
    if not amounts:
        return None

    candidates = [amount for amount in amounts if credit_cents is None or amount != credit_cents]
    if not candidates:
        return max(amounts)
    return max(candidates)


def detect_payment_method(text: str | None) -> str | None:
    normalized = (text or "").lower()
    if any(token in normalized for token in ("pix", "à vista", "a vista", "avista")):
        return "pix"
    if any(token in normalized for token in ("crédito", "credito", "parcelado", "parcela", "12x")):
        return "credit"
    return None


def simulate_purchase(credit_cents: int, product_cents: int) -> dict[str, Any]:
    max_applicable = max_applicable_credit_for_product_cents(product_cents)
    applied_cents = min(max(credit_cents, 0), max_applicable, max(product_cents, 0))
    final_cents = max(product_cents - applied_cents, 0)
    can_apply_full_balance = credit_cents > 0 and applied_cents >= credit_cents
    min_purchase_cents = min_purchase_for_credit_cents(applied_cents) if applied_cents else None

    eligible = True
    reason = None
    if credit_cents < CARD_USAGE_TABLE[0][0]:
        eligible = False
        reason = (
            f"O simulador oficial considera saldos a partir de {format_cents_to_brl(CARD_USAGE_TABLE[0][0])}. "
            "Confirme seu saldo ou fale com a equipe."
        )
    elif max_applicable <= 0:
        eligible = False
        reason = (
            f"Para usar Cartão Presente, o produto deve ser superior a "
            f"{format_cents_to_brl(CARD_USAGE_TABLE[0][2])} (tabela oficial)."
        )
    elif applied_cents <= 0:
        eligible = False
        band = credit_band_for_amount(credit_cents)
        if band:
            _, _, min_required = band
            reason = (
                f"Para aplicar {format_cents_to_brl(credit_cents)}, a compra deve ser superior a "
                f"{format_cents_to_brl(min_required)} (tabela oficial)."
            )
        else:
            reason = (
                "Neste valor de produto, a tabela permite aplicar até "
                f"{format_cents_to_brl(max_applicable)}, inferior ao saldo informado."
            )

    return {
        "eligible": eligible,
        "reason": reason,
        "credit_cents": credit_cents,
        "product_cents": product_cents,
        "max_applicable_cents": max_applicable,
        "applied_cents": applied_cents if eligible else 0,
        "final_cents": final_cents if eligible else product_cents,
        "min_purchase_cents": min_purchase_cents,
        "can_apply_full_balance": can_apply_full_balance if eligible else False,
        "remaining_balance_cents": max(credit_cents - applied_cents, 0) if eligible else credit_cents,
    }


def build_purchase_simulation_reply(
    credit_cents: int,
    product_cents: int | None = None,
    payment_method: str | None = None,
    display_name: str | None = None,
) -> str:
    credit_label = format_cents_to_brl(credit_cents)
    greeting = f"Olá, {display_name.strip()}!" if (display_name or "").strip() else "Olá!"

    if product_cents is None:
        min_purchase = min_purchase_for_credit_cents(credit_cents)
        if min_purchase is None:
            return (
                f"{greeting} Com crédito de {credit_label}, consulte a tabela completa em "
                "https://www.sorteionewstore.com.br/ ou informe o valor do relógio para eu simular o desconto."
            )
        return (
            f"{greeting} Com {credit_label} de Cartão Presente, a compra deve ser superior a "
            f"{format_cents_to_brl(min_purchase)} (tabela oficial). "
            "Informe o valor do produto (ex.: relógio de R$ 6.799,99) que eu calculo quanto abate e quanto fica a pagar."
        )

    product_label = format_cents_to_brl(product_cents)
    result = simulate_purchase(credit_cents, product_cents)

    if not result["eligible"]:
        return (
            f"{greeting} Seu saldo é {credit_label}. "
            f"{result['reason']} "
            f"O produto informado ficou em {product_label}."
        )

    applied_label = format_cents_to_brl(result["applied_cents"])
    final_label = format_cents_to_brl(result["final_cents"])
    max_applicable_label = format_cents_to_brl(result["max_applicable_cents"])
    remaining_label = format_cents_to_brl(result["remaining_balance_cents"])

    if result["can_apply_full_balance"]:
        summary = (
            f"{greeting} Sim, dá para abater todo o saldo de {credit_label} "
            f"no relógio de {product_label}."
        )
    else:
        summary = (
            f"{greeting} Não dá para abater todo o saldo de {credit_label} "
            f"no relógio de {product_label}. "
            f"Pela tabela oficial, nesta compra o máximo aplicável é {max_applicable_label}."
        )

    lines = [
        summary,
        "",
        "Simulação:",
        f"• Valor do produto: {product_label}",
        f"• Saldo informado: {credit_label}",
        f"• Máximo aplicável nesta compra (tabela): {max_applicable_label}",
        f"• Cartão Presente aplicado: {applied_label}",
        f"• Valor a pagar: {final_label}",
    ]

    if result["remaining_balance_cents"] > 0:
        lines.append(f"• Saldo que sobra no cartão: {remaining_label}")

    if payment_method == "credit" and result["final_cents"] > 0:
        installment = result["final_cents"] / 12
        lines.append(f"• Referência no crédito: em até 12x de {format_cents_to_brl(int(installment))} sem juros")

    lines.extend(
        [
            "",
            "A tabela limita quanto do cartão pode ser usado conforme o valor do produto; "
            "o saldo disponível pode ser maior que o permitido na compra.",
            "O desconto segue a forma de pagamento escolhida (Pix ou crédito). Compras via Pix podem precisar de aplicação manual pela equipe.",
            "Válido em compra única; dá para usar só parte do saldo. Simule também em https://www.newstorerj.com.br/",
        ]
    )
    return "\n".join(lines)
