"""Busca de produto sobre o indice local, e leitura de um produto por id.

Por que a busca e local: o MercosAdaptor so aceita `alterado_apos` na listagem —
nao existe busca textual. Baixar o catalogo a cada mensagem seria inviavel (o
adaptador serializa chamadas e pausa entre paginas). O sync incremental mantem o
indice atualizado; a busca acontece aqui, em memoria do banco local.

Filtros, em tres categorias:

* SUPORTADOS — aplicados de verdade (`query`, `name`, `reference`, `limit`,
  `available`, `page`);
* PISTAS — aceitos, mas reordenam em vez de descartar (`brand`), porque a Mercos
  nao tem campo de marca e filtrar por substring esconderia produto que existe;
* RECUSADOS — sem mapeamento nenhum (`ean`, `tokens`, `category_id`). A recusa e
  explicita, com o nome de cada um: ignorar em silencio faria o agente crer que
  filtrou.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .freshness import FactFreshness, evaluate_freshness
from .normalizer import CommerceProduct

#: Filtros que a busca local sabe aplicar hoje.
#: `page` entrou aqui porque o caminho legado o envia em TODA requisicao: como
#: ele estava entre os recusados, 100% das buscas do agente voltavam
#: `unsupported_filter` e o catalogo inteiro ficava inalcancavel. Paginar sobre
#: o indice local e trivial, entao a resposta certa e implementar, nao recusar.
SUPPORTED_FILTERS: frozenset[str] = frozenset(
    {"query", "name", "reference", "limit", "available", "page"}
)

#: Aceitos como PISTA, nunca como filtro estruturado. A diferenca importa: uma
#: pista reordena, um filtro descarta. Como a Mercos nao tem campo de marca, um
#: filtro de marca inventado esconderia produto que existe — e busca vazia o
#: agente le como "nao temos esse produto".
HINT_FILTERS: frozenset[str] = frozenset({"brand"})

#: Anunciados pelo schema herdado, sem mapeamento nesta integracao.
#: `category_id` entrou aqui apos a validacao contra a Mercos real: o campo
#: `categoria_id` nao existe em nenhuma das 500 linhas inspecionadas. Anunciar
#: um filtro que nunca casa produziria busca vazia sem erro.
UNSUPPORTED_FILTERS: frozenset[str] = frozenset({"ean", "tokens", "category_id"})

DEFAULT_LIMIT = 10
MAX_LIMIT = 20

#: Teto da janela que a busca local percorre para paginar. Alem disto a leitura
#: do indice nao alcanca, e fingir que alcanca produziria pagina vazia
#: indistinguivel de "acabou".
MAX_SCAN = 100


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
            "hint_filters": sorted(HINT_FILTERS),
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


def resolve_page(raw: Any) -> int:
    """Pagina 1-based. Valor invalido vira a primeira pagina, nunca um erro."""
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, page)


def brand_hint(arguments: dict[str, Any]) -> str | None:
    """A marca pedida, normalizada — ou ``None`` quando nao veio."""
    raw = (arguments or {}).get("brand")
    texto = str(raw).strip().casefold() if raw else ""
    return texto or None


def prefer_brand(
    products: list[CommerceProduct], brand: str | None
) -> list[CommerceProduct]:
    """Reordena trazendo a marca pedida para a frente. NAO descarta nada.

    A Mercos nao expoe campo de marca: ela aparece, quando aparece, dentro do
    nome. Filtrar por substring descartaria produto cujo nome escreve a marca de
    outro jeito, e o agente leria a lista vazia como ausencia de catalogo. Como
    pista, o pior caso e uma ordem menos util — nunca um produto escondido.
    """
    if not brand:
        return products
    combina = [p for p in products if brand in (p.name or "").casefold()]
    resto = [p for p in products if brand not in (p.name or "").casefold()]
    return combina + resto


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


def is_commercially_unavailable(product: CommerceProduct) -> bool:
    """Produto EXPLICITAMENTE inativo ou excluido.

    Defesa em profundidade: mesmo com o indice limpo pelo sync, ainda pode
    existir snapshot legado, corrida entre syncs ou linha gravada antes desta
    correcao. Incerteza (`None`) NAO conta aqui — so estado negativo declarado.
    """
    return product.active is False or product.excluded is True


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
    limit = resolve_limit(args.get("limit"))
    page = resolve_page(args.get("page"))
    brand = brand_hint(args)

    text = args.get("query") or args.get("name")
    text = str(text).strip() if text else None
    if text is None and brand:
        # Marca sozinha e o unico texto disponivel: vira a busca lexical.
        text = brand

    offset = (page - 1) * limit
    if offset >= MAX_SCAN:
        # Pagina alem do alcance da leitura. Vazia de verdade, sem fingir erro
        # nem fingir que existe mais catalogo adiante.
        return _search_result([], page=page, has_more=False)

    janela = reader.search_products(
        tenant_id=tenant_id,
        text=text,
        reference=str(args["reference"]).strip() if args.get("reference") else None,
        category_id=None,
        available=args.get("available") if isinstance(args.get("available"), bool) else None,
        # Um item alem da janela, so para saber se existe proxima pagina sem
        # precisar de uma segunda consulta.
        limit=min(offset + limit + 1, MAX_SCAN),
    )
    # Segunda barreira: o indice nao deveria conter inativo/excluido, mas um
    # snapshot legado nao pode virar oferta.
    vendaveis = [p for p in janela if not is_commercially_unavailable(p)]
    ordenados = prefer_brand(vendaveis, brand)
    recorte = ordenados[offset : offset + limit]
    return _search_result(
        recorte, page=page, has_more=len(ordenados) > offset + limit
    )


def _search_result(
    products: list[CommerceProduct], *, page: int, has_more: bool
) -> dict[str, Any]:
    return {
        "ok": True,
        "count": len(products),
        "source": "local_index",
        "products": [present_product(product) for product in products],
        "page": page,
        "has_more": has_more,
    }


def get_product(
    reader: ProductIndexReader, *, tenant_id: str, product_id: str
) -> dict[str, Any]:
    product = reader.get_product(tenant_id=tenant_id, product_id=str(product_id))
    if product is None:
        return {"ok": False, "error": "product_not_found", "product_id": str(product_id)}
    if is_commercially_unavailable(product):
        return {
            "ok": False,
            "error": "commerce_product_unavailable",
            "product_id": str(product_id),
        }
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
    if is_commercially_unavailable(product):
        # O motivo e INDISPONIBILIDADE DO PRODUTO, nunca "estoque zero": dizer
        # "sem estoque" sugere que volta a ter, o que aqui e falso.
        return {
            "ok": False,
            "error": "commerce_product_unavailable",
            "product_id": str(product_id),
            "active": product.active,
            "excluded": product.excluded,
        }

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
