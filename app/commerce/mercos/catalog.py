"""Ponte entre o produto normalizado e o indice local (`ai_catalog_index`).

Reuso deliberado: o indice existente ja tem `product_id`, `reference`,
`title_normalized`, `price`, `stock`, `available`, `freshness_at`,
`factual_source`, `payload` e `UNIQUE (tenant_id, catalog_item_key)` — chave que
torna o upsert idempotente. Nenhuma segunda tabela de catalogo foi criada.

Ponto importante do schema: `price`, `stock` e `available` sao NULLABLE. A regra
"ausencia nao e zero" do normalizador sobrevive ate o banco.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from .normalizer import CommerceProduct, normalize_product

#: Procedencia gravada no indice. Valor neutro ja usado pelo restante do sistema.
CATALOG_FACTUAL_SOURCE = "commerce_search"

#: Campos do payload Mercos que nunca precisam ficar no indice. `observacoes` e
#: campo livre: pode conter anotacao interna sobre cliente ou negociacao.
_PAYLOAD_DROP = frozenset({"observacoes", "produtos_grade"})


class CatalogIndexWriter(Protocol):
    """Destino do produto normalizado. Deve ser idempotente (upsert por chave)."""

    def upsert_products(self, tenant_id: str, products: list[CommerceProduct]) -> int: ...

    def delete_products(self, tenant_id: str, product_ids: list[str]) -> int: ...


def sanitize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Payload para o indice, sem campo livre que possa carregar anotacao."""
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if key not in _PAYLOAD_DROP}


def _parse_changed_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def product_to_index_fields(
    product: CommerceProduct, *, synced_at: datetime | None = None
) -> dict[str, Any]:
    """Produto normalizado -> colunas do indice.

    `freshness_at` usa `ultima_alteracao` quando ela existe — e o instante em que
    a Mercos afirma o dado — e cai para o momento do sync quando nao existe.
    Nunca inventa um instante mais recente do que o provider garante.
    """
    now = synced_at or datetime.now(timezone.utc)
    changed_at = _parse_changed_at(product.changed_at)
    return {
        "product_id": product.external_id,
        "reference": product.reference,
        "title_normalized": (product.name or "").strip().casefold(),
        "price": product.price,
        "stock": product.stock,
        "available": product.available,
        "category": product.category_id,
        "freshness_at": changed_at or now,
        "factual_source": CATALOG_FACTUAL_SOURCE,
        "payload": sanitize_payload(product.raw),
    }


class CatalogDecision(str, Enum):
    """O que fazer com um produto nesta pagina do sync."""

    #: Confirmadamente vendavel: entra ou continua no indice.
    INDEX = "index"
    #: Confirmadamente indisponivel: sai do indice, se estiver la.
    REMOVE = "remove"
    #: Estado incerto: nao cria snapshot novo e NAO destroi o anterior.
    IGNORE_UNKNOWN = "ignore_unknown_state"


def decide(product: CommerceProduct) -> CatalogDecision:
    """Classifica um produto pelo estado comercial declarado pela Mercos.

    Regra::

        ativo is True  AND excluido is False  -> INDEX
        ativo is False OR  excluido is True   -> REMOVE
        qualquer campo ausente                -> IGNORE_UNKNOWN

    `IGNORE_UNKNOWN` existe porque **ausencia nao e falso**. Um payload que
    chegue sem `ativo` nao autoriza a concluir que o produto morreu: criar
    snapshot seria afirmar disponibilidade sem evidencia, e apagar o snapshot
    anterior seria destruir dado bom por causa de um campo faltando. Nesse caso
    o snapshot existente permanece e a politica de freshness continua decidindo
    o que ainda pode ser afirmado.

    A ordem importa: um estado explicitamente negativo (`ativo=False` ou
    `excluido=True`) vence a incerteza do outro campo, porque ai ha evidencia
    de que o produto nao deve ser oferecido.
    """
    if product.active is False or product.excluded is True:
        return CatalogDecision.REMOVE
    if product.active is True and product.excluded is False:
        return CatalogDecision.INDEX
    return CatalogDecision.IGNORE_UNKNOWN


@dataclass
class PageOutcome:
    """Contagem por decisao. Sem payload, sem PII."""

    received: int = 0
    upserted: int = 0
    removed: int = 0
    ignored_unknown_state: int = 0

    def as_log(self) -> dict[str, int]:
        return {
            "received": self.received,
            "upserted": self.upserted,
            "removed": self.removed,
            "ignored_unknown_state": self.ignored_unknown_state,
        }


class ProductPageWriter:
    """`PageWriter` do sync: decide, remove e grava — nessa ordem.

    Por que nao basta "pular o excluido": um produto indexado no sync N que
    chega excluido no sync N+1 permaneceria no indice para sempre se apenas
    fosse ignorado, e o agente seguiria oferecendo item que nao existe mais. A
    transicao de estado precisa ser tratada, nao omitida.

    Remocao antes da gravacao: um produto nunca esta nas duas listas (a decisao
    e exclusiva), mas remover primeiro garante que, se a gravacao falhar no
    meio, o indice nunca fique com item que ja deveria ter saido.

    Qualquer falha propaga para o motor de sync, que entao NAO avanca o cursor —
    a pagina inteira sera refeita.
    """

    def __init__(self, writer: CatalogIndexWriter, *, tenant_id: str) -> None:
        if not str(tenant_id or "").strip():
            raise ValueError("tenant_id do dominio comercial e obrigatorio")
        self._writer = writer
        self._tenant_id = str(tenant_id).strip()
        self.last_outcome = PageOutcome()

    async def write_page(self, resource: str, records: list[Any]) -> int:
        outcome = PageOutcome()
        to_index: list[CommerceProduct] = []
        to_remove: list[str] = []

        for record in records:
            raw = getattr(record, "raw", None)
            if not isinstance(raw, dict):
                continue
            product = normalize_product(raw)
            if product is None:
                continue
            outcome.received += 1
            decision = decide(product)
            if decision is CatalogDecision.INDEX:
                to_index.append(product)
            elif decision is CatalogDecision.REMOVE:
                to_remove.append(product.external_id)
            else:
                outcome.ignored_unknown_state += 1

        # Remocao pontual: SOMENTE os ids desta pagina. Nunca GC de catalogo.
        if to_remove:
            outcome.removed = self._writer.delete_products(self._tenant_id, to_remove)
        if to_index:
            outcome.upserted = self._writer.upsert_products(self._tenant_id, to_index)

        self.last_outcome = outcome
        print("[commerce.sync.page]", outcome.as_log())
        return outcome.upserted
