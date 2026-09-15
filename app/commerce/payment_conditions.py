"""Condicao de pagamento: ler da fonte, cachear, e nunca escolher no chute.

A conta real tem catorze condicoes cadastradas e uma ativa. Pegar "a primeira da
lista" seria simples e errado: a primeira e uma condicao EXCLUIDA, e o pedido
sairia com prazo que a empresa nao pratica mais. Por isso a selecao automatica
so acontece quando ha exatamente uma candidata — nao ha escolha a fazer — e
qualquer empate vira pergunta ao cliente.

O cache existe por causa do custo da fonte: o adaptador serializa requisicoes e
dorme dois segundos depois de cada uma. Consultar condicoes a cada mensagem
deixaria a conversa lenta sem ganho, porque essa lista muda de mes em mes.

E o cache vencido nao serve de rede de seguranca: se o refresh falhar, o
resultado e indisponibilidade, nao o valor velho. Prazo de pagamento vencido
sustentando um pedido novo e exatamente o tipo de erro que so aparece na fatura.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: Meia hora. A lista muda com o cadastro comercial, nao com a conversa.
PAYMENT_CONDITION_CACHE_TTL_SECONDS = 1800

#: Nenhuma condicao ativa: o pedido nao tem como ser fechado.
PAYMENT_CONDITION_REQUIRED = "payment_condition_required"
#: Mais de uma: quem escolhe e o cliente.
PAYMENT_CONDITION_SELECTION_REQUIRED = "payment_condition_selection_required"
#: Nao deu para confirmar agora — diferente de nao existir.
PAYMENT_CONDITION_UNAVAILABLE = "payment_condition_unavailable"
#: Exatamente uma candidata.
PAYMENT_CONDITION_SELECTED = "selected"


@dataclass
class PaymentConditionResolution:
    """O que da para afirmar sobre a condicao de pagamento agora."""

    status: str
    condition_id: str | None = None
    condition_name: str | None = None
    options: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PaymentConditionCache:
    """Cache em memoria, por processo. Sem Redis, sem banco, sem dependencia."""

    rows: list[dict[str, Any]] | None = None
    fetched_at: float = 0.0

    def valid(self, now: float) -> bool:
        return (
            self.rows is not None
            and (now - self.fetched_at) < PAYMENT_CONDITION_CACHE_TTL_SECONDS
        )

    def store(self, rows: list[dict[str, Any]], now: float) -> None:
        self.rows = list(rows)
        self.fetched_at = now


#: Cache do processo. Serverless recicla instancia; perder o cache custa uma
#: leitura, nunca correcao.
_CACHE = PaymentConditionCache()


def active_payment_conditions(rows: Any) -> list[dict[str, Any]]:
    """So as nao excluidas, no formato real da fonte.

    `excluido` e o campo que a fonte usa para aposentar uma condicao sem apagar
    historico — ignora-lo faria o agente oferecer prazo descontinuado.
    """
    if not isinstance(rows, (list, tuple)):
        return []
    ativas: list[dict[str, Any]] = []
    for linha in rows:
        if not isinstance(linha, dict):
            continue
        if linha.get("id") is None:
            continue
        if linha.get("excluido") is True:
            continue
        ativas.append(linha)
    return ativas


async def resolve_payment_condition(
    *,
    cache: PaymentConditionCache | None = None,
    fetch,
    now: float | None = None,
) -> PaymentConditionResolution:
    """Condicao a usar no pedido — ou o motivo de nao haver uma."""
    armazem = cache if cache is not None else _CACHE
    instante = time.monotonic() if now is None else now

    if not armazem.valid(instante):
        try:
            linhas = await fetch()
        except Exception:  # noqa: BLE001 - indisponibilidade e um estado, nao um crash
            # Cache vencido + fonte fora = indisponivel. Reaproveitar o valor
            # velho aqui deixaria um prazo possivelmente extinto fechar pedido.
            return PaymentConditionResolution(status=PAYMENT_CONDITION_UNAVAILABLE)
        armazem.store(linhas or [], instante)

    ativas = active_payment_conditions(armazem.rows or [])
    if not ativas:
        return PaymentConditionResolution(status=PAYMENT_CONDITION_REQUIRED)
    if len(ativas) > 1:
        return PaymentConditionResolution(
            status=PAYMENT_CONDITION_SELECTION_REQUIRED, options=ativas
        )
    unica = ativas[0]
    return PaymentConditionResolution(
        status=PAYMENT_CONDITION_SELECTED,
        condition_id=str(unica["id"]),
        condition_name=unica.get("nome"),
        options=ativas,
    )
