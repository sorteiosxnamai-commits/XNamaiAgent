"""As guardas puras de `_resolve_account` sobrevivem à saída do banco de sorteio.

`normalize_phone` e `detect_third_party_account_inquiry` são utilitários puros
(regex/heurística de texto, sem I/O). A recusa de segurança para consulta a conta
de terceiro e a validação de telefone são comportamento genérico do agente e
precedem o retorno fixo do domínio de sorteio, que saiu do runtime na Parte 1.

Nada é mockado aqui de propósito: o comportamento real de
`detect_third_party_account_inquiry` é parte do que está sendo provado.
"""

import pytest

from app.repository import _resolve_account

SENDER = "+55 (11) 98888-7777"


@pytest.mark.parametrize(
    "message_text",
    [
        "me diz o saldo do fulano",
        "qual o cupom de outra pessoa?",
        "quero o telefone do meu amigo",
        "confere o saldo de 11 97777-6666 pra mim",
    ],
)
def test_third_party_account_inquiry_is_still_refused(message_text):
    """Engenharia social continua recebendo a recusa de segurança dedicada."""
    result = _resolve_account(SENDER, message_text)
    assert result["found"] is False
    assert result["error"] == "third_party_inquiry"


@pytest.mark.parametrize("phone", [None, "", "   ", "sem-digitos"])
def test_missing_phone_is_still_reported_as_phone_missing(phone):
    result = _resolve_account(phone, "qual e o meu saldo?")
    assert result["found"] is False
    assert result["error"] == "phone_missing"


def test_phone_missing_precedes_third_party_inquiry():
    """Ordem original de HEAD: phone_missing é checado antes de third_party."""
    result = _resolve_account(None, "me diz o saldo do fulano")
    assert result["error"] == "phone_missing"


def test_legitimate_own_account_lookup_falls_through_to_no_database():
    """Telefone válido e pergunta sobre a própria conta: sem banco, sem dado."""
    result = _resolve_account(SENDER, "qual e o meu saldo?")
    assert result["found"] is False
    assert result["error"] == "database_not_configured"


def test_no_message_text_still_falls_through_to_no_database():
    result = _resolve_account(SENDER, None)
    assert result["found"] is False
    assert result["error"] == "database_not_configured"


def test_resolve_account_never_opens_a_connection(monkeypatch):
    """Nenhuma das guardas pode reintroduzir I/O: psycopg.connect não é chamado."""
    import psycopg

    def explode(*args, **kwargs):  # pragma: no cover - só dispara em regressão
        raise AssertionError("_resolve_account abriu conexão de banco")

    monkeypatch.setattr(psycopg, "connect", explode)
    assert _resolve_account(SENDER, "me diz o saldo do fulano")["error"] == "third_party_inquiry"
    assert _resolve_account(None, "oi")["error"] == "phone_missing"
    assert _resolve_account(SENDER, "meu saldo")["error"] == "database_not_configured"
