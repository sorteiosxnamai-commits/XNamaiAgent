import pytest

from app.persona_policy import (
    assert_persona_instructions_safe,
    find_volatile_persona_claims,
)


def test_tone_persona_is_allowed():
    text = (
        "Você é a assistente da New Store. Tom acolhedor, respostas curtas. "
        "Nunca invente preço ou estoque."
    )
    assert find_volatile_persona_claims(text) == []
    assert_persona_instructions_safe(text)


def test_volatile_price_and_checkout_are_rejected():
    bad = "Ofereça o Seastar por R$ 1990 no link https://loja.example/checkout/1"
    assert "price_amount" in find_volatile_persona_claims(bad)
    assert "checkout_url" in find_volatile_persona_claims(bad)
    with pytest.raises(ValueError, match="persona_volatile_facts_forbidden"):
        assert_persona_instructions_safe(bad)
