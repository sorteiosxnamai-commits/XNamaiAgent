import re

from app.guardrails import default_safe_handoff, detect_human_support_request
from app.site_knowledge import HUMAN_SUPPORT_MESSAGE


def test_detect_human_support_request():
    assert detect_human_support_request("Quero falar com um atendente")
    assert detect_human_support_request("Quero falar com uma pessoa")
    assert detect_human_support_request("Qual o contato de vendas?")
    assert not detect_human_support_request("qual meu saldo")
