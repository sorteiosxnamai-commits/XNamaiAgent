"""O provider tem uma fronteira, e ninguem de fora atravessa por baixo dela.

`_client`, `_index` e `_sync_state` sao como o `MercosCommerceProvider` fala com
a fonte comercial. Quem consome o provider deve enxergar apenas capacidades —
`search_products`, `check_inventory`, e agora a leitura de condicoes de pagamento
— porque e isso que sobrevive a uma troca de fornecedor.

Este teste existe por um motivo concreto: o fluxo de revisao chegava ao
adaptador via ``getattr(provider, "_client", None)``. Funcionava, e era pior do
que o acesso direto — a forma com string escapava de qualquer busca por
``._client``. Por isso a checagem abaixo procura as DUAS formas.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Atributos internos do provider comercial.
PRIVADOS = ("_client", "_index", "_sync_state")

#: Quem pode toca-los.
#: * o proprio provider, que e o dono;
#: * `test_commerce_tenant_isolation`, que verifica justamente a fiacao interna
#:   do provider — checar isolamento de tenant exige olhar como ele foi montado.
PERMITIDOS = {
    "app/commerce/mercos/provider.py",
    "tests/test_commerce_tenant_isolation.py",
    "tests/test_provider_boundary.py",
}


def _arquivos_python():
    for base in ("app", "tests"):
        for caminho in sorted((REPO_ROOT / base).rglob("*.py")):
            if "__pycache__" in caminho.parts:
                continue
            relativo = caminho.relative_to(REPO_ROOT).as_posix()
            if relativo in PERMITIDOS:
                continue
            yield relativo, caminho


def _acessos_privados(caminho: pathlib.Path) -> list[str]:
    """Acesso direto (`x._client`) e indireto (`getattr(x, "_client")`)."""
    try:
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    achados: list[str] = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Attribute) and no.attr in PRIVADOS:
            # `self._client` dentro de uma classe que nao seja o provider ainda
            # e atributo proprio dela; so interessa quando o alvo e outro objeto.
            alvo = no.value
            if isinstance(alvo, ast.Name) and alvo.id == "self":
                continue
            achados.append(f"{no.attr} (acesso direto, linha {no.lineno})")
        if isinstance(no, ast.Call) and isinstance(no.func, ast.Name):
            if no.func.id == "getattr" and len(no.args) >= 2:
                segundo = no.args[1]
                if isinstance(segundo, ast.Constant) and segundo.value in PRIVADOS:
                    achados.append(
                        f"{segundo.value} (via getattr, linha {no.lineno})"
                    )
    return achados


def test_no_module_reaches_into_the_provider_internals():
    ofensores: dict[str, list[str]] = {}
    for relativo, caminho in _arquivos_python():
        achados = _acessos_privados(caminho)
        if achados:
            ofensores[relativo] = achados
    assert not ofensores, f"fronteira do provider atravessada: {ofensores}"


def test_the_payment_flow_uses_a_public_capability():
    """A leitura de condicoes passa por API publica do provider."""
    import inspect

    from app.commerce import turn_flow

    fonte = inspect.getsource(turn_flow)
    assert '"_client"' not in fonte
    assert "list_payment_conditions" in fonte


def test_the_provider_publishes_the_reader():
    from app.commerce.mercos.provider import MercosCommerceProvider

    assert callable(getattr(MercosCommerceProvider, "list_payment_conditions", None))


# === a superficie do modelo nao cresceu ==================================


def test_no_new_tool_is_offered_to_the_model():
    """Condicao de pagamento e leitura DETERMINISTICA do fluxo, nunca uma tool.

    Toda tool nova e uma decisao que o modelo passa a poder tomar sozinho —
    aqui a escolha da condicao tem regra propria (uma ativa seleciona; varias
    perguntam) e nao deve virar improviso.
    """
    from datetime import datetime, timezone

    import app.commerce.provider as modulo_provider
    import app.commerce.tools as tools
    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.provider import MercosCommerceProvider
    from app.commerce.mercos.sync_state import SyncState

    class _Sincronizado:
        def read(self, provider, resource):
            return SyncState(provider, resource, last_success_at=datetime.now(timezone.utc))

    provider = MercosCommerceProvider(
        MercosAdaptorClient(base_url="https://a.example.com", api_key="k"),
        index=object(), tenant_id="xnamai", sync_state=_Sincronizado(),
    )
    anterior = modulo_provider.get_commerce_provider
    tools_anterior = tools.get_commerce_provider
    modulo_provider.get_commerce_provider = lambda: provider
    tools.get_commerce_provider = lambda: provider
    try:
        nomes = [s["function"]["name"] for s in (tools.tool_schemas_for_model() or [])]
    finally:
        modulo_provider.get_commerce_provider = anterior
        tools.get_commerce_provider = tools_anterior

    assert nomes == ["search_products", "get_product", "check_inventory"]
    assert "list_payment_conditions" not in nomes


# === o comportamento das condicoes nao mudou ==============================


@pytest.mark.asyncio
async def test_the_provider_reader_delegates_to_the_adaptor_route():
    import httpx

    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.provider import MercosCommerceProvider

    vistos: list[str] = []

    def handler(request):
        vistos.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "resource": "payment-conditions", "count": 1,
                "data": [{"id": 1, "nome": "à vista", "excluido": False}],
            },
        )

    provider = MercosCommerceProvider(
        MercosAdaptorClient(
            base_url="https://a.example.com", api_key="k",
            transport=httpx.MockTransport(handler),
        ),
        index=object(), tenant_id="xnamai", sync_state=None,
    )
    linhas = await provider.list_payment_conditions()
    assert vistos == ["/v1/payment-conditions"]
    assert [l["id"] for l in linhas] == [1]


@pytest.mark.asyncio
async def test_a_provider_without_a_source_returns_nothing_instead_of_raising():
    """Sem fonte comercial a leitura devolve vazio; a revisao entao bloqueia por
    'nenhuma condicao', que e um estado tratado — nao uma excecao no turno."""
    from app.commerce.provider import NullCommerceProvider

    leitor = getattr(NullCommerceProvider(), "list_payment_conditions", None)
    if leitor is None:
        pytest.skip("provider nulo nao publica a leitura")
    assert await leitor() == []
