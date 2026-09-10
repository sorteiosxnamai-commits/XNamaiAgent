"""Revisão final — consumidores das constantes esvaziadas de `site_knowledge`.

`SITE_URL`, `STORE_URL` e `NS_SALES_WHATSAPP` ficaram vazias na Task 10. Toda
resposta construída por `agent_replies` / `guardrails` / `handoff_service` tem de
continuar BEM FORMADA: nada de interpolação pendurada (`" em ."`, `": ."`, espaço
duplo, frase terminando em preposição solta) e, sobretudo, nada de afirmar fato
que não pode ser confirmado — em especial "existe uma rodada aberta" quando a
consulta devolve `database_not_configured`.

Mesma regra já aplicada pelo Gate 6 em `app/simulation.py`: constante vazia →
declarar "não configurado" e OMITIR a frase inteira.
"""

from __future__ import annotations

import re

import pytest

from app import agent_replies, guardrails, handoff_service, site_knowledge
from app.models import IncomingMessage

# --------------------------------------------------------------------------
# Boa-formação
# --------------------------------------------------------------------------

_MALFORMED = (
    (re.compile(r"\s[.,;:!?]"), "espaço antes de pontuação (interpolação vazia)"),
    (re.compile(r"[ \t]{2,}"), "espaço duplo (interpolação vazia)"),
    (re.compile(r"[(\[]\s*[)\]]"), "parênteses/colchetes vazios"),
    (re.compile(r"\*\s*\*"), "marcação de negrito vazia"),
    (re.compile(r"(?<![\d.])\.\s*\."), "pontuação duplicada"),
)

#: Conector/preposição solta imediatamente antes de pontuação — sobra clássica de
#: `f"... em {SITE_URL}."` com a constante vazia.
_DANGLING_CONNECTOR = re.compile(
    r"\b(em|no|na|nos|nas|para|pelo|pela|ao|aos|à|às|de|do|da|dos|das|com|ou|e|até"
    r"|via|acesse|acessar|consulte|site|whatsapp|link|url)\s*[.,;:!?]",
    re.IGNORECASE,
)

_END_CONNECTOR = re.compile(
    r"\b(em|no|na|para|de|do|da|com|ou|e|via|acesse|acessar|consulte|whatsapp|site)$",
    re.IGNORECASE,
)


def assert_well_formed(text: str, label: str) -> None:
    assert isinstance(text, str), f"{label}: resposta não é texto"
    assert text.strip(), f"{label}: resposta vazia"
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        for pattern, why in _MALFORMED:
            assert not pattern.search(stripped), f"{label}: {why} → {stripped!r}"
        match = _DANGLING_CONNECTOR.search(stripped)
        assert not match, f"{label}: conector solto {match.group(0)!r} → {stripped!r}"
        assert not _END_CONNECTOR.search(stripped.rstrip(".!? ")), (
            f"{label}: frase termina em conector solto → {stripped!r}"
        )


def _msg(text: str = "oi", phone: str = "+5521999990000") -> IncomingMessage:
    return IncomingMessage(channel="whatsapp", text=text, sender_phone=phone)


@pytest.fixture(autouse=True)
def _no_db_preferences(monkeypatch):
    monkeypatch.setattr(agent_replies, "get_user_preferences", lambda user_id: {})
    monkeypatch.setattr(agent_replies, "mark_preferred_name_prompted", lambda user_id: None)


def test_constants_are_really_empty():
    """Pré-condição do arquivo inteiro: sem isso os testes abaixo passam a vácuo."""
    assert site_knowledge.SITE_URL == ""
    assert site_knowledge.STORE_URL == ""
    assert site_knowledge.NS_SALES_WHATSAPP == ""
    assert agent_replies.SITE_URL == ""
    assert agent_replies.STORE_URL == ""
    assert agent_replies.NS_SALES_WHATSAPP == ""


