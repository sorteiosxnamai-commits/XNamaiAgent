from app.guardrails import detect_last_participation_inquiry, detect_raffle_history_inquiry
from app.repository import format_payment_numbers


def test_detect_raffle_history_inquiry_last_sorteio():
    assert detect_raffle_history_inquiry("qual o ultimo sorteio que participei?") is True


def test_detect_last_participation_inquiry():
    assert detect_last_participation_inquiry("qual o ultimo sorteio que participei?") is True
    assert detect_last_participation_inquiry("meus sorteios passados") is False


def test_format_payment_numbers():
    assert format_payment_numbers(["5", "6", "7"]) == "5, 6, 7"
    assert format_payment_numbers([5, 6, 7]) == "5, 6, 7"
    assert format_payment_numbers(None) is None
