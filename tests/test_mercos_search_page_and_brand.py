"""Paginacao real e marca como pista, na busca do catalogo Mercos.

O defeito que estes testes impedem deixou 6.120 produtos inalcancaveis com o
catalogo perfeito e as tools verdes. `app/sales_agent.py` monta TODA requisicao
de busca como::

    {**request.tool_arguments(), "limit": ..., "page": request.page}

e `page` estava entre os filtros recusados. Resultado: 100% das buscas do agente
voltavam `unsupported_filter`, viravam `product_lookup_failed` e o cliente ouvia
"Nao consegui consultar as informacoes da loja" — enquanto a mesma busca sem
`page` devolvia dez produtos.

A licao das duas correcoes e a mesma, em direcoes opostas. `page` era recusado
sem precisar: paginar sobre o indice local e trivial, entao implementar e a
resposta certa. `brand` NAO tem campo correspondente na Mercos: vira PISTA que
reordena, nunca filtro que descarta — porque descartar esconderia produto que
existe, e lista vazia o agente le como "nao temos".
"""

from __future__ import annotations

from typing import Any

import pytest

from app.commerce.mercos.catalog_index_reader import CatalogIndexProductReader
from app.commerce.mercos.catalog_search import (
    HINT_FILTERS,
    SUPPORTED_FILTERS,
    UNSUPPORTED_FILTERS,
    prefer_brand,
    resolve_page,
    search_products,
)

TENANT = "xnamai"


def _linha(n: int, nome: str | None = None) -> dict[str, Any]:
    titulo = nome or f"Produto {n:03d}"
    return {
        "product_id": str(n),
        "reference": f"REF-{n}",
        "title_normalized": titulo.casefold(),
        "price": 10.0 + n,
        "stock": 5,
        "available": True,
        "category": None,
        "freshness_at": None,
        "payload": {"nome": titulo, "ativo": True, "excluido": False},
    }


class _Repo:
    """Duplo que pagina como o SQL: LIMIT/OFFSET, nao recorte em memoria."""

    def __init__(self, linhas):
        self.linhas = linhas
        self.chamadas: list[dict] = []

    def list_catalog_items(self, *, tenant_id, limit, offset=0):
        self.chamadas.append({"metodo": "browse", "limit": limit, "offset": offset})
        if tenant_id != TENANT:
            return []
        return self.linhas[offset : offset + limit]

    def search_lexical(self, *, tenant_id, query, limit=None, offset=0, **kwargs):
        self.chamadas.append(
            {"metodo": "lexical", "query": query, "limit": limit, "offset": offset}
        )
        if tenant_id != TENANT:
            return []
        alvo = (query or "").casefold()
        casam = [l for l in self.linhas if alvo in (l["title_normalized"] or "")]
        limite = limit or len(casam)
        return casam[offset : offset + limite]

    def search_exact(self, *, tenant_id, reference=None, **kwargs):
        return [l for l in self.linhas if l.get("reference") == reference]

    def get_by_product_and_variant(self, *, tenant_id, product_id, variant_id=None):
        return next((l for l in self.linhas if l["product_id"] == str(product_id)), None)


def _buscar(linhas, **args):
    return search_products(
        CatalogIndexProductReader(repository=_Repo(linhas)),
        tenant_id=TENANT,
        arguments=args,
    )


def _ids(resultado) -> list[str]:
    return [p["external_id"] for p in resultado["products"]]


# === 1. page deixou de ser recusado ========================================


def test_page_is_a_supported_filter_now():
    assert "page" in SUPPORTED_FILTERS
    assert "page" not in UNSUPPORTED_FILTERS


def test_the_exact_argument_shape_the_agent_sends_is_accepted():
    """A forma literal de `sales_agent._run_probe`, que voltava erro."""
    resultado = _buscar([_linha(n) for n in range(1, 6)], query="produto", limit=10, page=1)
    assert resultado["ok"] is True
    assert resultado.get("error") is None
    assert resultado["count"] == 5


def test_page_one_returns_the_first_window():
    resultado = _buscar([_linha(n) for n in range(1, 26)], limit=10, page=1)
    assert _ids(resultado) == [str(n) for n in range(1, 11)]
    assert resultado["paging"] == {"page": 1, "limit": 10, "returned": 10}


def test_page_two_returns_the_next_window():
    resultado = _buscar([_linha(n) for n in range(1, 26)], limit=10, page=2)
    assert _ids(resultado) == [str(n) for n in range(11, 21)]
    assert resultado["paging"]["page"] == 2


def test_pages_do_not_overlap():
    linhas = [_linha(n) for n in range(1, 31)]
    p1 = set(_ids(_buscar(linhas, limit=10, page=1)))
    p2 = set(_ids(_buscar(linhas, limit=10, page=2)))
    assert p1 & p2 == set()


