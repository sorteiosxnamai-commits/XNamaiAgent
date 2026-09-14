"""Pergunta generica de catalogo ("o que voces vendem?") tem resposta.

Antes deste ramo, `search_products` sem texto e sem referencia voltava vazia —
e vazio o agente le como "nao temos produtos". Ausencia de filtro nao e ausencia
de catalogo, do mesmo jeito que ausencia de sync nao e catalogo vazio.

Nenhum teste faz request externo: o repositorio e injetado.
"""

from __future__ import annotations

import pytest

from app.commerce.mercos.catalog_index_reader import CatalogIndexProductReader
from app.commerce.mercos.catalog_search import search_products
from app.commerce.mercos.normalizer import normalize_product
from tests.fixtures import mercos_payloads as fx

TENANT = "xnamai"


def _row(product_id, *, ativo=True, excluido=False, nome="Produto Sintetico A"):
    """Linha do indice, no formato que o repositorio devolve."""
    return {
        "product_id": str(product_id),
        "reference": f"REF-{product_id}",
        "title_normalized": nome.casefold(),
        "price": 149.90,
        "stock": 5,
        "available": True if (ativo and not excluido) else None,
        "category": None,
        "freshness_at": None,
        "payload": {"nome": nome, "ativo": ativo, "excluido": excluido},
    }


class _Repo:
    """Repositorio em memoria que registra qual metodo foi chamado."""

    def __init__(self, rows=None, *, tenant=TENANT):
        self.rows = rows if rows is not None else []
        self.tenant = tenant
        self.calls: list[str] = []

    def list_catalog_items(self, *, tenant_id, limit):
        self.calls.append("browse")
        if tenant_id != self.tenant:
            return []
        return self.rows[:limit]

    def search_exact(self, *, tenant_id, reference=None, **kwargs):
        self.calls.append("exact")
        if tenant_id != self.tenant:
            return []
        return [r for r in self.rows if r.get("reference") == reference]

    def search_lexical(self, *, tenant_id, query, **kwargs):
        self.calls.append("lexical")
        if tenant_id != self.tenant:
            return []
        return [r for r in self.rows if query.casefold() in (r.get("title_normalized") or "")]

    def get_by_product_and_variant(self, *, tenant_id, product_id, variant_id=None):
        return next((r for r in self.rows if r["product_id"] == str(product_id)), None)


def _reader(repo):
    return CatalogIndexProductReader(repository=repo)


# === 1-2. browse ===========================================================


def test_search_without_text_or_reference_falls_back_to_browse():
    repo = _Repo([_row(1), _row(2)])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={})
    assert repo.calls == ["browse"]
    assert result["ok"] is True
    assert result["source"] == "local_index"
    assert result["count"] == 2


@pytest.mark.parametrize("args", [{}, {"limit": 5}, {"query": ""}, {"reference": "   "}])
def test_empty_filters_also_browse(args):
    """Filtro em branco e o mesmo que filtro nenhum."""
    repo = _Repo([_row(1)])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments=args)
    assert "browse" in repo.calls
    assert result["count"] == 1


def test_browse_returns_the_canonical_search_shape():
    """Nenhuma estrutura paralela: e o mesmo contrato de search_products."""
    repo = _Repo([_row(1)])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={})
    assert set(result) == {"ok", "count", "source", "products"}
    produto = result["products"][0]
    assert produto["external_id"] == "1"
    assert "freshness" in produto


def test_browse_respects_the_limit():
    repo = _Repo([_row(n) for n in range(1, 30)])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={"limit": 3})
    assert result["count"] == 3


# === 3-4. defesa comercial no browse =======================================


def test_browse_never_returns_inactive_products():
    repo = _Repo([_row(1, ativo=False), _row(2)])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={})
    assert [p["external_id"] for p in result["products"]] == ["2"]


def test_browse_never_returns_excluded_products():
    repo = _Repo([_row(1, excluido=True), _row(2)])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={})
    assert [p["external_id"] for p in result["products"]] == ["2"]


def test_browse_hides_unavailable_even_if_the_index_is_dirty():
    """Snapshot legado do indice nao pode virar vitrine."""
    repo = _Repo([_row(1, ativo=False), _row(2, excluido=True)])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={})
    assert result["count"] == 0
    assert result["ok"] is True


# === 5. isolamento por tenant ==============================================


def test_browse_is_tenant_scoped():
    repo = _Repo([_row(1)], tenant=TENANT)
    result = search_products(_reader(repo), tenant_id="outro_tenant", arguments={})
    assert result["count"] == 0


# === 6-7. vazio vs nao-sincronizado ========================================


def test_empty_catalog_is_a_valid_zero_when_the_index_is_reachable():
    repo = _Repo([])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={})
    assert result["ok"] is True
    assert result["count"] == 0


def test_without_sync_the_model_never_gets_a_search_tool_to_conclude_emptiness():
    """A distincao que evita "nao temos produtos" por falta de sync.

    Sem sync concluido o provider nao expoe tool alguma — o modelo nao chega a
    receber um resultado vazio de catalogo, entao nao pode concluir ausencia a
    partir dele. O gate e estrutural, nao textual.
    """
    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.provider import MercosCommerceProvider
    from app.commerce.mercos.sync_state import SyncState

    class _SemSync:
        def read(self, provider, resource):
            return SyncState(provider, resource)  # last_success_at=None

    provider = MercosCommerceProvider(
        MercosAdaptorClient(base_url="https://a.example.com", api_key="k"),
        index=object(),
        tenant_id=TENANT,
        sync_state=_SemSync(),
    )
    assert provider.sync_ready is False
    assert provider.available is False
    assert provider.llm_capabilities == frozenset()


# === 8-9. busca especifica continua funcionando ============================


def test_specific_query_still_uses_lexical_search():
    repo = _Repo([_row(1, nome="Relogio Azul"), _row(2, nome="Outra Coisa")])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={"query": "relogio"})
    assert "lexical" in repo.calls
    assert "browse" not in repo.calls
    assert [p["external_id"] for p in result["products"]] == ["1"]


def test_reference_still_wins_over_browse():
    repo = _Repo([_row(1), _row(2)])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={"reference": "REF-2"})
    assert repo.calls[0] == "exact"
    assert "browse" not in repo.calls
    assert [p["external_id"] for p in result["products"]] == ["2"]


def test_browse_never_uses_category_id():
    """`categoria_id` nao existe no payload real: segue recusado."""
    repo = _Repo([_row(1)])
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={"category_id": "55"})
    assert result["ok"] is False
    assert result["error"] == "unsupported_filter"
    assert repo.calls == []


# === 10. nenhum request externo ============================================


def test_browse_performs_no_http_call():
    """O browse le o indice local; nunca o adaptador."""
    import httpx

    def explode(*args, **kwargs):
        raise AssertionError("browse nao pode fazer request externo")

    original = httpx.AsyncClient.send
    httpx.AsyncClient.send = explode
    try:
        repo = _Repo([_row(1)])
        result = search_products(_reader(repo), tenant_id=TENANT, arguments={})
    finally:
        httpx.AsyncClient.send = original
    assert result["count"] == 1


def test_browse_never_leaks_raw_payload():
    repo = _Repo([_row(1)])
    repo.rows[0]["payload"]["observacoes"] = "anotacao interna"
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={})
    assert "anotacao interna" not in str(result)


def test_normalizer_keeps_missing_stock_as_none_in_browse():
    repo = _Repo([_row(1)])
    repo.rows[0]["stock"] = None
    result = search_products(_reader(repo), tenant_id=TENANT, arguments={})
    assert result["products"][0]["stock"] is None
