from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelProfile:
    channel: str
    max_reply_chars: int
    allow_audio_reply: bool
    allow_product_images: bool
    tone: str
    assisted_chat: bool


_PROFILES: dict[str, ChannelProfile] = {
    "whatsapp": ChannelProfile(
        channel="whatsapp",
        max_reply_chars=900,
        allow_audio_reply=True,
        allow_product_images=True,
        tone="conversational",
        assisted_chat=True,
    ),
    "instagram": ChannelProfile(
        channel="instagram",
        max_reply_chars=700,
        allow_audio_reply=False,
        allow_product_images=True,
        tone="short_social",
        assisted_chat=True,
    ),
    "facebook": ChannelProfile(
        channel="facebook",
        max_reply_chars=700,
        allow_audio_reply=False,
        allow_product_images=True,
        tone="short_social",
        assisted_chat=True,
    ),
    "widget": ChannelProfile(
        channel="widget",
        max_reply_chars=900,
        allow_audio_reply=False,
        allow_product_images=True,
        tone="assisted",
        assisted_chat=True,
    ),
}


def get_channel_profile(channel: str | None) -> ChannelProfile:
    key = (channel or "unknown").strip().lower()
    return _PROFILES.get(
        key,
        ChannelProfile(
            channel=key or "unknown",
            max_reply_chars=900,
            allow_audio_reply=False,
            allow_product_images=False,
            tone="neutral",
            assisted_chat=False,
        ),
    )


def channel_system_hint(channel: str | None) -> str:
    from .prompt_layers import STYLE_VOICE_RULES

    profile = get_channel_profile(channel)
    shared = STYLE_VOICE_RULES
    if profile.channel == "whatsapp":
        return (
            f"Canal: whatsapp. {shared} Use 1–3 blocos curtos, "
            "uma ideia por bloco, no máximo um CTA."
        )
    if profile.channel == "instagram":
        return (
            f"Canal: instagram. {shared} Seja direto; continue por aqui "
            "sem mandar o cliente para WhatsApp sem necessidade."
        )
    if profile.channel == "facebook":
        return (
            f"Canal: facebook. {shared} Respostas curtas a médias, "
            "preservando continuidade."
        )
    if profile.channel == "widget":
        return (
            f"Canal: widget. {shared} Pode usar um pouco mais de estrutura, "
            "mas continue objetivo."
        )
    if profile.tone == "short_social":
        return (
            f"Canal: {profile.channel}. {shared} Responda de forma curta, "
            "sem áudio e com no máximo uma pergunta."
        )
    if profile.assisted_chat:
        return (
            f"Canal: {profile.channel}. {shared} Conduza o atendimento de forma "
            "assistida, objetiva e natural."
        )
    return f"Canal: {profile.channel}. {shared}"
