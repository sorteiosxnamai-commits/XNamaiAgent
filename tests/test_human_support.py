import re

from app.guardrails import default_safe_handoff, detect_human_support_request
from app.site_knowledge import HUMAN_SUPPORT_MESSAGE, NS_SALES_WHATSAPP


def test_human_support_message_declares_no_channel_configured():
    """Sem canal humano oficial configurado, a mensagem diz isso — não inventa contato."""
    assert NS_SALES_WHATSAPP == ""
    lowered = HUMAN_SUPPORT_MESSAGE.lower()
    assert "não configurado" in lowered
    assert not re.search(r"\d{4,}", HUMAN_SUPPORT_MESSAGE), "nenhum telefone pode aparecer"
    assert "http" not in lowered


def test_default_safe_handoff_invents_no_contact():
    reply = default_safe_handoff()
    assert not re.search(r"\d{4,}", reply), "nenhum telefone pode aparecer"
    assert "http" not in reply.lower()


def test_detect_human_support_request():
    assert detect_human_support_request("Quero falar com um atendente")
    assert detect_human_support_request("Qual o contato de vendas?")
    assert not detect_human_support_request("qual meu saldo")
