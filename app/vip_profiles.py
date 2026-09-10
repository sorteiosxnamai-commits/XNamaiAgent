"""Perfis VIP reconhecidos pelo agente.

Parte 1 da migração XNamai: o registro legado (pessoas e apelidos da marca
anterior) foi removido. Não há perfil VIP oficial configurado — `VIP_PROFILES`
fica vazio e `get_vip_profile` passa a devolver `None` sempre.

Preservados de propósito por serem regra genérica, não conteúdo de marca:
- normalização de telefone e guarda de telefone vazio em `get_vip_profile`;
- casamento por sufixo de telefone (E.164 parcial);
- fallback determinístico de apelido em `pick_vip_nickname`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .repository import normalize_phone


@dataclass(frozen=True)
class VipProfile:
    phone_suffix: str
    full_name: str
    title: str
    nicknames: tuple[str, ...]


#: Nenhum perfil VIP configurado.
VIP_PROFILES: tuple[VipProfile, ...] = ()


def get_vip_profile(phone: str | None) -> VipProfile | None:
    normalized = normalize_phone(phone)
    if not normalized:
        return None

    for profile in VIP_PROFILES:
        suffix = profile.phone_suffix
        if normalized == suffix or normalized.endswith(suffix) or normalized.endswith(suffix[-9:]):
            return profile
    return None


def pick_vip_nickname(profile: VipProfile, seed: str | None = None) -> str:
    if not profile.nicknames:
        return profile.full_name.split()[0]
    index = sum(ord(ch) for ch in (seed or profile.full_name)) % len(profile.nicknames)
    return profile.nicknames[index]


def build_vip_greeting(profile: VipProfile, nickname: str) -> str:
    return f"Olá, {nickname}! {profile.full_name}, {profile.title}. Atendimento prioritário."


def build_vip_balance_reply(
    profile: VipProfile,
    nickname: str,
    balance_brl: str,
    extra: str = "",
) -> str:
    lines = [
        build_vip_greeting(profile, nickname),
        f"Seu saldo, {nickname}, está em {balance_brl}.",
    ]
    if extra:
        lines.append(extra)
    lines.append("Posso ajudar em mais alguma coisa?")
    return " ".join(lines)


def build_vip_coupon_reply(
    profile: VipProfile,
    nickname: str,
    code: str,
    balance_brl: str,
) -> str:
    return (
        f"{build_vip_greeting(profile, nickname)} "
        f"Código: *{code}* | saldo {balance_brl}."
    )


def build_vip_general_reply(profile: VipProfile, nickname: str, base_text: str) -> str:
    return f"{nickname}, {base_text}"


def build_vip_openai_context(profile: VipProfile, nickname: str) -> str:
    nicknames = ", ".join(f'"{item}"' for item in profile.nicknames)
    return f"""
Cliente VIP identificado:
- Nome: {profile.full_name}
- Cargo: {profile.title}
- Apelidos: {nicknames}
- Apelido sugerido nesta conversa: {nickname}

Tom obrigatório: cordial e respeitoso. Respostas curtas para WhatsApp.
""".strip()