def test_the_last_page_returns_the_remainder():
    resultado = _buscar([_linha(n) for n in range(1, 16)], limit=10, page=2)
    assert resultado["count"] == 5
    assert resultado["paging"]["returned"] == 5


def test_the_offset_reaches_the_index_instead_of_being_sliced_in_memory():
    """A janela sai do banco: paginar nao le o comeco so para descartar."""
    repo = _Repo([_linha(n) for n in range(1, 60)])
    search_products(
        CatalogIndexProductReader(repository=repo),
        tenant_id=TENANT,
        arguments={"limit": 10, "page": 4},
    )
    assert repo.chamadas[0]["offset"] == 30
    assert repo.chamadas[0]["limit"] == 10


@pytest.mark.parametrize("bruto,esperado", [(None, 1), (0, 1), (-3, 1), ("2", 2), ("abc", 1), (3, 3)])
def test_an_invalid_page_falls_back_to_the_first(bruto, esperado):
    """Pagina invalida nao vira erro: o agente perderia o catalogo de novo."""
    assert resolve_page(bruto) == esperado


def test_a_page_past_the_end_is_empty_not_an_error():
    resultado = _buscar([_linha(n) for n in range(1, 26)], limit=10, page=99)
    assert resultado["ok"] is True
    assert resultado["count"] == 0
    assert resultado["paging"]["returned"] == 0


def test_the_page_size_asked_of_the_index_is_the_page_size():
    """Sem inflar a leitura: o banco devolve exatamente uma pagina."""
    repo = _Repo([_linha(n) for n in range(1, 500)])
    search_products(
        CatalogIndexProductReader(repository=repo),
        tenant_id=TENANT,
        arguments={"limit": 20, "page": 5},
    )
    assert repo.chamadas[0]["limit"] == 20
    assert repo.chamadas[0]["offset"] == 80


# === 2. brand e pista, nunca filtro =======================================


def test_brand_is_a_hint_not_a_rejected_filter():
    assert "brand" in HINT_FILTERS
    assert "brand" not in UNSUPPORTED_FILTERS


def test_brand_reorders_without_discarding():
    """O ponto central: nada some por causa da marca."""
    linhas = [_linha(1, "Cabo Generico"), _linha(2, "Cabo Hmaston"), _linha(3, "Cabo Outro")]
    resultado = _buscar(linhas, query="cabo", brand="Hmaston", limit=10)
    assert resultado["count"] == 3, "marca virou filtro e escondeu produto"
    assert _ids(resultado)[0] == "2", "marca pedida nao foi priorizada"


def test_an_unmatched_brand_still_returns_everything():
    linhas = [_linha(1, "Cabo Generico"), _linha(2, "Cabo Outro")]
    resultado = _buscar(linhas, query="cabo", brand="MarcaInexistente", limit=10)
    assert resultado["count"] == 2


def test_brand_alone_becomes_the_lexical_search():
    linhas = [_linha(1, "Cabo Hmaston"), _linha(2, "Fone Outro")]
    resultado = _buscar(linhas, brand="hmaston", limit=10)
    assert _ids(resultado) == ["1"]


def test_brand_matching_is_case_insensitive():
    linhas = [_linha(1, "Cabo Generico"), _linha(2, "Cabo HMASTON")]
    assert _ids(_buscar(linhas, query="cabo", brand="hmaston", limit=10))[0] == "2"


def test_prefer_brand_is_stable_within_each_group():
    class _P:
        def __init__(self, nome, pid):
            self.name = nome
            self.external_id = pid

    itens = [_P("A Hmaston", "1"), _P("B", "2"), _P("C Hmaston", "3"), _P("D", "4")]
    assert [p.external_id for p in prefer_brand(itens, "hmaston")] == ["1", "3", "2", "4"]


def test_prefer_brand_without_a_brand_changes_nothing():
    linhas = [_linha(1), _linha(2)]
    assert _ids(_buscar(linhas, query="produto", limit=10)) == ["1", "2"]


# === 3. o que continua recusado ===========================================


@pytest.mark.parametrize("filtro,valor", [("ean", "789"), ("category_id", "55"), ("tokens", ["a"])])
def test_genuinely_unmapped_filters_are_still_rejected(filtro, valor):
    """Sem mapeamento real, recusa explicita continua sendo o certo."""
    resultado = _buscar([_linha(1)], **{filtro: valor})
    assert resultado["ok"] is False
    assert resultado["error"] == "unsupported_filter"
    assert resultado["unsupported_filters"] == [filtro]


def test_the_rejection_tells_what_is_supported_and_what_is_a_hint():
    resultado = _buscar([_linha(1)], ean="789")
    assert "page" in resultado["supported_filters"]
    assert resultado["hint_filters"] == ["brand"]


def test_an_empty_unsupported_filter_does_not_reject():
    resultado = _buscar([_linha(1)], query="produto", ean="", category_id=None)
    assert resultado["ok"] is True


# === 4. nada do que ja funcionava regrediu ================================


