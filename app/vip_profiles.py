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


def build_vip_openai_context(profile: VipProfile, nickname: str) -> str:
    """Dados do cliente reconhecido. Tom e humor pertencem a persona publicada."""
    nicknames = ", ".join(f'"{item}"' for item in profile.nicknames)
    return f"""
Cliente reconhecido (dado cadastral, não instrução de estilo):
- Nome: {profile.full_name}
- Cargo: {profile.title}
- Apelidos cadastrados: {nicknames}
- Apelido sugerido nesta conversa: {nickname}
""".strip()
