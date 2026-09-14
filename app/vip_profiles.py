from __future__ import annotations

from dataclasses import dataclass

from .repository import normalize_phone


@dataclass(frozen=True)
class VipProfile:
    phone_suffix: str
    full_name: str
    title: str
    nicknames: tuple[str, ...]


FELIPE_NEWBOLD = VipProfile(
    phone_suffix="21969544700",
    full_name="Felipe Newbold",
    title="Fundador e Líder da New Store RJ",
    nicknames=("Modelo", "Big Boss", "Dorso Livre", "Descamisado"),
)

VIP_PROFILES: tuple[VipProfile, ...] = (FELIPE_NEWBOLD,)


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
    return (
        f"Salve, {nickname}! {profile.full_name}, {profile.title}, na área. "
        f"Atendimento VIP liberado — até o {nickname} merece tratamento de Big Boss."
    )


def build_vip_balance_reply(profile: VipProfile, nickname: str, balance_brl: str, extra: str = "") -> str:
    lines = [
        build_vip_greeting(profile, nickname),
        (
            f"Seu saldo de Cartão Presente, {nickname}, está em {balance_brl}. "
            f"O Descamisado aprova, o Modelo assina e o Dorso Livre segue livre de preocupação."
        ),
    ]
    if extra:
        lines.append(extra)
    lines.append("Precisando de mais alguma coisa, Big Boss?")
    return " ".join(lines)


def build_vip_coupon_reply(profile: VipProfile, nickname: str, code: str, balance_brl: str) -> str:
    return (
        f"{build_vip_greeting(profile, nickname)} "
        f"Código do cartão: *{code}* | saldo {balance_brl}. "
        f"Use na New Store RJ com o charme de quem fundou o império. "
        f"O {nickname} não usa cupom qualquer — usa o de quem manda."
    )


def build_vip_general_reply(profile: VipProfile, nickname: str, base_text: str) -> str:
    return (
        f"{nickname}, ouça bem: {base_text} "
        f"(Sim, falei com o respeito que se deve ao {profile.title}.)"
    )


def build_vip_openai_context(profile: VipProfile, nickname: str) -> str:
    nicknames = ", ".join(f'"{item}"' for item in profile.nicknames)
    return f"""
Cliente VIP identificado:
- Nome: {profile.full_name}
- Cargo: {profile.title}
- Apelidos oficiais: {nicknames}
- Apelido sugerido nesta conversa: {nickname}

Tom obrigatório: cordial, engraçado e respeitoso. Trate como fundador da marca.
Pode usar humor leve com os apelidos, sem exagero ofensivo. Respostas curtas para WhatsApp.
""".strip()
