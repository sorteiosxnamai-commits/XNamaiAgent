"""Condicao de pagamento: lida da fonte, cacheada, e nunca escolhida no chute.

A conta real tem catorze condicoes cadastradas e UMA ativa. Seria facil pegar a
primeira da lista — e errado: a primeira e uma condicao excluida, e o pedido
sairia com prazo que a empresa nao pratica mais.

A regra e explicita nos tres casos. Uma ativa seleciona sozinha, porque nao ha
escolha a fazer. Zero bloqueia. Mais de uma pergunta ao cliente, nunca decide
por ele.

O cache existe porque o adaptador serializa chamadas e dorme dois segundos
depois de cada uma: consultar condicoes a cada mensagem tornaria a conversa
lenta sem motivo — a lista muda de mes em mes, nao de minuto em minuto.
"""

from __future__ import annotations

import pytest

from app.commerce.payment_conditions import (
    PAYMENT_CONDITION_CACHE_TTL_SECONDS,
    PAYMENT_CONDITION_REQUIRED,
    PAYMENT_CONDITION_SELECTION_REQUIRED,
    PAYMENT_CONDITION_UNAVAILABLE,
    PaymentConditionCache,
    active_payment_conditions,
    resolve_payment_condition,
)

# Formato REAL auditado na conta: 14 registros, 1 ativo.
ATIVA = {"id": 247134, "nome": "à vista por transferência ou depósito", "excluido": False}
EXCLUIDA_1 = {"id": 221254, "nome": "7 dias", "excluido": True}
EXCLUIDA_2 = {"id": 221255, "nome": "14 dias", "excluido": True}
OUTRA_ATIVA = {"id": 300000, "nome": "30 dias", "excluido": False}


def _fonte(linhas, *, falhas=0):
    """Duplo do adaptador. Conta chamadas e pode falhar sob demanda."""
    estado = {"chamadas": 0, "restantes": falhas}

    async def ler():
        estado["chamadas"] += 1
        if estado["restantes"] > 0:
            estado["restantes"] -= 1
            raise RuntimeError("adaptador indisponivel")
        return list(linhas)

    ler.estado = estado
    return ler


# === filtragem sobre o payload real =======================================


def test_only_non_excluded_conditions_count():
    """A primeira da lista e uma condicao EXCLUIDA: pegar por ordem erraria."""
    ativas = active_payment_conditions([EXCLUIDA_1, EXCLUIDA_2, ATIVA])
    assert [c["id"] for c in ativas] == [247134]


def test_an_empty_source_yields_no_active_conditions():
    assert active_payment_conditions([]) == []


def test_a_malformed_row_is_ignored_not_crashed():
    assert active_payment_conditions([None, "x", {"nome": "sem id"}, ATIVA]) == [ATIVA]


# === F/G/H. regra de selecao ==============================================


@pytest.mark.asyncio
async def test_a_single_active_condition_is_selected_on_its_own():
    resultado = await resolve_payment_condition(
        cache=PaymentConditionCache(), fetch=_fonte([EXCLUIDA_1, ATIVA])
    )
    assert resultado.status == "selected"
    assert resultado.condition_id == "247134"


@pytest.mark.asyncio
async def test_no_active_condition_blocks_the_order():
    resultado = await resolve_payment_condition(
        cache=PaymentConditionCache(), fetch=_fonte([EXCLUIDA_1, EXCLUIDA_2])
    )
    assert resultado.status == PAYMENT_CONDITION_REQUIRED
    assert resultado.condition_id is None


@pytest.mark.asyncio
async def test_several_active_conditions_ask_instead_of_choosing():
    """Escolher por conta propria fecharia o pedido com prazo que o cliente
    nao pediu — e a diferenca aparece na fatura, nao na conversa."""
    resultado = await resolve_payment_condition(
        cache=PaymentConditionCache(), fetch=_fonte([ATIVA, OUTRA_ATIVA])
    )
    assert resultado.status == PAYMENT_CONDITION_SELECTION_REQUIRED
    assert resultado.condition_id is None
    assert {c["id"] for c in resultado.options} == {247134, 300000}


@pytest.mark.asyncio
async def test_the_first_row_is_never_picked_by_position():
    resultado = await resolve_payment_condition(
        cache=PaymentConditionCache(), fetch=_fonte([EXCLUIDA_1, ATIVA, OUTRA_ATIVA])
    )
    assert resultado.condition_id != str(EXCLUIDA_1["id"])


def test_no_condition_id_is_hardcoded_in_the_module():
    """O id ativo de hoje nao pode virar constante: ele muda com o cadastro."""
    import inspect

    from app.commerce import payment_conditions

    fonte = inspect.getsource(payment_conditions)
    assert "247134" not in fonte


# === L/M/N. cache =========================================================


@pytest.mark.asyncio
async def test_the_ttl_is_thirty_minutes():
    assert PAYMENT_CONDITION_CACHE_TTL_SECONDS == 1800


@pytest.mark.asyncio
async def test_a_warm_cache_does_not_touch_the_source():
    cache = PaymentConditionCache()
    fonte = _fonte([ATIVA])
    await resolve_payment_condition(cache=cache, fetch=fonte)
    await resolve_payment_condition(cache=cache, fetch=fonte)
    assert fonte.estado["chamadas"] == 1


@pytest.mark.asyncio
async def test_an_expired_cache_is_refreshed():
    cache = PaymentConditionCache()
    fonte = _fonte([ATIVA])
    await resolve_payment_condition(cache=cache, fetch=fonte, now=0.0)
    await resolve_payment_condition(
        cache=cache, fetch=fonte, now=PAYMENT_CONDITION_CACHE_TTL_SECONDS + 1
    )
    assert fonte.estado["chamadas"] == 2


@pytest.mark.asyncio
async def test_a_failed_refresh_after_expiry_never_reuses_the_stale_value():
    """Condicao vencida nao pode sustentar um pedido: o prazo pode ter mudado."""
    cache = PaymentConditionCache()
    await resolve_payment_condition(cache=cache, fetch=_fonte([ATIVA]), now=0.0)

    resultado = await resolve_payment_condition(
        cache=cache,
        fetch=_fonte([ATIVA], falhas=1),
        now=PAYMENT_CONDITION_CACHE_TTL_SECONDS + 1,
    )
    assert resultado.status == PAYMENT_CONDITION_UNAVAILABLE
    assert resultado.condition_id is None


@pytest.mark.asyncio
async def test_a_failure_with_no_cache_at_all_is_unavailable():
    resultado = await resolve_payment_condition(
        cache=PaymentConditionCache(), fetch=_fonte([ATIVA], falhas=1)
    )
    assert resultado.status == PAYMENT_CONDITION_UNAVAILABLE


# === o cliente do adaptor sabe ler a rota ================================


@pytest.mark.asyncio
async def test_the_client_reads_the_existing_adaptor_route():
    import httpx

    from app.commerce.mercos.client import MercosAdaptorClient

    vistos: list[str] = []

    def handler(request):
        vistos.append(request.url.path)
        return httpx.Response(
            200, json={"resource": "payment-conditions", "count": 1, "data": [ATIVA]}
        )

    cliente = MercosAdaptorClient(
        base_url="https://a.example.com", api_key="k",
        transport=httpx.MockTransport(handler),
    )
    linhas = await cliente.list_payment_conditions()
    assert vistos == ["/v1/payment-conditions"]
    assert linhas == [ATIVA]
