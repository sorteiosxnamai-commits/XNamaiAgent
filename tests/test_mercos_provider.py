"""MercosCommerceProvider: matriz de capacidades, dispatch e falha fechada.

Nenhum teste toca a Mercos nem o adaptador real: o transporte e simulado.
"""

from __future__ import annotations

import httpx
import pytest

from app.commerce.errors import COMMERCE_UNAVAILABLE_CODE, CommerceUnavailableError
from app.commerce.mercos.client import MercosAdaptorClient
from app.commerce.mercos.provider import (
    CAPABILITY_BY_NAME,
    CAPABILITY_MATRIX,
    LLM_EXPOSED_CAPABILITIES,
    SUPPORTED_CAPABILITIES,
    MercosCommerceProvider,
)
from app.commerce.tools import TOOL_REGISTRY


#: Tenant de teste. Explicito de proposito: o provider nao inventa tenant, e o
#: teste tambem nao deve esconder qual particao esta usando.
TENANT = "tenant-de-teste"


def _provider(handler) -> MercosCommerceProvider:
    return MercosCommerceProvider(
        MercosAdaptorClient(
            base_url="https://adaptor.example.com",
            api_key="internal-key",
            timeout_seconds=5,
            transport=httpx.MockTransport(handler),
        ),
        tenant_id=TENANT,
    )


def _ok(_):
    return httpx.Response(
        200,
        json={
            "resource": "categories",
            "count": 1,
            "pageCursor": None,
            "nextCursor": None,
            "data": [{"id": 10, "ultima_alteracao": "2026-01-01T00:00:00"}],
        },
    )


# --- matriz de capacidades --------------------------------------------------


def test_matrix_covers_every_declared_capability():
    """Toda capacidade do contrato precisa de veredito explicito.

    Silencio seria indistinguivel de esquecimento: uma capacidade sem linha na
    matriz cairia no ``UNSUPPORTED`` por acidente, e ninguem saberia por que.
    """
    declared = set(TOOL_REGISTRY["commerce"])
    classified = set(CAPABILITY_BY_NAME)
    assert not (declared - classified), f"sem veredito: {sorted(declared - classified)}"


def test_every_matrix_entry_states_a_reason():
    for item in CAPABILITY_MATRIX:
        assert item.reason.strip(), f"{item.capability} sem motivo declarado"
        assert item.strategy in {
            "SUPPORTED_DIRECTLY",
            "SUPPORTED_LOCAL",
            "SUPPORTED_COMPOSED",
            "SUPPORTED_VIA_COMPOSITION",
            "UNSUPPORTED",
        }


def test_capabilities_the_adaptor_cannot_serve_are_unsupported():
    """Carrinho, cupom, cotacao de frete e link de pagamento nao existem la."""
    for capability in (
        "create_cart",
        "get_cart",
        "get_cart_complete",
        "set_cart_item_quantity",
        "delete_cart",
        "list_coupons",
        "get_coupon",
        "quote_shipping",
        "list_shipping_methods",
        "get_order_payment",
    ):
        assert CAPABILITY_BY_NAME[capability].strategy == "UNSUPPORTED"
        assert capability not in SUPPORTED_CAPABILITIES


def test_carriers_are_never_sold_as_shipping_quotes():
    """Transportadora nao e cotacao — o erro classico deste mapeamento."""
    for capability in ("quote_shipping", "list_shipping_methods"):
        reason = CAPABILITY_BY_NAME[capability].reason.casefold()
        assert "transportadora" in reason


def test_only_the_three_reviewed_capabilities_are_exposed():
    """Primeira ativacao: so o que le do indice local com schema oficial.

    Com a documentacao oficial da Mercos, produto passou a ter mapeamento real
    (`codigo`, `nome`, `preco_tabela`, `saldo_estoque`, `ativo`, `excluido`).
    Cliente e pedido seguem fora da superficie do modelo: busca de cliente nao
    tem filtro no adaptador, e mutacao de pedido nao entra sem revisao.
    """
    assert LLM_EXPOSED_CAPABILITIES == frozenset(
        {"search_products", "get_product", "check_inventory"}
    )


