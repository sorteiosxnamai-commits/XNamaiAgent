"""Read-only boundary to the official XNaMai Club backend.

Membership and checkout require the customer's JWT. The WhatsApp agent has no
such credential and must never collect or persist a password to obtain one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class PublicPlan:
    name: str
    monthly_price_cents: int | None


class ClubProvider:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.transport = transport

    async def get_public_plans(self) -> list[PublicPlan] | None:
        if not self.base_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=8.0, transport=self.transport) as client:
                response = await client.get(f"{self.base_url}/api/plans")
                response.raise_for_status()
                payload: Any = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(payload, list):
            return None
        plans = []
        for row in payload:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                return None
            cents = row.get("monthlyPriceCents")
            if cents is not None and (isinstance(cents, bool) or not isinstance(cents, int) or cents < 0):
                return None
            plans.append(PublicPlan(row["name"], cents))
        return plans
