from app.guardrails import detect_simulation_inquiry
from app.site_knowledge import max_applicable_credit_for_product_cents
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


def test_max_applicable_for_10k_watch():
    assert max_applicable_credit_for_product_cents(1_000_000) == 100_000


def test_max_applicable_for_tissot_price():
    assert max_applicable_credit_for_product_cents(679_999) == 80_000


def test_simulate_purchase_with_110_balance_and_10k_watch():
    result = simulate_purchase(credit_cents=11000, product_cents=1_000_000)
    assert result["eligible"] is True
    assert result["applied_cents"] == 11000
    assert result["final_cents"] == 989_000
    assert result["can_apply_full_balance"] is True


def test_build_purchase_simulation_reply_for_tironi_case():
    reply = build_purchase_simulation_reply(
        credit_cents=11000,
        product_cents=1_000_000,
        display_name="Tironi",
    )
    assert "Tironi" in reply
    assert "R$ 110,00" in reply
    assert "R$ 10.000,00" in reply
    assert "R$ 9.890,00" in reply
    assert "Valor a pagar" in reply


def test_simulate_purchase_rejects_below_minimum_purchase():
    result = simulate_purchase(credit_cents=11000, product_cents=100_000)
    assert result["eligible"] is False


def test_hypothetical_10k_balance_on_10k_watch_cannot_apply_all():
    text = (
        "Se eu tiver 10 mil de saldo, eu consigo comprar um relogio de 10 mil, "
        "eu consigo abater todo o saldo nele?"
    )
    credit = resolve_simulation_credit_cents(text, account_credit_cents=11000)
    product = parse_product_price_cents(text, credit_cents=credit)
    result = simulate_purchase(credit, product or 0)

    assert credit == 1_000_000
    assert product == 1_000_000
    assert result["eligible"] is True
    assert result["max_applicable_cents"] == 100_000
    assert result["applied_cents"] == 100_000
    assert result["final_cents"] == 900_000
    assert result["can_apply_full_balance"] is False
    assert result["remaining_balance_cents"] == 900_000

    reply = build_purchase_simulation_reply(credit, product_cents=product)
    assert "Não dá para abater todo o saldo" in reply
    assert "R$ 1.000,00" in reply
    assert "R$ 9.000,00" in reply


def test_tissot_example_from_site():
    result = simulate_purchase(credit_cents=80000, product_cents=679999)
    assert result["eligible"] is True
    assert result["applied_cents"] == 80000
    assert result["final_cents"] == 599999
    assert result["can_apply_full_balance"] is True
