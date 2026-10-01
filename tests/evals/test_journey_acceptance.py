"""Acceptance criteria detect the actual regressions, independent of wording."""
from scripts.evaluate_conversation_journeys import evaluate_turn, run


def test_relevant_sounding_reply_cannot_hide_loss_of_power_constraint():
    response = {"reply_text":"Separei as opções: 120W\n1. Caixa de som Bluetooth 40W\n2. Caixa de som Bluetooth 120W"}
    assert "lost_product_filter" in evaluate_turn({"product_filter":"caixa de som 120w bluetooth"},response)


def test_advice_cannot_silently_search_catalog_or_send_fallback():
    response={"reply_text":"Não consegui validar.","safety_reason":"factual_validation_failed",
              "evaluation":{"used_commerce_provider":True}}
    errors=evaluate_turn({"no_commerce_lookup":True},response)
    assert "unnecessary_commerce_lookup" in errors
    assert "fallback:factual_validation_failed" in errors


def test_journeys_carry_actual_replies_to_next_turn_and_isolate_identities():
    calls=[]
    def send(body):
        calls.append(body)
        return {"reply_text":"Resposta " + body["text"]}
    rows=run("unused","unused",[{"id":"a","turns":[{"text":"primeira"},{"text":"segunda"}]},
                                 {"id":"b","turns":[{"text":"outra"}]}],send=send)
    assert calls[1]["history"][-1]["content"]=="Resposta primeira"
    assert calls[2]["history"]==[]
    assert calls[0]["phone"]!=calls[2]["phone"]
    assert all(not row["failures"] for row in rows)
