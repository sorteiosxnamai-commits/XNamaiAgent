"""Channel adapter contracts (FASE 3)."""

from __future__ import annotations

from typing import Any, Protocol

from app.models import AgentResult, IncomingMessage


class ChannelAdapter(Protocol):
    provider: str

    def verify_request(self, *, headers: dict[str, str], body: bytes) -> bool: ...

    def parse_inbound(self, payload: dict[str, Any]) -> list[IncomingMessage]: ...

    async def send_reply(
        self,
        incoming: IncomingMessage,
        result: AgentResult,
    ) -> dict[str, Any]: ...


def channel_supports_story_media(provider: str, channel: str) -> bool:
    """Brevo Instagram cannot deliver Story CDN URLs; Meta can."""
    if (channel or "").lower() != "instagram":
        return False
    return (provider or "").lower() in {"meta", "meta_instagram"}
