"""Contrato observavel do contexto de cliente no ingress (Task L).

A primeira tentativa da Parte 1 removeu a chamada a
``find_customer_profile_by_phone`` do worker. A funcao ja era um stub
fail-closed — nao abre conexao nem consulta fonte externa — entao a remocao nao
neutralizava nada e so mudava o FORMATO do dicionario devolvido. Estes testes
fixam as duas formas do contrato e provam que nenhuma fonte e consultada.
"""

from __future__ import annotations

import asyncio

import pytest

from app.ingress.worker import _customer_context_for
from app.models import IncomingMessage


def _run(coro):
    return asyncio.run(coro)


def test_with_phone_returns_the_profile_lookup_shape():
    context = _run(_customer_context_for(IncomingMessage(sender_phone="5511999999999", text="oi")))
    assert context == {"found": False}


def test_without_phone_returns_the_channel_shape():
    message = IncomingMessage(
        text="oi",
        channel="whatsapp",
        sender_key="key-1",
        sender_name="Cliente",
    )
    context = _run(_customer_context_for(message))
    assert context == {
        "found": False,
        "channel": "whatsapp",
        "sender_key": "key-1",
        "display_name": "Cliente",
    }


def test_lookup_never_opens_a_database_connection(monkeypatch):
    """Fail-closed de verdade: nenhuma conexao e aberta em nenhum caminho."""
    import psycopg

    def explode(*args, **kwargs):
        raise AssertionError("o ingress nao pode abrir conexao de banco")

    monkeypatch.setattr(psycopg, "connect", explode)
    assert _run(_customer_context_for(IncomingMessage(sender_phone="5511999999999", text="oi")))["found"] is False
    assert _run(_customer_context_for(IncomingMessage(text="oi")))["found"] is False


@pytest.mark.parametrize("phone", ["", None, "   "])
def test_missing_phone_falls_back_to_the_channel_shape(phone):
    context = _run(_customer_context_for(IncomingMessage(sender_phone=phone, text="oi", channel="whatsapp")))
    assert context["found"] is False