def test_reference_still_wins():
    resultado = _buscar([_linha(1), _linha(2)], reference="REF-2", page=1, limit=10)
    assert _ids(resultado) == ["2"]


def test_browse_without_filters_still_works():
    resultado = _buscar([_linha(n) for n in range(1, 4)])
    assert resultado["ok"] is True
    assert resultado["count"] == 3
    assert resultado["source"] == "local_index"
    assert resultado["paging"]["page"] == 1


def test_the_lifecycle_defence_survives_pagination():
    linhas = [_linha(n) for n in range(1, 6)]
    linhas[0]["payload"]["ativo"] = False
    resultado = _buscar(linhas, limit=10, page=1)
    assert "1" not in _ids(resultado)


def test_the_result_still_carries_freshness():
    resultado = _buscar([_linha(1)], limit=10, page=1)
    assert "freshness" in resultado["products"][0]


def test_the_brand_is_never_concatenated_into_the_lexical_query():
    """`"Hmaston cabo"` zeraria o recall: a leitura e um LIKE de trecho unico."""
    repo = _Repo([_linha(1, "Cabo Hmaston")])
    search_products(
        CatalogIndexProductReader(repository=repo),
        tenant_id=TENANT,
        arguments={"query": "cabo", "brand": "Hmaston", "limit": 10},
    )
    lexicais = [c for c in repo.chamadas if c["metodo"] == "lexical"]
    assert lexicais[0]["query"] == "cabo"
    assert "hmaston cabo" not in str(repo.chamadas).casefold()


def test_the_brand_becomes_an_alternative_query_when_the_text_finds_nothing():
    """Ampliar recuperacao, nunca estreitar: so roda se o texto voltou vazio."""
    repo = _Repo([_linha(1, "Fone Hmaston")])
    resultado = search_products(
        CatalogIndexProductReader(repository=repo),
        tenant_id=TENANT,
        arguments={"query": "inexistente", "brand": "Hmaston", "limit": 10},
    )
    consultas = [c["query"] for c in repo.chamadas if c["metodo"] == "lexical"]
    assert consultas == ["inexistente", "hmaston"]
    assert resultado["count"] == 1


def test_the_alternative_query_does_not_run_when_the_text_already_matched():
    repo = _Repo([_linha(1, "Cabo Hmaston"), _linha(2, "Cabo Outro")])
    search_products(
        CatalogIndexProductReader(repository=repo),
        tenant_id=TENANT,
        arguments={"query": "cabo", "brand": "Hmaston", "limit": 10},
    )
    consultas = [c["query"] for c in repo.chamadas if c["metodo"] == "lexical"]
    assert consultas == ["cabo"]


def test_paging_is_reported_on_every_successful_search():
    for args in ({}, {"query": "produto"}, {"limit": 5, "page": 2}):
        resultado = _buscar([_linha(n) for n in range(1, 30)], **args)
        assert set(resultado["paging"]) == {"page", "limit", "returned"}
        assert resultado["paging"]["returned"] == resultado["count"]


def test_the_paginated_queries_order_by_a_unique_tiebreaker():
    """Sem desempate, OFFSET sobre empate repete e pula linhas.

    Descoberto contra o catalogo real: depois de um full refresh milhares de
    linhas compartilham o mesmo `freshness_at`, e a page 3 trazia um produto que
    a page 2 ja tinha mostrado.
    """
    import inspect

    from app.catalog_index_repository import CatalogIndexRepository

    for metodo in (
        CatalogIndexRepository.list_catalog_items,
        CatalogIndexRepository.search_lexical,
    ):
        fonte = inspect.getsource(metodo)
        assert "OFFSET %(offset)s" in fonte, f"{metodo.__name__} nao pagina"
        assert "catalog_item_key" in fonte.split("ORDER BY")[1].split("LIMIT")[0], (
            f"{metodo.__name__} pagina sem desempate estavel"
        )


# === 5. o contrato que o caminho legado consome ===========================


def test_every_product_carries_the_id_key_the_legacy_path_reads():
    """`sales_agent._absorb_products` descarta produto sem `id`.

    O Mercos apresentava so `external_id`, entao TODO candidato era descartado
    na absorcao e o cliente ouvia "nao encontrei opcoes" com o catalogo cheio.
    E o mesmo identificador sob a chave que o consumidor procura.
    """
    resultado = _buscar([_linha(n) for n in range(1, 4)])
    for produto in resultado["products"]:
        assert produto.get("id") is not None
        assert produto["id"] == produto["external_id"]


def test_the_absorption_gate_would_keep_every_product():
    """Reproduz literalmente a condicao de `_absorb_products`."""
    resultado = _buscar([_linha(n) for n in range(1, 6)])
    aceitos = [
        p for p in resultado["products"]
        if isinstance(p, dict) and p.get("id") is not None
    ]
    assert len(aceitos) == resultado["count"] == 5
