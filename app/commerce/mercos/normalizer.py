"""Traducao entre o payload do adaptador e os contratos neutros do AI core.

Fonte dos campos: documentacao OFICIAL da Mercos (produtos e clientes na API v1,
pedidos na v2), confirmada pelo dono do produto. O MercosAdaptor e pass-through,
entao o corpo que chega aqui e o corpo da Mercos.

Regra que governa este modulo inteiro
-------------------------------------
**Ausencia nao e zero.** `preco_tabela` ausente vira `price=None`, nunca `0`;
`saldo_estoque` ausente vira `stock=None`, nunca `0`; `ativo` ausente vira
`active=None`, nunca `False`. Um zero inventado vira "produto de graca" ou
"sem estoque" na boca do agente — os dois sao caros e parecem fato.

`raw` fica disponivel para o indice local. NUNCA vai para o modelo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROVIDER_NAME = "mercos"

#: `status` de pedido (Mercos v2). Valores oficiais — nada alem destes.
ORDER_STATUS: dict[int, str] = {0: "cancelado", 1: "orcamento", 2: "pedido"}

#: `status_faturamento` (Mercos v2). Valores oficiais.
ORDER_BILLING_STATUS: dict[int, str] = {
    0: "nao_faturado",
    1: "parcialmente_faturado",
    2: "faturado",
}


def _present(value: Any) -> bool:
    """O campo veio com conteudo? String vazia conta como ausente."""
    return value is not None and not (isinstance(value, str) and not value.strip())


def _text(value: Any) -> str | None:
    if not _present(value):
        return None
    return str(value).strip()


def _number(value: Any) -> float | None:
    """Numero, ou ``None``. ZERO e valor legitimo e sobrevive.

    O caminho normal e JSON numerico. String e defensivo, e ai a separacao
    decimal importa: ``"1.234,50"`` e mil duzentos e trinta e quatro reais e
    cinquenta, nao um erro de parse. Com ponto E virgula, o ponto e milhar.
    Com so um deles, vale a leitura convencional de JSON.
    """
    if not _present(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _boolean(value: Any) -> bool | None:
    """Booleano, ou ``None`` quando ausente/indecifravel — jamais ``False`` por omissao."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "t", "1", "sim", "s"}:
        return True
    if text in {"false", "f", "0", "nao", "não", "n"}:
        return False
    return None


# ---------------------------------------------------------------------------
# Identidade generica (usada pelo sync para qualquer recurso)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommerceRecord:
    """Identidade neutra de um registro comercial."""

    external_id: str
    provider: str
    resource: str
    changed_at: str | None = None
    raw: dict[str, Any] | None = None

    def identity(self) -> dict[str, Any]:
        """Projecao segura: so identidade e procedencia, sem payload cru."""
        return {
            "external_id": self.external_id,
            "provider": self.provider,
            "resource": self.resource,
            "changed_at": self.changed_at,
        }


def normalize_record(resource: str, row: dict[str, Any]) -> CommerceRecord | None:
    """Extrai identidade de uma linha. ``None`` quando nao ha `id`."""
    if not isinstance(row, dict):
        return None
    raw_id = row.get("id")
    if not _present(raw_id):
        return None
    return CommerceRecord(
        external_id=str(raw_id),
        provider=PROVIDER_NAME,
        resource=resource,
        changed_at=_text(row.get("ultima_alteracao")),
        raw=row,
    )


def normalize_page(resource: str, rows: list[dict[str, Any]]) -> list[CommerceRecord]:
    records = []
    for row in rows:
        record = normalize_record(resource, row)
        if record is not None:
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Produto
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommerceProduct:
    """Produto neutro. Campos ausentes ficam ``None`` — nunca zero."""

    external_id: str
    provider: str = PROVIDER_NAME
    reference: str | None = None
    name: str | None = None
    price: float | None = None
    stock: int | None = None
    active: bool | None = None
    excluded: bool | None = None
    category_id: str | None = None
    unit: str | None = None
    changed_at: str | None = None
    available: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def for_model(self) -> dict[str, Any]:
        """O que pode chegar ao modelo. Sem `raw`, sem campo nao mapeado."""
        return {
            "external_id": self.external_id,
            "provider": self.provider,
            "reference": self.reference,
            "name": self.name,
            "price": self.price,
            "stock": self.stock,
            "active": self.active,
            "available": self.available,
            "unit": self.unit,
            "category_id": self.category_id,
        }


