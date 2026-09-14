"""As duas portas do sync de catalogo, e o que cada uma protege.

O caminho OPERACIONAL da integracao comercial nao pode depender do segredo de
remarketing: desligar o remarketing derrubaria o sync junto, e quem auditasse
permissoes depois encontraria uma feature autenticada pelo secret de outra.

A rota administrativa existe para isso. A de cron continua, porque o agendador
da plataforma so sabe mandar o header de cron ja configurado no projeto.

Nenhum teste toca banco, adaptador ou rede.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_ROUTE = "/api/admin/commerce/sync/products"
CRON_ROUTE = "/api/cron/commerce/sync/products"


@pytest.fixture
def client():
    import api.index as index

    return TestClient(index.app), index


# --- as duas rotas existem e sao distintas ---------------------------------


def test_both_sync_routes_exist(client):
    _, index = client
    caminhos = {r.path for r in index.app.routes}
    assert ADMIN_ROUTE in caminhos
    assert CRON_ROUTE in caminhos


def test_admin_route_does_not_depend_on_the_remarketing_secret(client):
    """A rota operacional e autenticada por ADMIN_API_TOKEN, nao pelo cron."""
    import inspect

    _, index = client
    fonte = inspect.getsource(index)
    bloco = fonte[fonte.index(ADMIN_ROUTE) : fonte.index(CRON_ROUTE)]
    assert "verify_admin_token" in bloco
    assert "verify_remarketing_cron" not in bloco


# --- falha fechada ---------------------------------------------------------


def test_admin_route_is_closed_without_a_configured_token(client):
    """Sem ADMIN_API_TOKEN a rota recusa TODO MUNDO — nunca fica aberta."""
    api, _ = client
    resposta = api.post(ADMIN_ROUTE)
    assert resposta.status_code == 500
    assert resposta.json()["detail"] == "admin_token_not_configured"


def test_admin_route_rejects_a_wrong_token(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("ADMIN_API_TOKEN", "token-correto")
    get_settings.cache_clear()
    try:
        api, _ = client
        resposta = api.post(ADMIN_ROUTE, headers={"Authorization": "Bearer token-errado"})
        assert resposta.status_code == 401
        assert resposta.json()["detail"] == "invalid_admin_token"
    finally:
        get_settings.cache_clear()


def test_admin_route_rejects_a_missing_authorization_header(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("ADMIN_API_TOKEN", "token-correto")
    get_settings.cache_clear()
    try:
        api, _ = client
        assert api.post(ADMIN_ROUTE).status_code == 401
    finally:
        get_settings.cache_clear()


def test_the_vercel_bypass_is_never_application_authentication(client, monkeypatch):
    """Proteger o deployment nao autentica a aplicacao."""
    from app.config import get_settings

    monkeypatch.setenv("ADMIN_API_TOKEN", "token-correto")
    get_settings.cache_clear()
    try:
        api, _ = client
        resposta = api.post(
            ADMIN_ROUTE, headers={"x-vercel-protection-bypass": "qualquer-coisa"}
        )
        assert resposta.status_code == 401
    finally:
        get_settings.cache_clear()


# --- o runner e o mesmo ----------------------------------------------------


def test_admin_route_reuses_the_existing_runner(client, monkeypatch):
    """Nenhuma logica de sync duplicada: as duas portas chamam o mesmo runner."""
    from app.config import get_settings

    chamadas = []

    async def fake_runner():
        chamadas.append("chamado")
        return {"ok": True, "resource": "products", "pages": 0, "records": 0}

    monkeypatch.setattr("app.commerce.health.run_configured_product_sync", fake_runner)
    monkeypatch.setenv("ADMIN_API_TOKEN", "token-correto")
    get_settings.cache_clear()
    try:
        api, _ = client
        resposta = api.post(ADMIN_ROUTE, headers={"Authorization": "Bearer token-correto"})
        assert resposta.status_code == 200
        assert resposta.json()["ok"] is True
        assert chamadas == ["chamado"]
    finally:
        get_settings.cache_clear()


def test_both_routes_call_the_same_facade():
    import inspect

    import api.index as index

    fonte = inspect.getsource(index)
    assert fonte.count("run_configured_product_sync()") == 2


def test_sync_result_never_leaks_cursor_or_payload(client, monkeypatch):
    from app.config import get_settings

    async def fake_runner():
        return {
            "ok": True, "resource": "products", "pages": 1,
            "records": 3, "skipped": 0, "error_code": None,
            "retry_after": None, "stopped_reason": None, "cursor_advanced": True,
        }

    monkeypatch.setattr("app.commerce.health.run_configured_product_sync", fake_runner)
    monkeypatch.setenv("ADMIN_API_TOKEN", "token-correto")
    get_settings.cache_clear()
    try:
        api, _ = client
        corpo = api.post(
            ADMIN_ROUTE, headers={"Authorization": "Bearer token-correto"}
        ).json()
        assert "last_cursor" not in corpo
        assert "payload" not in corpo
        assert "data" not in corpo
    finally:
        get_settings.cache_clear()


# --- o sync nao inventa prontidao ------------------------------------------


def test_running_the_route_without_configuration_refuses_explicitly(client, monkeypatch):
    """Sem adaptador configurado o sync recusa — nao finge sucesso."""
    from app.config import get_settings

    monkeypatch.setenv("ADMIN_API_TOKEN", "token-correto")
    get_settings.cache_clear()
    try:
        api, _ = client
        corpo = api.post(
            ADMIN_ROUTE, headers={"Authorization": "Bearer token-correto"}
        ).json()
        assert corpo["ok"] is False
        assert corpo["error"] in {
            "commerce_adaptor_not_configured",
            "database_not_configured",
            "sync_not_supported_by_provider",
        }
    finally:
        get_settings.cache_clear()
