from app.context_builder import detect_customer_intents, gather_customer_facts, _primary_intent
from app.models import IncomingMessage


def test_primary_intent_prefers_simulation_over_balance():
    intents = detect_customer_intents(
        "Com o meu saldo, consigo comprar um relogio de 10 mil, quanto abateria do valor?"
    )
    assert "simulation" in intents
    assert "balance" in intents
    assert _primary_intent(intents) == "simulation"


def test_gather_customer_facts_includes_simulation_block(monkeypatch):
    def fake_account(phone, text=None):
        return {
            "found": True,
            "user_id": 1,
            "name": "Tironi Silva",
            "balance_brl": "R$ 110,00",
            "coupon_value_cents": 11000,
        }

    monkeypatch.setattr("app.context_builder.find_coupon_balance_by_phone", fake_account)
    monkeypatch.setattr(
        "app.context_builder.find_last_payment_participation",
        lambda user_id: {"found": False},
    )
    monkeypatch.setattr(
        "app.context_builder.get_user_preferences",
        lambda user_id: {"preferred_name": "Tironi"},
    )

    message = IncomingMessage(
        sender_phone="85999999999",
        text="Com o meu saldo, consigo comprar um relogio de 10 mil, quanto abateria do valor?",
    )
    facts = gather_customer_facts(message, {"found": True, "user_id": 1, "name": "Tironi Silva"})

    assert facts["primary_intent"] == "simulation"
    assert facts["simulation"]["final_brl"] == "R$ 9.890,00"
    assert facts["account"]["balance_brl"] == "R$ 110,00"
