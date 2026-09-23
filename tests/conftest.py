"""Test isolation: ignore local dotenv files and forbid live DB/HTTP traffic.

Tests can replace psycopg with explicit fakes and HTTPX with MockTransport,
ASGITransport, WSGITransport or FastAPI TestClient. Blocking connections also
protects developer machines that have production credentials in the environment.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_database_connections(monkeypatch):
    import psycopg

    def blocked(*args, **kwargs):
        raise AssertionError("teste tentou abrir conexão real de banco — use um fake")

    monkeypatch.setattr(psycopg, "connect", blocked)

@pytest.fixture(autouse=True, scope="session")
def _isolate_commerce_environment():
    """A suite ignora o arquivo `.env` inteiro.

    Nao basta esvaziar a env do processo: o projeto ignora variavel vazia de
    proposito (para nao quebrar boot com env em branco), entao "" faria o valor
    do arquivo voltar a valer. E `delenv` tambem nao serve, pelo mesmo motivo.

    Desligar o `env_file` e o que torna a suite HERMETICA — que e como ela ja
    roda em CI, onde nao existe `.env`. Um teste que dependesse do arquivo ja
    estaria quebrado la.
    """
    from app.config import Settings, get_settings

    anterior = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    try:
        yield
    finally:
        Settings.model_config["env_file"] = anterior
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_real_commerce_calls(monkeypatch):
    """Rede de seguranca: nenhum teste abre conexao real com o adaptador.

    Se algum caminho escapar da neutralizacao acima, o teste falha com uma
    mensagem clara em vez de bater na Mercos de producao.
    """
    import httpx

    real_send = httpx.AsyncClient.send

    def _permitido(client, request) -> bool:
        transport = client._transport_for_url(request.url)
        return isinstance(transport, (httpx.MockTransport, httpx.ASGITransport, httpx.WSGITransport)) or (
            type(transport).__module__ == "starlette.testclient"
        )

    async def guarded(self, request, *args, **kwargs):
        host = request.url.host or ""
        if not _permitido(self, request):
            raise AssertionError(
                f"teste tentou chamada HTTP real para {host!r} — use MockTransport"
            )
        return await real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded)
    real_sync_send = httpx.Client.send

    def guarded_sync(self, request, *args, **kwargs):
        if not _permitido(self, request):
            raise AssertionError("teste tentou chamada HTTP real — use MockTransport")
        return real_sync_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", guarded_sync)
