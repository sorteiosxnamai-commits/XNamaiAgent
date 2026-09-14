"""Politica de validade do fato comercial vindo do snapshot sincronizado.

O problema que isto resolve: o indice local guarda o ultimo snapshot conhecido
do catalogo. Preco e estoque envelhecem. Responder "custa X, tem em estoque" com
base num snapshot de duas semanas atras e afirmar como atual algo que nao foi
confirmado — a forma mais cara de alucinacao, porque parece um fato e vira
promessa comercial.

A regra e por TIPO de fato, nao por registro:

* identidade (nome, referencia, EAN) muda pouco -> janela larga;
* preco muda com frequencia -> janela curta;
* estoque muda o tempo todo -> janela muito curta.

Fora da janela o fato nao vira "zero" nem "indisponivel": vira
``unconfirmed``. Ausencia de confirmacao e diferente de ausencia do produto, e o
agente ja sabe dizer que nao conseguiu confirmar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


class FactFreshness(str, Enum):
    """Quao confiavel e um fato do snapshot agora."""

    #: Dentro da janela: pode ser afirmado como atual.
    FRESH = "fresh"
    #: Fora da janela: existe, mas nao pode ser afirmado como atual.
    UNCONFIRMED = "unconfirmed"
    #: Nunca foi sincronizado: nao ha fato algum.
    UNKNOWN = "unknown"


#: Janelas por tipo de fato. Conservadoras de proposito: e melhor dizer "nao
#: confirmei" do que afirmar um preco vencido.
DEFAULT_WINDOWS: dict[str, timedelta] = {
    "identity": timedelta(days=7),
    "price": timedelta(hours=12),
    "stock": timedelta(hours=1),
}


@dataclass(frozen=True)
class FreshnessVerdict:
    """Veredito de validade, pronto para virar metadado factual."""

    fact_kind: str
    freshness: FactFreshness
    synced_at: datetime | None
    age_seconds: float | None

    @property
    def can_be_stated_as_current(self) -> bool:
        return self.freshness is FactFreshness.FRESH

    def as_metadata(self) -> dict[str, object]:
        """Projecao para o contrato factual — sem payload de negocio."""
        return {
            "fact_kind": self.fact_kind,
            "freshness": self.freshness.value,
            "synced_at": self.synced_at.isoformat() if self.synced_at else None,
            "age_seconds": round(self.age_seconds) if self.age_seconds is not None else None,
        }


def evaluate_freshness(
    fact_kind: str,
    synced_at: datetime | None,
    *,
    now: datetime | None = None,
    windows: dict[str, timedelta] | None = None,
) -> FreshnessVerdict:
    """Classifica um fato do snapshot.

    `fact_kind` desconhecido cai na janela mais CURTA disponivel — na duvida,
    exigir confirmacao em vez de presumir validade longa.
    """
    table = windows or DEFAULT_WINDOWS
    if synced_at is None:
        return FreshnessVerdict(fact_kind, FactFreshness.UNKNOWN, None, None)

    reference = now or datetime.now(timezone.utc)
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)

    age = (reference - synced_at).total_seconds()
    if age < 0:
        # Relogio adiantado na origem: tratar como recem-sincronizado, nunca
        # como "do futuro" (o que daria validade infinita).
        age = 0.0

    window = table.get(fact_kind) or min(table.values())
    verdict = (
        FactFreshness.FRESH if age <= window.total_seconds() else FactFreshness.UNCONFIRMED
    )
    return FreshnessVerdict(fact_kind, verdict, synced_at, age)
