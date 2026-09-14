"""Leitura e escrita do produto no indice local (`ai_catalog_index`).

Reusa `CatalogIndexRepository` e `upsert_canonical_items`, que ja existiam. Este
modulo so faz a traducao entre o contrato neutro (`CommerceProduct`) e o item
canonico do indice — nenhuma tabela nova, nenhum SQL novo.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .catalog import product_to_index_fields
from .normalizer import CommerceProduct


def _to_product(row: dict[str, Any]) -> CommerceProduct | None:
    """Linha do indice -> contrato neutro.

    `price`, `stock` e `available` sao NULLABLE no schema; `None` atravessa
    intacto. A regra "ausencia nao e zero" vale tambem na volta do banco.
    """
    if not isinstance(row, dict):
        return None
    product_id = row.get("product_id")
    if product_id is None or str(product_id).strip() == "":
        return None

    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    freshness = row.get("freshness_at")
    changed_at = freshness.isoformat() if isinstance(freshness, datetime) else (
        str(freshness) if freshness else None
    )
    stock = row.get("stock")
    return CommerceProduct(
        external_id=str(product_id),
        reference=row.get("reference"),
        name=payload.get("nome") or (row.get("title_normalized") or None),
        price=float(row["price"]) if row.get("price") is not None else None,
        stock=int(stock) if stock is not None else None,
        active=payload.get("ativo") if isinstance(payload.get("ativo"), bool) else None,
        excluded=payload.get("excluido") if isinstance(payload.get("excluido"), bool) else None,
        category_id=row.get("category"),
        unit=payload.get("unidade"),
        changed_at=changed_at,
        available=row.get("available") if isinstance(row.get("available"), bool) else None,
        raw=payload,
    )


class CatalogIndexProductReader:
    """Busca e leitura de produto sobre `ai_catalog_index`."""

    def __init__(self, repository: Any | None = None) -> None:
        self._repository = repository

    def _repo(self):
        if self._repository is None:
            from ...catalog_index_repository import CatalogIndexRepository

            self._repository = CatalogIndexRepository()
        return self._repository

    def search_products(
        self,
        *,
        tenant_id: str,
        text: str | None,
        reference: str | None,
        category_id: str | None,
        available: bool | None,
        limit: int,
    ) -> list[CommerceProduct]:
        repo = self._repo()
        rows: list[dict[str, Any]] = []

        # Referencia e identificador: quando o cliente a informa, ela manda.
        if reference:
            rows = repo.search_exact(tenant_id=tenant_id, reference=reference) or []
        if not rows and text:
            rows = repo.search_lexical(tenant_id=tenant_id, query=text) or []

        products = []
        for row in rows:
            product = _to_product(row)
            if product is None:
                continue
            if category_id and str(product.category_id or "") != str(category_id):
                continue
            if available is not None and product.available is not available:
                continue
            products.append(product)
            if len(products) >= limit:
                break
        return products

    def get_product(self, *, tenant_id: str, product_id: str) -> CommerceProduct | None:
        row = self._repo().get_by_product_and_variant(
            tenant_id=tenant_id, product_id=str(product_id), variant_id=None
        )
        return _to_product(row) if row else None

    def delete_products(self, tenant_id: str, product_ids: list[str]) -> int:
        """Remove snapshots pontualmente.

        A traducao `product_id -> catalog_item_key` mora aqui: o repositorio
        compartilhado nao precisa saber nada de Mercos.
        """
        from ...catalog_index_repository import make_catalog_item_key

        keys = [make_catalog_item_key(str(pid), None) for pid in product_ids if str(pid).strip()]
        if not keys:
            return 0
        return self._repo().delete_items(tenant_id=tenant_id, catalog_item_keys=keys)

    def upsert_products(self, tenant_id: str, products: list[CommerceProduct]) -> int:
        """Upsert idempotente. A chave unica do indice evita duplicacao."""
        from ...catalog_index import CanonicalCatalogItem, upsert_canonical_items
        from ...catalog_index_repository import make_catalog_item_key

        items = []
        now = datetime.now(timezone.utc)
        for product in products:
            fields = product_to_index_fields(product, synced_at=now)
            items.append(
                CanonicalCatalogItem(
                    tenant_id=tenant_id,
                    catalog_item_key=make_catalog_item_key(product.external_id, None),
                    product_id=fields["product_id"],
                    reference=fields["reference"],
                    title_normalized=fields["title_normalized"],
                    price=fields["price"],
                    stock=fields["stock"],
                    available=fields["available"],
                    category=fields["category"],
                    freshness_at=fields["freshness_at"],
                    factual_source=fields["factual_source"],
                    raw=fields["payload"],
                )
            )
        return upsert_canonical_items(items)
