"""Busca de produto sobre o indice local, e leitura de um produto por id.

Por que a busca e local: o MercosAdaptor so aceita `alterado_apos` na listagem —
nao existe busca textual. Baixar o catalogo a cada mensagem seria inviavel (o
adaptador serializa chamadas e pausa entre paginas). O sync incremental mantem o
indice atualizado; a busca acontece aqui, em memoria do banco local.

Filtros: o schema herdado anuncia `brand` e `ean`, que a integracao Mercos **nao**
mapeia. Em vez de ignorar em silencio — o que faria o agente crer que filtrou —
estes filtros sao REJEITADOS explicitamente, com o nome de cada um.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .freshness import FactFreshness, evaluate_freshness
from .normalizer import CommerceProduct

#: Filtros que a busca local sabe aplicar hoje.
SUPPORTED_FILTERS: frozenset[str] = frozenset(
    {"query", "name", "reference", "limit", "category_id", "available"}
)

#: Anunciados pelo schema herdado, sem mapeamento nesta integracao.
UNSUPPORTED_FILTERS: frozenset[str] = frozenset({"brand", "ean", "tokens", "page"})

DEFAULT_LIMIT = 10
MAX_LIMIT = 20


class ProductIndexReader(Protocol):
    """Leitura do indice local."""

    def search_products(
        self,
        *,
        tenant_id: str,
        text: str | None,
        reference: str | None,
        category_id: str | None,
        available: bool | None,
        limit: int,
    ) -> list[CommerceProduct]: ...

    def get_product(self, *, tenant_id: str, product_id: str) -> CommerceProduct | None: ...


@dataclass(frozen=True)
class SearchRejection:
    """Filtro pedido que esta integracao nao sabe aplicar."""

    filters: tuple[str, ...]

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "unsupported_filter",
            "unsupported_filters": list(self.filters),
            "supported_filters": sorted(SUPPORTED_FILTERS),
        }


def reject_unsupported_filters(arguments: dict[str, Any]) -> SearchRejection | None:
    """Recusa explicita: melhor um erro claro do que um filtro ignorado."""
    asked = {
        key
        for key, value in (arguments or {}).items()
        if key in UNSUPPORTED_FILTERS and value not in (None, "", [], {})
    }
    if not asked:
        return None
    return SearchRejection(tuple(sorted(asked)))


def resolve_limit(raw: Any) -> int:
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def _freshness_for(product: CommerceProduct) -> dict[str, Any]:
    """Validade por tipo de fato — preco e estoque envelhecem em ritmos diferentes."""
    from .catalog import _parse_changed_at

    synced_at = _parse_changed_at(product.changed_at)
    price = evaluate_freshness("price", synced_at)
    stock = evaluate_freshness("stock", synced_at)
    return {
        "price": price.as_metadata(),
        "stock": stock.as_metadata(),
        "price_confirmed": price.can_be_stated_as_current,
        "stock_confirmed": stock.can_be_stated_as_current,
    }


def present_product(product: CommerceProduct) -> dict[str, Any]:
    """Produto + validade. O modelo recebe o valor E se ele pode ser afirmado.

    O valor expirado NAO e apagado nem zerado: continua ali com
    ``*_confirmed=false``, para que o agente possa dizer "nao consegui confirmar"
    em vez de negar a existencia do produto.
    """
    payload = product.for_model()
    payload["freshness"] = _freshness_for(product)
    return payload


def search_products(
    reader: ProductIndexReader, *, tenant_id: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    rejection = reject_unsupported_filters(arguments)
    if rejection is not None:
        return rejection.as_result()

    args = dict(arguments or {})
    text = args.get("query") or args.get("name")
    products = reader.search_products(
        tenant_id=tenant_id,
        text=str(text).strip() if text else None,
        reference=str(args["reference"]).strip() if args.get("reference") else None,
        category_id=str(args["category_id"]).strip() if args.get("category_id") else None,
        available=args.get("available") if isinstance(args.get("available"), bool) else None,
        limit=resolve_limit(args.get("limit")),
    )
    return {
        "ok": True,
        "count": len(products),
        "source": "local_index",
        "products": [present_product(product) for product in products],
    }


def get_product(
    reader: ProductIndexReader, *, tenant_id: str, product_id: str
) -> dict[str, Any]:
    product = reader.get_product(tenant_id=tenant_id, product_id=str(product_id))
    if product is None:
        return {"ok": False, "error": "product_not_found", "product_id": str(product_id)}
    return {"ok": True, "source": "local_index", "product": present_product(product)}


def check_inventory(
    reader: ProductIndexReader, *, tenant_id: str, product_id: str
) -> dict[str, Any]:
    """Estoque a partir de `saldo_estoque`, com validade explicita.

    Tres respostas distintas, e a diferenca entre elas importa:

    * ``stock`` numerico e ``stock_confirmed=true``  -> pode ser afirmado;
    * ``stock`` numerico e ``stock_confirmed=false`` -> existe, mas envelheceu;
    * ``stock=None``                                -> a Mercos nao informou.

    Nenhuma delas vira "sem estoque" por omissao.
    """
    product = reader.get_product(tenant_id=tenant_id, product_id=str(product_id))
    if product is None:
        return {"ok": False, "error": "product_not_found", "product_id": str(product_id)}

    freshness = _freshness_for(product)
    stock_state = freshness["stock"]["freshness"]
    return {
        "ok": True,
        "source": "local_index",
        "product_id": product.external_id,
        "stock": product.stock,
        "active": product.active,
        "excluded": product.excluded,
        "available": product.available,
        "stock_confirmed": stock_state == FactFreshness.FRESH.value,
        "freshness": freshness["stock"],
    }
