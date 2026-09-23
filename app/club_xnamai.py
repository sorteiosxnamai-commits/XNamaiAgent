"""Deterministic information and a restrained offer for XNaMai Club."""

from __future__ import annotations

import unicodedata
from typing import Any

from .commerce_context import CommerceConversationState
from .models import AgentResult
from .site_knowledge import CLUB_URL, STORE_URL


_CLUB_TERMS = (
    "club xnamai",
    "clube xnamai",
    "xnamai club",
    "preco de membro",
    "precos de membro",
    "valor de membro",
    "valores de membro",
    "quero ser membro",
    "quero virar membro",
    "assinar o club",
    "assinar o clube",
    "cadastrar no club",
    "cadastrar no clube",
    "cadastro no club",
    "cadastro no clube",
)
_MEMBER_TERMS = (
    "ja sou membro do club",
    "sou membro do club",
    "ja sou membro do clube",
    "sou membro do clube",
    "ja sou membro xnamai",
    "sou membro xnamai",
    "ja tenho o club",
    "tenho o club",
    "ja assino o club",
    "assino o club",
    "ja assino o clube",
    "assino o clube",
)
_NON_MEMBER_TERMS = (
    "nao sou membro do club",
    "nao sou membro do clube",
    "nao sou membro xnamai",
    "nao tenho o club",
    "nao assino o club",
    "nao assino o clube",
    "ainda nao sou membro do club",
    "ainda nao sou membro do clube",
    "ainda nao sou membro xnamai",
)


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(
        character for character in normalized
        if not unicodedata.combining(character)
    ).strip()


def detect_club_membership_statement(text: str | None) -> str | None:
    folded = _fold(text)
    if any(term in folded for term in _NON_MEMBER_TERMS):
        return "non_member"
    if any(term in folded for term in _MEMBER_TERMS):
        return "member"
    return None


def is_club_request(text: str | None) -> bool:
    folded = _fold(text)
    return bool(
        detect_club_membership_statement(text)
        or any(term in folded for term in _CLUB_TERMS)
    )


def club_offer_text() -> str:
    return (
        "Se você ainda não é membro, conheça o XNaMai Club: membros com assinatura "
        "ativa têm acesso a preços exclusivos em compras elegíveis. Catálogo oficial: "
        f"{STORE_URL} | Plano e regras atuais do Club: {CLUB_URL}"
    )


def maybe_append_club_offer(
    reply_text: str,
    *,
    state: CommerceConversationState | None,
) -> tuple[str, bool]:
    """Append the offer once, unless the customer already declared membership."""
    if state is not None and (
        state.club_offer_shown or state.club_membership_status == "member"
    ):
        return reply_text, False
    return f"{reply_text}\n\n{club_offer_text()}", True


def handle_club_turn(
    text: str | None,
    *,
    state: CommerceConversationState,
) -> AgentResult | None:
    if not is_club_request(text):
        return None

    membership = detect_club_membership_statement(text)
    if membership == "member":
        reply = (
            "Perfeito. Como membro do XNaMai Club, entre com sua conta para acessar "
            f"os preços exclusivos. Catálogo: {STORE_URL} | Club: {CLUB_URL}"
        )
    else:
        reply = (
            "O XNaMai Club é o clube empresarial da Xnamai. Membros com assinatura "
            "ativa têm preços exclusivos em compras elegíveis. As condições podem "
            "mudar e os preços do Club "
            "não são cumulativos com outros descontos, salvo informação expressa. "
            f"Confira o plano e as regras atuais em {CLUB_URL}"
        )

    return AgentResult(
        reply_text=reply,
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "active_topic": "xnamai_club",
            "club_offer_shown": True,
            "club_membership_status": membership or state.club_membership_status,
            "used_openai_interpreter": False,
            "used_openai_responder": False,
            "used_commerce_provider": False,
        },
    )
