from app.guardrails import detect_balance_inquiry, detect_coupon_code_inquiry
from app.repository import detect_third_party_account_inquiry, format_cents_to_brl, phones_match


def test_detect_balance_inquiry():
    assert detect_balance_inquiry("Quero saber meu saldo?") is True
    assert detect_balance_inquiry("Ola") is False


def test_detect_coupon_code_inquiry():
    assert detect_coupon_code_inquiry("Qual meu codigo do cupom?") is True


def test_format_cents_to_brl():
    assert format_cents_to_brl(15050) == "R$ 150,50"
    assert format_cents_to_brl(100000) == "R$ 1.000,00"
    assert format_cents_to_brl(None) == "R$ 0,00"


def test_phones_match():
    assert phones_match("+55 85 99949-8149", "5585999498149") is True
    assert phones_match("85999498149", "5585999498149") is True
    assert phones_match("11999999999", "85999498149") is False


def test_detect_third_party_account_inquiry():
    assert detect_third_party_account_inquiry("saldo do João", "5585999498149") is True
    assert detect_third_party_account_inquiry("quero meu saldo", "5585999498149") is False
    assert detect_third_party_account_inquiry("saldo do 11999999999", "5585999498149") is True
