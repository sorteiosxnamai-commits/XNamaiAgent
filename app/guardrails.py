from __future__ import annotations

import re

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
    """Recusa segura de topico bloqueado.

    Nao usa `site_knowledge`: aquele modulo carrega o contato e a URL da marca
    legada, e encaminhar o cliente da XNamai para la seria mandar gente para a
    empresa errada. O arquivo continua intocado no repositorio.
    """
    return (
        "Para sua segurança, não posso seguir com esse assunto por aqui. "
        "Vou encaminhar seu atendimento para a equipe da XNamai."
    )