def compute_available(
    *, active: bool | None, excluded: bool | None, stock: int | None
) -> bool | None:
    """Disponibilidade — so quando ha evidencia suficiente.

    Regra documentada e deliberadamente estreita::

        ativo is True  AND  excluido is False  AND  saldo_estoque is not None
            -> available = saldo_estoque > 0
        qualquer outro caso
            -> available = None   (nao confirmado)

    `None` fora dessas condicoes e proposital: nao afirma disponibilidade nem
    indisponibilidade. Produto inativo NAO vira ``available=False`` aqui porque
    a semantica de `ativo` na venda nao foi confirmada — e afirmar "nao temos"
    erroneamente custa uma venda tanto quanto prometer o que nao existe.
    """
    if active is True and excluded is False and stock is not None:
        return stock > 0
    return None


def normalize_product(row: dict[str, Any]) -> CommerceProduct | None:
    """Produto Mercos (API v1) -> contrato neutro. ``None`` sem `id`."""
    if not isinstance(row, dict) or not _present(row.get("id")):
        return None

    stock = _integer(row.get("saldo_estoque"))
    active = _boolean(row.get("ativo"))
    excluded = _boolean(row.get("excluido"))
    return CommerceProduct(
        external_id=str(row["id"]),
        reference=_text(row.get("codigo")),
        name=_text(row.get("nome")),
        price=_number(row.get("preco_tabela")),
        stock=stock,
        active=active,
        excluded=excluded,
        category_id=_text(row.get("categoria_id")),
        unit=_text(row.get("unidade")),
        changed_at=_text(row.get("ultima_alteracao")),
        available=compute_available(active=active, excluded=excluded, stock=stock),
        raw=row,
    )


# ---------------------------------------------------------------------------
# Cliente — PII
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommerceCustomer:
    """Cliente neutro.

    `document`, `emails` e `phones` sao PII: existem no contrato para uso
    interno (identificacao), nunca para log. `for_model()` os omite.
    """

    external_id: str
    provider: str = PROVIDER_NAME
    legal_name: str | None = None
    display_name: str | None = None
    person_type: str | None = None
    document: str | None = field(default=None, repr=False)
    emails: list[str] = field(default_factory=list, repr=False)
    phones: list[str] = field(default_factory=list, repr=False)
    blocked: bool | None = None
    excluded: bool | None = None
    changed_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def for_model(self) -> dict[str, Any]:
        """Projecao sem PII: identidade e estado, nunca documento ou contato."""
        return {
            "external_id": self.external_id,
            "provider": self.provider,
            "display_name": self.display_name or self.legal_name,
            "person_type": self.person_type,
            "blocked": self.blocked,
            "has_document": bool(self.document),
            "has_email": bool(self.emails),
            "has_phone": bool(self.phones),
        }


def _string_list(value: Any) -> list[str]:
    """Lista de contatos. Aceita lista de string ou de dict, sem inventar chave."""
    if not _present(value):
        return []
    if isinstance(value, str):
        return [value.strip()]
    if not isinstance(value, list):
        return []
    found: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            found.append(item.strip())
        elif isinstance(item, dict):
            for key in ("email", "telefone", "numero", "valor"):
                text = _text(item.get(key))
                if text:
                    found.append(text)
                    break
    return found


def normalize_customer(row: dict[str, Any]) -> CommerceCustomer | None:
    """Cliente Mercos (API v1) -> contrato neutro. ``None`` sem `id`."""
    if not isinstance(row, dict) or not _present(row.get("id")):
        return None
    return CommerceCustomer(
        external_id=str(row["id"]),
        legal_name=_text(row.get("razao_social")),
        display_name=_text(row.get("nome_fantasia")),
        person_type=_text(row.get("tipo")),
        document=_text(row.get("cnpj")),
        emails=_string_list(row.get("emails")),
        phones=_string_list(row.get("telefones")),
        blocked=_boolean(row.get("bloqueado")),
        excluded=_boolean(row.get("excluido")),
        changed_at=_text(row.get("ultima_alteracao")),
        raw=row,
    )


