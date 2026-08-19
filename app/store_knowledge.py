"""Official NewStore institutional policies available to the agent."""

from __future__ import annotations

from typing import Protocol

from .site_knowledge import TRADE_IN_HANDOFF_MESSAGE


class StoreKnowledgeProvider(Protocol):
    """Internal seam for NewStore policy/institutional knowledge."""

    def lookup(self, question: str) -> str | None:
        ...


class NewStoreKnowledgeProvider:
    """Deterministic store policies (trade-in, evaluation, buy-back)."""

    def lookup(self, question: str) -> str | None:
        normalized = (question or "").lower()
        if not normalized:
            return None
        trade_cues = (
            "seminovo",
            "usado",
            "troca",
            "avalia",
            "avaliação",
            "avaliacao",
            "compram",
            "comprando",
            "trade",
        )
        if any(cue in normalized for cue in trade_cues):
            return (
                "A New Store avalia, troca e compra relógios. "
                "Esse fluxo é feito por atendente humano da loja — "
                "não negar a política e não inventar valores de avaliação."
            )
        return None


# Backward-compatible name (previously an empty stub).
EmptyStoreKnowledgeProvider = NewStoreKnowledgeProvider

DEFAULT_STORE_KNOWLEDGE = NewStoreKnowledgeProvider()


def lookup_store_policy(question: str) -> str | None:
    return DEFAULT_STORE_KNOWLEDGE.lookup(question)


def trade_in_policy_text() -> str:
    return TRADE_IN_HANDOFF_MESSAGE
