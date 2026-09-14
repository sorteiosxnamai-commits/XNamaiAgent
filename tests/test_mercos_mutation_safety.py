"""Mutacao ambigua nunca vira "nao aconteceu".

Um timeout DEPOIS do envio de um `POST /v1/orders` e o caso perigoso: o pedido
pode ter sido criado no ERP do cliente. Tratar isso como falha simples convida
ao reenvio — e a pedido duplicado. Por isso a falha ambigua carrega
``mutation_state="unknown"`` e nunca e repetida automaticamente.
"""

from __future__ import annotations

import httpx
import pytest

from app.commerce.mercos.client import MercosAdaptorClient, MercosAdaptorError


def _client(handler, **kwargs):
    return MercosAdaptorClient(
        base_url="https://adaptor.example.com",
        api_key="internal-key",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


MUTATIONS = [
    ("create_order", ({"cliente_id": 1},)),
    ("create_customer", ({"nome": "x"},)),
    ("update_order", ("42", {"x": 1})),
    ("update_customer", ("42", {"x": 1})),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", MUTATIONS, ids=[m[0] for m in MUTATIONS])
async def test_timeout_after_send_is_reported_as_unknown_not_failed(method, args):
    def handler(_):
        raise httpx.ReadTimeout("resposta nao chegou")

    with pytest.raises(MercosAdaptorError) as exc:
        await getattr(_client(handler), method)(*args)

    assert exc.value.code == "mutation_state_unknown"
    assert exc.value.details["mutation_state"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_gateway_errors_on_mutation_are_ambiguous(status):
    """502/503/504 podem ter chegado ao destino: tratar como indeterminado."""

    def handler(_):
        return httpx.Response(status, json={"error": "gateway"})

    with pytest.raises(MercosAdaptorError) as exc:
        await _client(handler).create_order({"cliente_id": 1})
    assert exc.value.code == "mutation_state_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_on_mutation_are_not_ambiguous(status):
    """4xx significa que a Mercos recusou: nao foi criado, e nao e 'unknown'."""

    def handler(_):
        return httpx.Response(status, json={"error": "recusado"})

    with pytest.raises(MercosAdaptorError) as exc:
        await _client(handler).create_order({"cliente_id": 1})
    assert exc.value.code != "mutation_state_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", MUTATIONS, ids=[m[0] for m in MUTATIONS])
async def test_no_mutation_is_ever_sent_twice(method, args):
    calls = {"n": 0}

    def handler(_):
        calls["n"] += 1
        raise httpx.ReadTimeout("timeout")

    with pytest.raises(MercosAdaptorError):
        await getattr(_client(handler, max_transport_retries=5), method)(*args)
    assert calls["n"] == 1, "mutacao nao pode ser reenviada automaticamente"


@pytest.mark.asyncio
async def test_reads_still_retry_transport_failures():
    """A regra de nao repetir vale para mutacao — leitura pode tentar de novo."""
    calls = {"n": 0}

    def handler(_):
        calls["n"] += 1
        raise httpx.ConnectError("sem rede")

    with pytest.raises(MercosAdaptorError):
        await _client(handler, max_transport_retries=3).list_resource("products")
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_ambiguous_error_never_leaks_the_payload():
    """O payload de pedido carrega dado de cliente: nao pode ir para o erro."""

    def handler(_):
        raise httpx.ReadTimeout("timeout")

    with pytest.raises(MercosAdaptorError) as exc:
        await _client(handler).create_customer(
            {"nome": "Fulano de Tal", "cpf": "000.000.000-00", "email": "a@b.c"}
        )
    rendered = f"{exc.value} {exc.value.details}"
    for leak in ("Fulano", "000.000.000-00", "a@b.c"):
        assert leak not in rendered
