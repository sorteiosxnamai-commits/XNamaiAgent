from __future__ import annotations

from dataclasses import dataclass

from .repository import normalize_phone


@dataclass(frozen=True)
class VipProfile:
    phone_suffix: str
    full_name: str
    title: str
    nicknames: tuple[str, ...]


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
    return f"Olá, {nickname}! Como posso ajudar?"


def build_vip_balance_reply(profile: VipProfile, nickname: str, balance_brl: str, extra: str = "") -> str:
    return f"{build_vip_greeting(profile, nickname)} {profile.full_name}, seu saldo informado é {balance_brl}. {extra}".strip()


def build_vip_coupon_reply(profile: VipProfile, nickname: str, code: str, balance_brl: str) -> str:
    return f"{nickname}, código: {code}. Saldo informado: {balance_brl}."


def build_vip_general_reply(profile: VipProfile, nickname: str, base_text: str) -> str:
    return f"{nickname}, {base_text}"


def build_vip_openai_context(profile: VipProfile, nickname: str) -> str:
    nicknames = ", ".join(f'"{item}"' for item in profile.nicknames)
    return f"""
Cliente VIP identificado:
- Nome: {profile.full_name}
- Cargo: {profile.title}
- Apelidos oficiais: {nicknames}
- Apelido sugerido nesta conversa: {nickname}

Tom: cordial e respeitoso, sem presumir vínculo com a empresa.
Pode usar humor leve com os apelidos, sem exagero ofensivo. Respostas curtas para WhatsApp.
""".strip()