def test_well_formed_checker_catches_the_known_broken_phrases():
    """Auto-teste: a guarda não pode passar a vácuo."""
    broken = (
        "Consulta de sorteio indisponível no momento. Acompanhe a rodada aberta em .",
        "Você também pode acessar sua conta em .",
        "Use em  no checkout.",
        "Participe em .",
        "fale com a equipe no WhatsApp .",
    )
    for sample in broken:
        with pytest.raises(AssertionError):
            assert_well_formed(sample, "amostra")

    assert_well_formed("Consulta de sorteio não configurada.", "amostra sã")
    assert_well_formed("• Valor a pagar: R$ 6.799,99", "amostra sã")


# --------------------------------------------------------------------------
# Caminhos PADRÃO (nenhum patch): é o que o runtime produz hoje
# --------------------------------------------------------------------------

_DEFAULT_BUILDERS = (
    ("build_balance_reply", "qual meu saldo?"),
    ("build_coupon_code_reply", "qual meu código?"),
    ("build_simulation_reply", "quanto abate de um relógio de 10 mil?"),
    ("build_simulation_reply", "consigo usar meu saldo?"),
    ("build_current_raffle_reply", "qual o sorteio aberto?"),
    ("build_available_numbers_reply", "quais números disponíveis?"),
    ("build_raffle_history_reply", "quais meus sorteios?"),
    ("build_rules_reply_result", "como funciona o sorteio?"),
)


@pytest.mark.parametrize("builder_name,text", _DEFAULT_BUILDERS)
def test_default_replies_are_well_formed(builder_name, text):
    builder = getattr(agent_replies, builder_name)
    result = builder(_msg(text))
    assert_well_formed(result.reply_text, builder_name)


def test_current_raffle_never_claims_an_open_round_without_a_source():
    """CRITICAL: sem fonte, não se afirma que existe rodada aberta."""
    result = agent_replies.build_current_raffle_reply(_msg("tem sorteio aberto?"))
    lowered = result.reply_text.lower()
    assert result.safety_reason == "database_not_configured"
    assert "configurad" in lowered, result.reply_text
    for claim in ("acompanhe a rodada aberta", "rodada aberta em", "sorteio aberto:"):
        assert claim not in lowered, f"afirma fato não confirmado: {result.reply_text!r}"
    assert_well_formed(result.reply_text, "build_current_raffle_reply")


def test_available_numbers_reply_declares_not_configured():
    result = agent_replies.build_available_numbers_reply(_msg("quais números?"))
    assert result.safety_reason == "database_not_configured"
    assert "configurad" in result.reply_text.lower()
    assert_well_formed(result.reply_text, "build_available_numbers_reply")


def test_guardrails_default_safe_handoff_is_well_formed():
    reply = guardrails.default_safe_handoff()
    assert_well_formed(reply, "default_safe_handoff")
    assert "configurad" in reply.lower()
    assert not re.search(r"\d{4,}", reply)
    assert "http" not in reply.lower()


@pytest.mark.parametrize(
    "reason",
    ("customer_requested_human", "trade_in_or_appraisal", "blocked_topic:aposta"),
)
def test_handoff_result_text_is_well_formed(reason):
    result = handoff_service.build_human_handoff_result(reason=reason)
    assert result.handoff_required is True
    assert_well_formed(result.reply_text, f"handoff:{reason}")


def test_handoff_metadata_never_publishes_an_empty_contact():
    """Metadado é consumido pelo provider: contato vazio é pior que ausente."""
    result = handoff_service.build_human_handoff_result(reason="customer_requested_human")
    handoff = result.response_metadata["handoff"]
    assert handoff.get("contact_whatsapp") in (None, ...) or handoff.get("contact_whatsapp"), (
        f"contact_whatsapp vazio nos metadados: {handoff!r}"
    )
    assert handoff.get("contact_whatsapp") != ""

    enriched = handoff_service.enrich_handoff_metadata(
        _msg("quero falar com um atendente"),
        type(result)(reply_text="x", intent="general", handoff_required=False),
    )
    assert enriched.response_metadata["handoff"].get("contact_whatsapp") != ""

    payload = handoff_service.handoff_provider_payload(result)
    assert payload is not None
    assert payload.get("contact_whatsapp") != ""