def test_no_mutation_is_exposed_to_the_llm():
    """O modelo nao ganha poder de criar pedido ou cliente nesta ativacao."""
    for mutation in ("create_order", "create_customer", "update_order", "update_customer"):
        assert mutation not in LLM_EXPOSED_CAPABILITIES


def test_customer_and_order_stay_out_of_the_model_surface():
    for capability in ("search_customer", "get_customer", "list_orders", "get_order"):
        assert capability not in LLM_EXPOSED_CAPABILITIES


# --- dispatch e falha fechada -----------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_capability_fails_closed():
    provider = _provider(_ok)
    for capability in ("get_cart", "list_coupons", "quote_shipping", "create_order"):
        with pytest.raises(CommerceUnavailableError):
            await provider.execute(capability, {})


@pytest.mark.asyncio
async def test_unknown_capability_fails_closed():
    with pytest.raises(CommerceUnavailableError):
        await _provider(_ok).execute("capability_que_nao_existe", {})


@pytest.mark.asyncio
async def test_supported_capability_returns_neutral_identity_only():
    result = await _provider(_ok).execute("list_categories", {})
    assert result["ok"] is True
    assert result["categories"] == [
        {
            "external_id": "10",
            "provider": "mercos",
            "resource": "categories",
            "changed_at": "2026-01-01T00:00:00",
        }
    ]


@pytest.mark.asyncio
async def test_raw_payload_never_leaves_the_provider():
    """O payload cru serve ao indice local, nunca a resposta que sobe ao core."""

    def handler(_):
        return httpx.Response(
            200,
            json={
                "resource": "categories",
                "count": 1,
                "pageCursor": None,
                "nextCursor": None,
                "data": [{"id": 10, "campo_interno_mercos": "segredo-de-negocio"}],
            },
        )

    result = await _provider(handler).execute("list_categories", {})
    assert "segredo-de-negocio" not in str(result)


# --- modos de falha ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected_code",
    [(401, "unauthorized"), (403, "unauthorized"), (404, "not_found"),
     (400, "bad_request"), (500, "adaptor_unavailable"), (502, "adaptor_unavailable")],
)
async def test_adaptor_errors_become_structured_results(status, expected_code):
    def handler(_):
        return httpx.Response(status, json={"error": "falhou"})

    result = await _provider(handler).execute("list_categories", {})
    assert result["ok"] is False
    assert result["code"] == expected_code
    assert result["capability"] == "list_categories"


@pytest.mark.asyncio
async def test_rate_limit_surfaces_retry_after():
    def handler(_):
        return httpx.Response(
            429, json={"error": "Too Many Requests"}, headers={"Retry-After": "42"}
        )

    result = await _provider(handler).execute("list_categories", {})
    assert result["code"] == "rate_limited"
    assert result["retry_after"] == 42


@pytest.mark.asyncio
async def test_offline_adaptor_never_becomes_commercial_data():
    def handler(_):
        raise httpx.ConnectError("offline")

    result = await _provider(handler).execute("list_categories", {})
    assert result["ok"] is False
    assert result["code"] == "transport_error"
    assert "categories" not in result


@pytest.mark.asyncio
async def test_invalid_json_never_becomes_commercial_data():
    def handler(_):
        return httpx.Response(200, content=b"<html>")

    result = await _provider(handler).execute("list_categories", {})
    assert result["ok"] is False
    assert result["code"] == "invalid_response"


# --- contrato com o resto do sistema ----------------------------------------


def test_provider_declares_the_neutral_contract():
    provider = _provider(_ok)
    assert provider.name == "mercos"
    assert isinstance(provider.supported_capabilities, frozenset)
    # Sem indice nem sync concluido, nada e oferecido ao modelo — a matriz diz o
    # que o provider SABE fazer; `llm_capabilities` diz o que ele PODE fazer agora.
    assert provider.llm_capabilities == frozenset()
    assert LLM_EXPOSED_CAPABILITIES == frozenset(
        {"search_products", "get_product", "check_inventory"}
    )


def test_provider_is_unavailable_while_nothing_is_exposed():
    """Disponivel = posso dar fato ao modelo. Hoje nao posso."""
    assert _provider(_ok).available is False


def test_commerce_unavailable_code_is_the_shared_one():
    assert COMMERCE_UNAVAILABLE_CODE == "commerce_provider_unavailable"
