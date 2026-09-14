from app.repository import detect_third_party_account_inquiry, format_cents_to_brl, phones_match

# Os detectores de saldo/cupom sairam com o dominio de sorteio (Parte 1).
# Sobram aqui os utilitarios genericos de telefone/moeda e a guarda de
# privacidade, que nao pertencem a sorteio algum.


def test_format_cents_to_brl():
    assert format_cents_to_brl(150000) == "R$ 1.500,00"
    assert format_cents_to_brl(0) == "R$ 0,00"
    assert format_cents_to_brl(None) == "R$ 0,00"


def test_phones_match():
    assert phones_match("5548999999999", "+55 48 99999-9999")
    assert phones_match("48999999999", "5548999999999")
    assert not phones_match("5548999999999", "5548988888888")


def test_detect_third_party_account_inquiry():
    assert detect_third_party_account_inquiry("qual o saldo do telefone 48988887777", "5548999999999")
    assert not detect_third_party_account_inquiry("qual o meu saldo", "5548999999999")