# ---------------------------------------------------------------------------
# Pedido
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommerceOrderItem:
    """Item de pedido neutro."""

    external_id: str | None = None
    product_id: str | None = None
    product_reference: str | None = None
    product_name: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    net_price: float | None = None
    subtotal: float | None = None
    excluded: bool | None = None


@dataclass(frozen=True)
class CommerceOrder:
    """Pedido neutro (Mercos v2).

    `status` e `billing_status` vem traduzidos E com o valor bruto ao lado: o
    rotulo ajuda a leitura, o bruto preserva a verdade caso a Mercos passe a
    devolver um codigo que ainda nao conhecemos.
    """

    external_id: str
    provider: str = PROVIDER_NAME
    number: str | None = None
    status: str | None = None
    status_raw: int | None = None
    billing_status: str | None = None
    billing_status_raw: int | None = None
    total: float | None = None
    shipping_value: float | None = None
    customer_id: str | None = None
    payment_condition: str | None = None
    payment_condition_id: str | None = None
    created_at: str | None = None
    issued_at: str | None = None
    changed_at: str | None = None
    items: list[CommerceOrderItem] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def for_model(self) -> dict[str, Any]:
        """Projecao sem dado cadastral do cliente do pedido."""
        return {
            "external_id": self.external_id,
            "provider": self.provider,
            "number": self.number,
            "status": self.status,
            "status_raw": self.status_raw,
            "billing_status": self.billing_status,
            "total": self.total,
            "shipping_value": self.shipping_value,
            "payment_condition": self.payment_condition,
            "issued_at": self.issued_at,
            "items": [
                {
                    "product_id": item.product_id,
                    "product_reference": item.product_reference,
                    "product_name": item.product_name,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "subtotal": item.subtotal,
                }
                for item in self.items
                if item.excluded is not True
            ],
        }


def _normalize_order_item(row: dict[str, Any]) -> CommerceOrderItem | None:
    if not isinstance(row, dict):
        return None
    return CommerceOrderItem(
        external_id=_text(row.get("id")),
        product_id=_text(row.get("produto_id")),
        product_reference=_text(row.get("produto_codigo")),
        product_name=_text(row.get("produto_nome")),
        quantity=_number(row.get("quantidade")),
        unit_price=_number(row.get("preco_tabela")),
        net_price=_number(row.get("preco_liquido")),
        subtotal=_number(row.get("subtotal")),
        excluded=_boolean(row.get("excluido")),
    )


def normalize_order(row: dict[str, Any]) -> CommerceOrder | None:
    """Pedido Mercos (API v2) -> contrato neutro. ``None`` sem `id`."""
    if not isinstance(row, dict) or not _present(row.get("id")):
        return None

    status_raw = _integer(row.get("status"))
    billing_raw = _integer(row.get("status_faturamento"))
    items = []
    for item_row in row.get("itens") or []:
        item = _normalize_order_item(item_row)
        if item is not None:
            items.append(item)

    return CommerceOrder(
        external_id=str(row["id"]),
        number=_text(row.get("numero")),
        status=ORDER_STATUS.get(status_raw) if status_raw is not None else None,
        status_raw=status_raw,
        billing_status=(
            ORDER_BILLING_STATUS.get(billing_raw) if billing_raw is not None else None
        ),
        billing_status_raw=billing_raw,
        total=_number(row.get("total")),
        shipping_value=_number(row.get("valor_frete")),
        customer_id=_text(row.get("cliente_id")),
        payment_condition=_text(row.get("condicao_pagamento")),
        payment_condition_id=_text(row.get("condicao_pagamento_id")),
        created_at=_text(row.get("data_criacao")),
        issued_at=_text(row.get("data_emissao")),
        changed_at=_text(row.get("ultima_alteracao")),
        items=items,
        raw=row,
    )
