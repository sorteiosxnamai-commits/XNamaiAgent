from __future__ import annotations

import re

BALANCE_KEYWORDS = (
    "saldo",
    "meu saldo",
    "consultar saldo",
    "ver saldo",
    "quanto tenho",
    "valor do cupom",
)

COUPON_CODE_KEYWORDS = (
    "codigo do cupom",
    "código do cupom",
    "numero do cupom",
    "número do cupom",
    "meu cupom",
    "codigo do cartao",
    "código do cartão",
    "cartao presente",
    "cartão presente",
)

SIMULATION_KEYWORDS = (
    "simular",
    "simulação",
    "simulacao",
    "quanto posso usar",
    "tabela de uso",
    "simulador",
)

AVAILABLE_NUMBERS_KEYWORDS = (
    "numeros disponiveis",
    "números disponíveis",
    "numeros disponiveis no sorteio",
    "números disponíveis no sorteio",
    "quais numeros estao disponiveis",
    "quais números estão disponíveis",
    "quais numeros disponiveis",
    "quais números disponíveis",
    "numeros livres",
    "números livres",
    "numeros abertos",
    "números abertos",
    "ver numeros do sorteio",
    "ver números do sorteio",
    "numeros do sorteio atual",
    "números do sorteio atual",
)

CURRENT_RAFFLE_KEYWORDS = (
    "sorteio atual",
    "rodada atual",
    "sorteio aberto",
    "qual sorteio esta aberto",
    "qual sorteio está aberto",
    "que sorteio esta aberto",
    "que sorteio está aberto",
    "sorteio esta aberto",
    "sorteio está aberto",
    "premio atual",
    "prêmio atual",
)

RAFFLE_HISTORY_KEYWORDS = (
    "sorteios passados",
    "sorteio passado",
    "ultimo sorteio",
    "último sorteio",
    "ultima participacao",
    "última participação",
    "vencedor",
    "numero sorteado",
    "número sorteado",
    "meus numeros",
    "meus números",
    "participei",
    "participação",
    "participacao",
    "resultado do sorteio",
    "sorteios que participei",
    "sorteio que participei",
)

RULES_KEYWORDS = (
    "como funciona",
    "regras",
    "faq",
    "duvida",
    "dúvida",
    "cartao presente",
    "cartão presente digital",
    "lotomania",
)

HUMAN_SUPPORT_KEYWORDS = (
    "falar com atendente",
    "falar com um atendente",
    "atendente humano",
    "atendimento humano",
    "falar com alguem",
    "falar com alguém",
    "falar com a equipe",
    "falar com vocês",
    "falar com voces",
    "quero um humano",
    "quero atendente",
    "preciso de ajuda",
    "contato de vendas",
    "falar com vendas",
    "equipe de vendas",
    "whatsapp da loja",
    "numero da loja",
    "número da loja",
    "telefone da loja",
    "contato da new store",
    "contato new store",
)

TRADE_IN_KEYWORDS = (
    "seminovo",
    "semi novo",
    "semi-novo",
    "usado",
    "usados",
    "segunda mao",
    "segunda mão",
    "troca",
    "trocar",
    "permuta",
    "avaliacao",
    "avaliação",
    "avaliar",
    "estao comprando",
    "estão comprando",
    "voces compram",
    "vocês compram",
    "vcs compram",
    "compram relogio",
    "compram relógio",
    "aceitam troca",
    "aceita troca",
    "trade in",
    "trade-in",
)


def detect_balance_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in BALANCE_KEYWORDS)


def detect_coupon_code_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in COUPON_CODE_KEYWORDS)


def detect_simulation_inquiry(text: str) -> bool:
    from .simulation import detect_purchase_simulation_inquiry

    normalized = (text or "").lower()
    if any(keyword in normalized for keyword in SIMULATION_KEYWORDS):
        return True
    return detect_purchase_simulation_inquiry(text)


def detect_commerce_inquiry(text: str | None) -> bool:
    normalized = (text or "").lower()
    if not normalized:
        return False
    phrases = (
        "tem estoque", "tem produto", "vocês têm", "voces tem", "vocês tem",
        "vende", "quanto custa", "qual o preço", "qual o preco", "preço",
        "preco", "quanto fica", "disponibilidade", "referência", "referencia", "sku", "ean",
        "pix", "parcelamento", "parcelar", "promoção", "promocao",
        "cupom comercial", "produto", "produtos", "relógio", "relogio",
        "marca", "modelo",
    )
    unicode_phrases = (
        "voc\u00eas t\u00eam", "voc\u00eas tem", "qual o pre\u00e7o", "pre\u00e7o",
        "disponibilidade", "refer\u00eancia", "promo\u00e7\u00e3o", "rel\u00f3gio",
    )
    return any(phrase in normalized for phrase in phrases + unicode_phrases)


def detect_current_raffle_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in CURRENT_RAFFLE_KEYWORDS)


def detect_available_numbers_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in AVAILABLE_NUMBERS_KEYWORDS)


def detect_raffle_history_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in RAFFLE_HISTORY_KEYWORDS)


def detect_last_participation_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    phrases = (
        "ultimo sorteio",
        "último sorteio",
        "ultima participacao",
        "última participação",
        "ultimo que participei",
        "último que participei",
    )
    return any(phrase in normalized for phrase in phrases)


def detect_rules_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in RULES_KEYWORDS)


def detect_human_support_request(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in HUMAN_SUPPORT_KEYWORDS)


def detect_trade_in_or_appraisal_request(text: str) -> bool:
    """Customer wants to sell, trade or appraise a watch — human sales handoff."""
    normalized = (text or "").lower()
    if not normalized:
        return False
    if any(keyword in normalized for keyword in TRADE_IN_KEYWORDS):
        # Avoid false positives like "trocar o estado" / cart quantity wording alone.
        commerce_cue = any(
            term in normalized
            for term in (
                "relogio",
                "relógio",
                "certina",
                "tissot",
                "seiko",
                "omega",
                "tag",
                "kuoe",
                "marca",
                "modelo",
                "peca",
                "peça",
                "seminovo",
                "usado",
                "avali",
                "compra",
                "comprando",
                "compram",
            )
        )
        if commerce_cue or any(
            term in normalized
            for term in ("seminovo", "semi novo", "usado", "avaliacao", "avaliação", "avaliar")
        ):
            return True
    # Explicit "are you buying X?" patterns.
    if re.search(
        r"\b(est[aã]o|vcs|voc[eê]s?)\s+comprando\b",
        normalized,
    ) and any(term in normalized for term in ("relogio", "relógio", "seminovo", "usado")):
        return True
    return False


BLOCKED_TOPICS = (
    "comprar número",
    "comprar numeros",
    "apostar",
    "aposta",
    "bet",
    "jogar dinheiro",
    "ganhar prêmio",
    "garantir prêmio",
)


def detect_blocked_request(text: str) -> str | None:
    normalized = (text or "").lower()
    for topic in BLOCKED_TOPICS:
        if topic in normalized:
            return f"blocked_topic:{topic}"
    return None


def default_safe_handoff() -> str:
    from .site_knowledge import HUMAN_SUPPORT_MESSAGE, SITE_URL

    return (
        "Para sua segurança, vou encaminhar esse atendimento para a equipe da New Store. "
        f"{HUMAN_SUPPORT_MESSAGE} "
        f"Você também pode acessar sua conta em {SITE_URL}."
    )
