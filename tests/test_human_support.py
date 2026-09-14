# NOTA (correcao da Parte 1): os testes que afirmavam "nao configurado" para
# URLs, contatos, tabela de credito e registro VIP foram removidos. Aquele
# comportamento era uma alteracao NAO AUTORIZADA de persona, revertida para o
# baseline 201bd16. O rebranding e tarefa separada.

import re

from app.guardrails import default_safe_handoff, detect_human_support_request
from app.site_knowledge import HUMAN_SUPPORT_MESSAGE, NS_SALES_WHATSAPP


def test_detect_human_support_request():
    assert detect_human_support_request("Quero falar com um atendente")
    assert detect_human_support_request("Qual o contato de vendas?")
    assert not detect_human_support_request("qual meu saldo")
