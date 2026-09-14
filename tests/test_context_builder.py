"""Classificacao de intent e montagem de fatos apos a saida do dominio de sorteio.

Os intents ``simulation``/``balance``/``coupon_code``/``raffle_history``/
``current_raffle``/``rules`` sairam do runtime na correcao da Parte 1 (Task H),
junto com a fonte de dados que os alimentava. Sobram ``commerce``,
``human_support`` e ``general`` — e nenhum caminho pode consultar conta.
"""

from __future__ import annotations

from app.context_builder import (
    INTENT_PRIORITY,
    _primary_intent,
    detect_customer_intents,
    gather_customer_facts,
)
from app.models import IncomingMessage


def test_surviving_intents_are_only_the_generic_ones():
    assert INTENT_PRIORITY == ("human_support", "commerce", "general")


def test_commerce_questions_are_classified_as_commerce():
    for text in (
        "Vocês têm Tissot Seastar?",
        "Tem estoque desse relógio?",
        "Quanto custa?",
        "Quanto fica no Pix?",
    ):
        assert _primary_intent(detect_customer_intents(text)) == "commerce", text


def test_raffle_domain_questions_no_longer_get_a_domain_intent():
    """Sem feature de sorteio, essas perguntas caem no generico."""
    for text in ("qual meu saldo?", "qual o sorteio aberto?", "como funciona o sorteio?"):
        intents = detect_customer_intents(text)
        assert "balance" not in intents
        assert "current_raffle" not in intents
        assert "rules" not in intents
        assert "simulation" not in intents


def test_human_support_request_is_preserved():
    assert "human_support" in detect_customer_intents("quero falar com um atendente")


def test_facts_never_carry_an_account_lookup():
    facts = gather_customer_facts(
        IncomingMessage(sender_phone="5511999999999", text="Vocês têm Tissot Seastar?"),
        {"found": True, "name": "Cliente"},
    )
    assert facts["primary_intent"] == "commerce"
    assert facts["account"] == {"found": False}
    assert facts["display_name"] == "Cliente"


def test_facts_carry_no_raffle_domain_blocks():
    facts = gather_customer_facts(
        IncomingMessage(sender_phone="5511999999999", text="qual meu saldo?"),
        {},
    )
    for dead in ("simulation", "coupon", "raffle_history", "open_draw", "last_participation"):
        assert dead not in facts, f"fato do dominio de sorteio ainda montado: {dead}"


def test_facts_never_open_a_database_connection(monkeypatch):
    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("montar fatos nao pode abrir conexao")))
    facts = gather_customer_facts(IncomingMessage(sender_phone="5511999999999", text="oi"), {})
    assert facts["account"] == {"found": False}
