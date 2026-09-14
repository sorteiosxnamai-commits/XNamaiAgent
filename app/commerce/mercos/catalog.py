"""Ponte entre o produto normalizado e o indice local (`ai_catalog_index`).

Reuso deliberado: o indice existente ja tem `product_id`, `reference`,
`title_normalized`, `price`, `stock`, `available`, `freshness_at`,
`factual_source`, `payload` e `UNIQUE (tenant_id, catalog_item_key)` — chave que
torna o upsert idempotente. Nenhuma segunda tabela de catalogo foi criada.

Ponto importante do schema: `price`, `stock` e `available` sao NULLABLE. A regra
"ausencia nao e zero" do normalizador sobrevive ate o banco.
"""

from __future__ import annotations

from datetime import datetime, timezone
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


class ProductPageWriter:
    """`PageWriter` do sync: normaliza a pagina e delega o upsert ao indice.

    Produtos sem `id` ja foram descartados pelo sync. Aqui, um produto que nao
    normaliza (payload corrompido) e descartado tambem — gravar registro pela
    metade poluiria a busca com um item sem identidade utilizavel.
    """

    def __init__(self, writer: CatalogIndexWriter, *, tenant_id: str) -> None:
        self._writer = writer
        self._tenant_id = tenant_id

    async def write_page(self, resource: str, records: list[Any]) -> int:
        products: list[CommerceProduct] = []
        for record in records:
            raw = getattr(record, "raw", None)
            if not isinstance(raw, dict):
                continue
            product = normalize_product(raw)
            if product is not None:
                products.append(product)
        if not products:
            return 0
        return self._writer.upsert_products(self._tenant_id, products)
