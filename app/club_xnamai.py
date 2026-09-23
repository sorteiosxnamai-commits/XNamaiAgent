"""Deterministic information and a restrained offer for XNaMai Club."""

from __future__ import annotations

import unicodedata
from typing import Any

from .commerce_context import CommerceConversationState
from .models import AgentResult
from .site_knowledge import CLUB_URL, STORE_URL
from .club_provider import PublicPlan


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


def is_club_followup(text: str | None, state: CommerceConversationState) -> bool:
    return state.club_flow_stage in {"awaiting_account", "awaiting_subscription"} and _fold(text).strip(" ?!.") in {
        "sim", "nao", "tenho", "nao tenho", "ja tenho", "ainda nao",
    }


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
    plans: list[PublicPlan] | None = None,
) -> AgentResult | None:
    if not is_club_request(text) and not is_club_followup(text, state):
        return None

    answer = _fold(text).strip(" ?!.")
    membership = detect_club_membership_statement(text)
    stage = "complete"
    if is_club_followup(text, state) and state.club_flow_stage == "awaiting_account":
        if answer in {"sim", "tenho", "ja tenho"}:
            reply = "Você já possui uma assinatura do Club? Confira o status na sua conta antes de escolher outro plano."
            stage = "awaiting_subscription"
        else:
            reply = (
                f"Crie sua conta no XNaMai Club em {CLUB_URL}. O cadastro exige nome, "
                "e-mail, senha, cidade, estado e CPF/CNPJ válido. Depois de entrar, "
                "escolha o plano e conclua o checkout no Club."
            )
    elif is_club_followup(text, state) and state.club_flow_stage == "awaiting_subscription":
        if answer in {"sim", "tenho", "ja tenho"}:
            reply = f"Entre em {CLUB_URL} e confira a assinatura no seu painel. Não consigo verificar seu status por esta conversa."
        else:
            reply = f"Entre em {CLUB_URL}, escolha um plano disponível e conclua o checkout na sua conta. O Club confirma a assinatura após o pagamento."
    elif membership == "member":
        reply = (
            "Se você já tem assinatura, entre na sua conta do XNaMai Club para "
            "conferir o status em Assinaturas. Não consigo confirmar sua assinatura "
            f"por esta conversa. Club: {CLUB_URL}"
        )
    else:
        intro = "Para ser membro do XNaMai Club, escolha um plano e entre ou crie sua conta no Club."
        if plans:
            descriptions = [
                f"{plan.name}: R$ {plan.monthly_price_cents / 100:.2f}/mês".replace(".", ",")
                if plan.monthly_price_cents is not None else plan.name
                for plan in plans
            ]
            intro += " Planos atuais: " + "; ".join(descriptions) + "."
        else:
            intro += " Consulte os planos atuais no Club."
        reply = (
            f"{intro} Você já tem conta no Club? Acesse {CLUB_URL} para fazer login "
            "ou cadastro; escolha o plano e conclua o checkout na sua conta. "
            "A assinatura deve ser confirmada pelo Club."
        )
        stage = "awaiting_account"

    return AgentResult(
        reply_text=reply,
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "active_topic": "xnamai_club",
            "club_offer_shown": True,
            "club_membership_status": membership or state.club_membership_status,
            "club_flow_stage": stage,
            "used_openai_interpreter": False,
            "used_openai_responder": False,
            "used_commerce_provider": False,
        },
    )
