from app.guardrails import detect_simulation_inquiry
from app.site_knowledge import CARD_USAGE_TABLE, max_applicable_credit_for_product_cents
from app.simulation import (
    build_purchase_simulation_reply,
    detect_purchase_simulation_inquiry,
    parse_product_price_cents,
    resolve_simulation_credit_cents,
    simulate_purchase,
)


def test_detect_purchase_simulation_from_balance_question():
    text = "Com o meu saldo, consigo comprar um relogio de 10 mil, quanto abateria do valor?"
    assert detect_purchase_simulation_inquiry(text)
    assert detect_simulation_inquiry(text)


def test_parse_product_price_from_mil():
    assert parse_product_price_cents("relogio de 10 mil", credit_cents=11000) == 1_000_000


def test_no_credit_table_means_no_applicable_credit():
    """Sem tabela oficial configurada, nenhum crédito é aplicável — nunca inventado.

    Substitui os dois casos que fixavam as faixas da tabela da marca anterior
    (10k -> 100k, 679.999 -> 80k). O comportamento genérico que sobrevive é:
    sem fonte oficial, o máximo aplicável é zero.
    """
    assert CARD_USAGE_TABLE == ()
    assert max_applicable_credit_for_product_cents(1_000_000) == 0
    assert max_applicable_credit_for_product_cents(679_999) == 0


def test_non_positive_product_never_accepts_credit():
    """Guarda genérica preservada de propósito: produto sem valor não abate nada."""
    assert max_applicable_credit_for_product_cents(0) == 0
    assert max_applicable_credit_for_product_cents(-1) == 0


def test_simulate_purchase_fails_explicitly_without_official_table():
    result = simulate_purchase(credit_cents=11000, product_cents=1_000_000)
    assert result["eligible"] is False
    assert "não configurada" in result["reason"]
    assert result["applied_cents"] == 0
    assert result["final_cents"] == 1_000_000, "nunca abater sem tabela oficial"
    assert result["can_apply_full_balance"] is False
    assert result["remaining_balance_cents"] == 11000


def test_build_purchase_simulation_reply_states_the_facts_it_still_has():
    reply = build_purchase_simulation_reply(
        credit_cents=11000,
        product_cents=1_000_000,
        display_name="Tironi",
    )
    assert "Tironi" in reply
    assert "R$ 110,00" in reply
    assert "R$ 10.000,00" in reply
    assert "não configurada" in reply
    assert "R$ 9.890,00" not in reply, "nenhum abatimento pode ser inventado"


def test_simulation_reply_never_points_to_a_legacy_brand_site():
    """Fronteira nova: a resposta não pode encaminhar para site de marca legada."""
    replies = [
        build_purchase_simulation_reply(credit_cents=11000),
        build_purchase_simulation_reply(credit_cents=11000, product_cents=1_000_000),
        build_purchase_simulation_reply(credit_cents=0, product_cents=0),
    ]
    for reply in replies:
        lowered = reply.casefold()
        assert "newstore" not in lowered
        assert "http" not in lowered


def test_simulate_purchase_rejects_below_minimum_purchase():
    result = simulate_purchase(credit_cents=11000, product_cents=100_000)
    assert result["eligible"] is False


def test_hypothetical_balance_parsing_is_generic_and_survives():
    """A leitura de saldo hipotético e de preço na frase é comportamento do cérebro.

    Ela não depende de tabela nem de marca: continua sendo exigida. O que mudou
    é apenas o desfecho da simulação, que agora falha de forma explícita.
    """
    text = (
        "Se eu tiver 10 mil de saldo, eu consigo comprar um relogio de 10 mil, "
        "eu consigo abater todo o saldo nele?"
    )
    credit = resolve_simulation_credit_cents(text, account_credit_cents=11000)
    product = parse_product_price_cents(text, credit_cents=credit)

    assert credit == 1_000_000
    assert product == 1_000_000

    result = simulate_purchase(credit, product or 0)
    assert result["eligible"] is False
    assert result["applied_cents"] == 0
    assert result["final_cents"] == 1_000_000

    reply = build_purchase_simulation_reply(credit, product_cents=product)
    assert "não configurada" in reply
    assert "Não dá para abater todo o saldo" not in reply
