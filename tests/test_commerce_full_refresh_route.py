"""A porta do full refresh: administrativa, confirmada e separada do cron.

Reconstruir o catalogo e varredura completa contra o adaptador — dezenas de
paginas. Disparar por engano custa caro, entao a rota exige a frase exata no
corpo alem do token. E nao herda o segredo de remarketing: desligar aquela
feature nao pode derrubar o reparo do catalogo.

Nenhum teste toca banco, adaptador ou rede.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ROTA = "/api/admin/commerce/sync/products/full-refresh"
ROTA_INCREMENTAL = "/api/admin/commerce/sync/products"
CONFIRMACAO = {"confirm": "FULL_REFRESH_PRODUCTS"}


@pytest.fixture
def client():
    import api.index as index

    return TestClient(index.app), index


@pytest.fixture
def com_token(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("ADMIN_API_TOKEN", "token-correto")
    get_settings.cache_clear()
    yield {"Authorization": "Bearer token-correto"}
    get_settings.cache_clear()


# --- a rota existe e e distinta da incremental -----------------------------


def test_the_route_exists_and_is_separate_from_the_incremental_one(client):
    _, index = client
    caminhos = {r.path for r in index.app.routes}
    assert ROTA in caminhos
    assert ROTA_INCREMENTAL in caminhos


def test_the_route_does_not_use_the_remarketing_secret(client):
    import inspect

    _, index = client
    fonte = inspect.getsource(index)
    bloco = fonte[fonte.index("commerce_product_full_refresh_admin") :]
    bloco = bloco[: bloco.index("return result")]
    assert "verify_remarketing_cron" not in bloco


# --- autenticacao ----------------------------------------------------------


def test_without_a_configured_token_the_route_is_closed(client):
    api, _ = client
    resposta = api.post(ROTA, json=CONFIRMACAO)
    assert resposta.status_code == 500
    assert resposta.json()["detail"] == "admin_token_not_configured"


def test_a_wrong_token_is_rejected(client, com_token):
    api, _ = client
    resposta = api.post(
        ROTA, json=CONFIRMACAO, headers={"Authorization": "Bearer errado"}
    )
    assert resposta.status_code == 401


def test_the_vercel_bypass_is_not_application_authentication(client, com_token):
    api, _ = client
    resposta = api.post(
        ROTA, json=CONFIRMACAO, headers={"x-vercel-protection-bypass": "qualquer"}
    )
    assert resposta.status_code == 401


def test_the_mercos_api_key_is_not_accepted_as_admin_auth(client, com_token):
    api, _ = client
    resposta = api.post(ROTA, json=CONFIRMACAO, headers={"X-API-Key": "chave-do-adaptor"})
    assert resposta.status_code == 401


# --- confirmacao explicita -------------------------------------------------


@pytest.mark.parametrize(
    "corpo",
    [None, {}, {"confirm": ""}, {"confirm": "sim"}, {"confirm": "full_refresh_products"}],
)
def test_without_the_exact_confirmation_the_route_refuses(client, com_token, corpo):
    api, _ = client
    resposta = api.post(ROTA, json=corpo, headers=com_token)
    assert resposta.status_code == 400
    assert resposta.json()["detail"]["error"] == "confirmation_required"


def test_the_refusal_says_what_was_expected(client, com_token):
    api, _ = client
    detalhe = api.post(ROTA, json={}, headers=com_token).json()["detail"]
    assert detalhe["expected"] == CONFIRMACAO


def test_confirmation_is_checked_before_anything_runs(client, com_token, monkeypatch):
    """Recusa por confirmacao nao pode ter tocado o adaptador."""
    chamadas = []

    async def espiao():
        chamadas.append("rodou")
        return {"ok": True}

    monkeypatch.setattr(
        "app.commerce.health.run_configured_product_full_refresh", espiao
    )
    api, _ = client
    api.post(ROTA, json={"confirm": "errado"}, headers=com_token)
    assert chamadas == []


# --- o caminho feliz -------------------------------------------------------


def test_with_token_and_confirmation_the_refresh_runs(client, com_token, monkeypatch):
    async def falso():
        return {
            "ok": True, "mode": "full_refresh", "complete": True,
            "resource": "products", "pages": 40, "records": 19635,
        }

    monkeypatch.setattr(
        "app.commerce.health.run_configured_product_full_refresh", falso
    )
    api, _ = client
    resposta = api.post(ROTA, json=CONFIRMACAO, headers=com_token)
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["ok"] is True
    assert corpo["mode"] == "full_refresh"
    assert corpo["complete"] is True


def test_the_two_routes_call_different_runners(client, com_token, monkeypatch):
    """O reparo nao pode virar o caminho de rotina, nem vice-versa."""
    chamadas: list[str] = []

    async def refresh():
        chamadas.append("full_refresh")
        return {"ok": True}

    async def incremental():
        chamadas.append("incremental")
        return {"ok": True}

    monkeypatch.setattr("app.commerce.health.run_configured_product_full_refresh", refresh)
    monkeypatch.setattr("app.commerce.health.run_configured_product_sync", incremental)

    api, _ = client
    api.post(ROTA, json=CONFIRMACAO, headers=com_token)
    api.post(ROTA_INCREMENTAL, headers=com_token)
    assert chamadas == ["full_refresh", "incremental"]


def test_the_response_never_leaks_cursor_or_payload(client, com_token, monkeypatch):
    async def falso():
        return {"ok": True, "mode": "full_refresh", "complete": True, "pages": 1}

    monkeypatch.setattr(
        "app.commerce.health.run_configured_product_full_refresh", falso
    )
    api, _ = client
    corpo = api.post(ROTA, json=CONFIRMACAO, headers=com_token).json()
    for proibido in ("last_cursor", "payload", "data"):
        assert proibido not in corpo


def test_an_unconfigured_provider_refuses_explicitly(client, com_token):
    """Sem provider comercial, recusa nomeada — nunca sucesso fingido."""
    api, _ = client
    corpo = api.post(ROTA, json=CONFIRMACAO, headers=com_token).json()
    assert corpo["ok"] is False
    assert corpo["error"] in {
        "commerce_adaptor_not_configured",
        "database_not_configured",
        "full_refresh_not_supported_by_provider",
        "sync_state_table_missing",
    }
