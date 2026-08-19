from __future__ import annotations

import re
import unicodedata

from app.commerce_context import CommerceConversationState


def confirmation_text_kind(
    state: CommerceConversationState,
    text: str,
) -> str | None:
    """Recognize a short final answer only for an already prepared order review."""
    if not (
        state.pending_action == "awaiting_order_confirmation"
        and state.order_confirmation_status == "pending"
        and state.order_review_version
    ):
        return None
    folded = "".join(
        char
        for char in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(char)
    )
    normalized = " ".join(re.findall(r"[a-z0-9]+", folded))
    if not normalized:
        return None
    explicit_change = any(
        term in normalized
        for term in (
            "cartao",
            "pix",
            "boleto",
            "pagamento",
            "quantidade",
            "produto",
            "endereco",
            "frete",
            "trocar",
            "alterar",
            "mudar",
        )
    )
    if explicit_change or " mas " in f" {normalized} ":
        return "change"
    if normalized in {"nao", "nao confirma", "cancela", "cancelar", "nao quero"}:
        return "reject"
    if normalized in {
        "sim",
        "confirmo",
        "confirmado",
        "pode finalizar",
        "pode concluir",
        "pode prosseguir",
        "finaliza",
        "pode fazer",
    }:
        return "confirm"
    return None
