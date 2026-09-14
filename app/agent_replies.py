from __future__ import annotations

from typing import Any

from .models import AgentResult, IncomingMessage
from .site_knowledge import THIRD_PARTY_REFUSAL
from .user_preferences import detect_preferred_name_update, save_preferred_name
from .vip_profiles import get_vip_profile, pick_vip_nickname

# Parte 1: o dominio de sorteio (saldo, Cartao Presente, simulacao, historico,
# rodada aberta, numeros disponiveis, regulamento) saiu do runtime junto com o
# banco que o alimentava. Sobram aqui apenas dois comportamentos genericos, que
# nao pertencem a sorteio algum:
#
#   - registrar como o cliente prefere ser chamado;
#   - recusar consulta a conta de terceiro (regra de privacidade).
#
# O texto de persona que menciona sorteios continua na persona protegida, mas
# nao aciona mais nenhuma feature executavel.


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


def _third_party_reply() -> AgentResult:
    return AgentResult(
        reply_text=THIRD_PARTY_REFUSAL,
        intent="security_refusal",
        handoff_required=False,
        safety_reason="third_party_account_inquiry",
    )