# --------------------------------------------------------------------------
# Caminhos com DADOS (patch do repositório): a Parte 2 volta a alcançá-los
# --------------------------------------------------------------------------


def test_coupon_code_reply_with_data_is_well_formed(monkeypatch):
    monkeypatch.setattr(
        agent_replies,
        "find_user_coupon_code",
        lambda phone, text=None: {
            "found": True,
            "user_id": 7,
            "coupon_code": "XPTO123",
            "balance_brl": "R$ 500,00",
        },
    )
    result = agent_replies.build_coupon_code_reply(_msg("qual meu código?"))
    assert "XPTO123" in result.reply_text
    assert_well_formed(result.reply_text, "build_coupon_code_reply/found")


def test_current_raffle_reply_with_data_is_well_formed(monkeypatch):
    monkeypatch.setattr(
        agent_replies,
        "find_current_raffle",
        lambda: {
            "found": True,
            "title": "Rodada 10",
            "prize_name": None,
            "status": "open",
            "quota_price_brl": "R$ 100,00",
            "available_numbers": [f"{n:03d}" for n in range(200)],
        },
    )
    result = agent_replies.build_current_raffle_reply(_msg("qual o sorteio?"))
    assert_well_formed(result.reply_text, "build_current_raffle_reply/found")


def test_current_raffle_lookup_error_is_well_formed(monkeypatch):
    monkeypatch.setattr(
        agent_replies, "find_current_raffle", lambda: {"found": False, "lookup_error": "boom"}
    )
    result = agent_replies.build_current_raffle_reply(_msg("qual o sorteio?"))
    assert result.safety_reason == "current_raffle_lookup_failed"
    assert_well_formed(result.reply_text, "build_current_raffle_reply/lookup_error")


def test_current_raffle_not_found_is_well_formed(monkeypatch):
    monkeypatch.setattr(
        agent_replies, "find_current_raffle", lambda: {"found": False, "error": "no_open_draw"}
    )
    result = agent_replies.build_current_raffle_reply(_msg("qual o sorteio?"))
    assert_well_formed(result.reply_text, "build_current_raffle_reply/not_found")


@pytest.mark.parametrize(
    "payload",
    (
        {"found": False, "lookup_error": "boom"},
        {"found": False, "error": "no_open_draw"},
        {"found": True, "title": "Rodada 10", "available_numbers": [], "total_count": None},
        {"found": True, "title": "Rodada 10", "available_numbers": [], "total_count": 100},
        {
            "found": True,
            "title": "Rodada 10",
            "prize_name": "Relógio",
            "price_brl": "R$ 100,00",
            "available_numbers": [f"{n:03d}" for n in range(300)],
            "total_count": 300,
        },
    ),
)
def test_available_numbers_branches_are_well_formed(monkeypatch, payload):
    monkeypatch.setattr(
        agent_replies, "find_available_numbers_for_open_draw", lambda: dict(payload)
    )
    result = agent_replies.build_available_numbers_reply(_msg("quais números?"))
    assert_well_formed(result.reply_text, "build_available_numbers_reply")


def test_raffle_history_without_participation_is_well_formed(monkeypatch):
    monkeypatch.setattr(
        agent_replies,
        "find_coupon_balance_by_phone",
        lambda phone, text=None: {"found": True, "user_id": 7, "balance_brl": "R$ 1,00"},
    )
    result = agent_replies.build_raffle_history_reply(_msg("quais meus sorteios?"))
    assert_well_formed(result.reply_text, "build_raffle_history_reply/empty")


def test_simulation_replies_without_account_are_well_formed(monkeypatch):
    for text in ("quanto abate?", "consigo comprar um relógio de R$ 6.799,99?"):
        result = agent_replies.build_simulation_reply(_msg(text))
        assert_well_formed(result.reply_text, f"build_simulation_reply({text!r})")
