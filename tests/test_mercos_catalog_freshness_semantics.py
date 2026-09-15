"""`freshness_at` e o instante do SYNC, nunca a data de alteracao na origem.

O bug que estes testes impedem levou o catalogo inteiro a sumir em producao,
com o sync reportando sucesso:

    19.635 registros lidos, 26 indexados, `ok: true`
    search_products(...) -> count: 0   para TODOS eles

A causa era uma colisao de significado numa unica coluna. O produtor
(`product_to_index_fields`) gravava `freshness_at = ultima_alteracao` — quando a
Mercos mudou o registro. O consumidor (`CatalogIndexRepository`) le a mesma
coluna como TTL de confirmacao — ha quanto tempo NOS confirmamos esta linha —
numa janela de 24h. Como `ultima_alteracao` quase nunca e de hoje, todo produto
estavel caia fora da janela: catalogo vazio, para sempre, por mais recente que
fosse o sync.

Os dois lados eram defensaveis isoladamente. A falha so existia na junta — que e
exatamente onde a suite nao olhava, porque os testes de browse usam repositorio
em memoria, sem coluna `freshness_at` e sem clausula de TTL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.catalog_index_repository import CatalogIndexRepository
from app.commerce.mercos.catalog import product_to_index_fields
from app.commerce.mercos.catalog_index_reader import CatalogIndexProductReader
from app.commerce.mercos.catalog_search import search_products
from app.commerce.mercos.normalizer import normalize_product
from tests.fixtures import mercos_payloads as fx

TENANT = "xnamai"

#: A data real que quebrou producao: o produto mais antigo do indice.
DATA_ANTIGA_NA_ORIGEM = "2025-11-20T08:56:18"

#: O sync que confirmou o snapshot — quase dez meses depois.
INSTANTE_DO_SYNC = datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)


def _campos(**overrides: Any) -> dict[str, Any]:
    overrides.setdefault("ultima_alteracao", DATA_ANTIGA_NA_ORIGEM)
    row = fx.product(**overrides)
    return product_to_index_fields(normalize_product(row), synced_at=INSTANTE_DO_SYNC)


# === 1. o produtor grava o instante do sync ================================


def test_freshness_at_is_the_sync_instant_not_the_source_change_date():
    campos = _campos()
    assert campos["freshness_at"] == INSTANTE_DO_SYNC
    assert campos["freshness_at"].year == 2026, "voltou a usar ultima_alteracao"


def test_an_old_source_date_does_not_age_a_snapshot_just_confirmed():
    """Dez meses sem alteracao na origem nao tornam o snapshot velho."""
    campos = _campos()
    idade = INSTANTE_DO_SYNC - campos["freshness_at"]
    assert idade == timedelta(0)


def test_the_source_change_date_is_preserved_in_the_payload():
    """`ultima_alteracao` nao se perde: vira metadado da origem."""
    campos = _campos()
    assert campos["payload"]["ultima_alteracao"] == DATA_ANTIGA_NA_ORIGEM


def test_without_an_explicit_sync_instant_it_falls_back_to_now():
    antes = datetime.now(timezone.utc)
    campos = product_to_index_fields(
        normalize_product(fx.product(ultima_alteracao=DATA_ANTIGA_NA_ORIGEM))
    )
    assert antes <= campos["freshness_at"] <= datetime.now(timezone.utc)


def test_a_product_without_a_source_date_also_gets_the_sync_instant():
    campos = _campos(ultima_alteracao=None)
    assert campos["freshness_at"] == INSTANTE_DO_SYNC


# === 2. a junta real: produtor -> SQL do repositorio -> leitura ============


class _MotorFalso:
    """Substitui SOMENTE o banco, nunca o repositorio.

    O SQL e os parametros vem do `CatalogIndexRepository` de verdade; aqui so se
    avalia o predicado que esse SQL expressa. E o que torna o teste hermetico
    sem deixar de exercitar a clausula de TTL que o mock em memoria pulava.
    """

    def __init__(self, linhas: list[dict[str, Any]]):
        self.linhas = linhas
        self.ultimo_sql = ""
        self.ultimos_params: dict[str, Any] = {}

    def __call__(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.ultimo_sql = sql
        self.ultimos_params = params
        resultado = [
            linha for linha in self.linhas
            if linha["tenant_id"] == params["tenant_id"]
        ]
        cutoff = params.get("cutoff")
        if cutoff is not None:
            resultado = [
                linha for linha in resultado
                if (linha.get("freshness_at") or linha.get("updated_at")) >= cutoff
            ]
        return resultado[: params.get("limit", len(resultado))]


def _linha_do_indice(**overrides: Any) -> dict[str, Any]:
    """Linha como o banco a devolveria depois do upsert do sync."""
    campos = _campos(**overrides)
    return {
        "tenant_id": TENANT,
        "catalog_item_key": f"p:{campos['product_id']}",
        "updated_at": INSTANTE_DO_SYNC,
        "variant_id": None,
        **campos,
    }


@pytest.fixture
def repositorio(monkeypatch):
    def montar(linhas):
        motor = _MotorFalso(linhas)
        repo = CatalogIndexRepository()
        monkeypatch.setattr(repo, "_fetch", motor)
        return repo, motor

    return montar


def test_the_repository_ttl_clause_is_actually_exercised(repositorio):
    """Guarda contra o teste virar vacuo se o TTL sumir do SQL."""
    repo, motor = repositorio([_linha_do_indice()])
    repo.list_catalog_items(tenant_id=TENANT, limit=10)
    assert "coalesce(freshness_at, updated_at)" in motor.ultimo_sql
    assert "cutoff" in motor.ultimos_params, "TTL desligado — o teste perderia o sentido"


def test_a_just_synced_product_survives_the_24h_ttl(repositorio):
    """A regressao central: com a semantica correta, a linha e legivel."""
    repo, _ = repositorio([_linha_do_indice()])
    linhas = repo.list_catalog_items(tenant_id=TENANT, limit=10)
    assert len(linhas) == 1


def test_with_the_old_semantics_the_catalog_would_vanish(repositorio):
    """Reproduz o bug: gravando a data da origem, a mesma leitura volta vazia."""
    linha = _linha_do_indice()
    linha["freshness_at"] = datetime(2025, 11, 20, 8, 56, 18, tzinfo=timezone.utc)
    repo, _ = repositorio([linha])
    assert repo.list_catalog_items(tenant_id=TENANT, limit=10) == []


def test_a_genuinely_stale_snapshot_is_still_filtered_out(repositorio):
    """A correcao nao desligou o TTL: snapshot antigo continua invisivel."""
    linha = _linha_do_indice()
    antigo = datetime.now(timezone.utc) - timedelta(days=3)
    linha["freshness_at"] = antigo
    linha["updated_at"] = antigo
    repo, _ = repositorio([linha])
    assert repo.list_catalog_items(tenant_id=TENANT, limit=10) == []


# === 3. ponta a ponta: Mercos -> indice -> search_products =================


class _RepoAdaptado:
    """Expoe o repositorio real com a interface que o reader consome."""

    def __init__(self, repo):
        self._repo = repo

    def list_catalog_items(self, *, tenant_id, limit):
        return self._repo.list_catalog_items(tenant_id=tenant_id, limit=limit)

    def search_exact(self, *, tenant_id, reference=None, **kwargs):
        return [
            linha for linha in self._repo.list_catalog_items(tenant_id=tenant_id, limit=100)
            if linha.get("reference") == reference
        ]

    def search_lexical(self, *, tenant_id, query, **kwargs):
        alvo = (query or "").casefold()
        return [
            linha for linha in self._repo.list_catalog_items(tenant_id=tenant_id, limit=100)
            if alvo in (linha.get("title_normalized") or "")
        ]

    def get_by_product_and_variant(self, *, tenant_id, product_id, variant_id=None):
        return next(
            (
                linha
                for linha in self._repo.list_catalog_items(tenant_id=tenant_id, limit=100)
                if linha["product_id"] == str(product_id)
            ),
            None,
        )


def test_generic_browse_returns_a_product_synced_from_an_old_source_record(repositorio):
    """A pergunta que voltava vazia em producao: "o que voces vendem?"."""
    repo, _ = repositorio([_linha_do_indice()])
    reader = CatalogIndexProductReader(repository=_RepoAdaptado(repo))
    resultado = search_products(reader, tenant_id=TENANT, arguments={})
    assert resultado["ok"] is True
    assert resultado["count"] == 1
    assert resultado["source"] == "local_index"


def _linha_sincronizada_agora(**overrides: Any) -> dict[str, Any]:
    """Como `_linha_do_indice`, mas com o sync ACONTECENDO agora.

    A politica de frescor mede contra o relogio real (estoque vence em 1h), e um
    instante fixo no passado faria estes dois testes passarem pela manha e
    falharem a tarde.
    """
    overrides.setdefault("ultima_alteracao", DATA_ANTIGA_NA_ORIGEM)
    agora = datetime.now(timezone.utc)
    campos = product_to_index_fields(
        normalize_product(fx.product(**overrides)), synced_at=agora
    )
    return {
        "tenant_id": TENANT,
        "catalog_item_key": f"p:{campos['product_id']}",
        "updated_at": agora,
        "variant_id": None,
        **campos,
    }


def test_price_and_stock_are_confirmed_right_after_a_sync(repositorio):
    """Freshness correto significa fato afirmavel — nao "nao confirmado"."""
    repo, _ = repositorio([_linha_sincronizada_agora()])
    reader = CatalogIndexProductReader(repository=_RepoAdaptado(repo))
    produto = search_products(reader, tenant_id=TENANT, arguments={})["products"][0]
    freshness = produto["freshness"]
    assert freshness["price_confirmed"] is True
    assert freshness["stock_confirmed"] is True


def test_an_old_source_date_never_invalidates_a_fresh_snapshot(repositorio):
    """`ultima_alteracao` de 2025 nao pode marcar como vencido o que acabou de
    ser confirmado — era o efeito colateral da semantica antiga."""
    linha = _linha_sincronizada_agora()
    assert linha["payload"]["ultima_alteracao"] == DATA_ANTIGA_NA_ORIGEM
    repo, _ = repositorio([linha])
    reader = CatalogIndexProductReader(repository=_RepoAdaptado(repo))
    produto = search_products(reader, tenant_id=TENANT, arguments={})["products"][0]
    assert produto["freshness"]["price_confirmed"] is True
    assert produto["freshness"]["stock_confirmed"] is True


def test_lexical_search_also_crosses_the_ttl_boundary(repositorio):
    repo, _ = repositorio([_linha_do_indice()])
    reader = CatalogIndexProductReader(repository=_RepoAdaptado(repo))
    resultado = search_products(reader, tenant_id=TENANT, arguments={"query": "sintetico"})
    assert resultado["count"] == 1


def test_the_lifecycle_defence_still_applies_after_the_fix(repositorio):
    """Corrigir freshness nao pode ressuscitar produto inativo."""
    linha = _linha_do_indice(ativo=False)
    repo, _ = repositorio([linha])
    reader = CatalogIndexProductReader(repository=_RepoAdaptado(repo))
    assert search_products(reader, tenant_id=TENANT, arguments={})["count"] == 0
