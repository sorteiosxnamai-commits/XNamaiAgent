"""Conversor `CommerceOrderDraft` -> payload de pedido Mercos (API v2).

Funcao pura e testavel: nao faz I/O, nao chama o adaptador, nao cria pedido.
Existe para que o mapeamento seja revisado ANTES de qualquer POST real.

Contrato oficial da criacao (v2):

* obrigatorios: ``cliente_id`` e ``data_emissao``;
* condicao de pagamento: ``condicao_pagamento_id`` **ou** ``condicao_pagamento``,
  nunca os dois — enviar ambos e ambiguidade, e a Mercos e quem decide o preco;
* item: ``produto_id``, ``preco_tabela`` e ``quantidade`` sao obrigatorios.

Nenhum campo fora dessa lista e enviado por conta propria. Campo inventado num
POST nao e bug de leitura: vira pedido errado no ERP do cliente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class OrderDraftError(ValueError):
    """Rascunho invalido. Erro de programa, nao de rede — falha antes do POST."""

    def __init__(self, message: str, *, field_name: str | None = None) -> None:
        super().__init__(message)
        self.field_name = field_name


@dataclass(frozen=True)
class CommerceOrderDraftItem:
    """Item do rascunho. Os tres campos sao exigidos pela Mercos."""

    product_id: str
    unit_price: float
    quantity: float


@dataclass(frozen=True)
class CommerceOrderDraft:
    """Rascunho neutro de pedido, montado pelo core e ja autorizado por ele.

    O provider NAO decide vender: quando este rascunho chega, a confirmacao do
    cliente ja aconteceu no fluxo existente do agente.
    """

    customer_id: str
    issued_at: str
    items: list[CommerceOrderDraftItem] = field(default_factory=list)
    payment_condition_id: str | None = None
    payment_condition: str | None = None


def _require(value: Any, name: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise OrderDraftError(f"campo obrigatorio ausente: {name}", field_name=name)
    return text


def _positive(value: Any, name: str) -> float:
    if value is None:
        raise OrderDraftError(f"campo obrigatorio ausente: {name}", field_name=name)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OrderDraftError(f"valor invalido em {name}", field_name=name) from exc
    if number <= 0:
        raise OrderDraftError(f"{name} deve ser maior que zero", field_name=name)
    return number


def build_order_payload(draft: CommerceOrderDraft) -> dict[str, Any]:
    """Rascunho -> payload v2. Levanta `OrderDraftError` antes de qualquer envio."""
    payload: dict[str, Any] = {
        "cliente_id": _require(draft.customer_id, "cliente_id"),
        "data_emissao": _require(draft.issued_at, "data_emissao"),
    }

    has_id = bool((draft.payment_condition_id or "").strip())
    has_text = bool((draft.payment_condition or "").strip())
    if has_id and has_text:
        raise OrderDraftError(
            "informe condicao_pagamento_id OU condicao_pagamento, nunca os dois",
            field_name="condicao_pagamento",
        )
    if not has_id and not has_text:
        raise OrderDraftError(
            "condicao de pagamento obrigatoria: informe condicao_pagamento_id ou "
            "condicao_pagamento",
            field_name="condicao_pagamento",
        )
    if has_id:
        payload["condicao_pagamento_id"] = str(draft.payment_condition_id).strip()
    else:
        payload["condicao_pagamento"] = str(draft.payment_condition).strip()

    if not draft.items:
        raise OrderDraftError("pedido sem itens", field_name="itens")

    payload["itens"] = [
        {
            "produto_id": _require(item.product_id, "produto_id"),
            "preco_tabela": _positive(item.unit_price, "preco_tabela"),
            "quantidade": _positive(item.quantity, "quantidade"),
        }
        for item in draft.items
    ]
    return payload
