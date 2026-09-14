"""Isolamento de ambiente para a suite.

Por que isto existe
-------------------
`Settings` le o arquivo `.env`. Num ambiente de desenvolvimento com credenciais
comerciais reais configuradas, a fabrica de provider passava a construir um
`MercosCommerceProvider` de verdade durante os testes — e o
`monkeypatch.delenv` dos testes nao adiantava, porque o valor vinha do arquivo,
nao da env do processo.

O efeito era duplo e silencioso:

* testes que afirmam "sem configuracao -> NullCommerceProvider" falhavam na
  maquina de quem tinha credencial, e passavam em CI — o pior tipo de teste;
* a suite fazia chamada HTTP para o adaptador de PRODUCAO. Nenhum teste deve
  tocar a Mercos real, nem para leitura.

A neutralizacao abaixo e minima e explicita: so as variaveis da fronteira
comercial. As demais (YCloud, OpenAI, banco) seguem como o ambiente definir,
porque varios testes dependem do comportamento "ausente por padrao" delas.

Um teste que QUEIRA um provider comercial configurado constroi o `Settings`
explicitamente (como `tests/test_commerce_tenant_isolation.py` faz) — nunca por
vazamento do ambiente.
"""

from __future__ import annotations

import pytest

#: Variaveis que decidem se existe fonte comercial. Vazias, o provider e o Null.
_COMMERCE_ENV = (
    "MERCOS_ADAPTOR_URL",
    "MERCOS_ADAPTOR_API_KEY",
    "MERCOS_ADAPTOR_TIMEOUT_SECONDS",
    "COMMERCE_TENANT_ID",
)


#: Hosts que nao saem para a rede: TestClient do FastAPI ("test"/"testserver"),
#: loopback e os dominios reservados para exemplo/teste.
_TEST_HOSTS = frozenset({"test", "testserver", "localhost", "127.0.0.1", "::1"})


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

    def _permitido(host: str) -> bool:
        if not host:
            return True
        if host in _TEST_HOSTS or host.endswith(".example.com"):
            return True
        return host.endswith(".invalid") or host.endswith(".localhost")

    async def guarded(self, request, *args, **kwargs):
        host = request.url.host or ""
        if not _permitido(host):
            raise AssertionError(
                f"teste tentou chamada HTTP real para {host!r} — use MockTransport"
            )
        return await real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded)
