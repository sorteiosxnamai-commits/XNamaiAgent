"""Cadastro e XNaMai Club atravessando o PIPELINE REAL (process_incoming_message).

Os bugs aconteciam depois do handler (roteamento antes dele e validacao factual
depois dele), entao estes cenarios passam por: roteamento -> handler ->
evolve_commerce_state -> decisao -> validacao factual -> critica -> composicao.
Estado entre turnos: o que um turno devolve em ``commerce_state`` e o que o
proximo carrega (como o banco faria).
"""

from __future__ import annotations

import pytest

from app.models import IncomingMessage

CPF = "529.982.247-25"
CPF_DIGITS = "52998224725"
EMAIL = "maria@example.com"
PHONE = "5511988887777"
ENABLED = frozenset({"create_customer", "lookup_customer_by_document"})


class Conversation:
    """Multi-turn driver: carries commerce_state between turns like the DB would."""

    def __init__(self, monkeypatch, *, capabilities=ENABLED, sender_name=None, provider_results=None):
        from app import message_pipeline

        self.state: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self.results = dict(provider_results or {})
        self.sender_name = sender_name
        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("AGENT_FACTUAL_VALIDATION_MODE", "enforce")
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setattr(message_pipeline, "load_commerce_conversation_state", lambda **_: dict(self.state))
        monkeypatch.setattr("app.capability_catalog.runtime_commerce_capabilities", lambda: capabilities)

        async def fake_execute(name, arguments):
            self.calls.append((name, dict(arguments or {})))
            return dict(self.results.get(name, {"ok": False, "error": "commerce_provider_unavailable"}))

        monkeypatch.setattr("app.commerce.tools.execute_tool", fake_execute)
        monkeypatch.setattr("app.openai_agent.execute_tool", fake_execute)
        monkeypatch.setattr("app.sales_agent.execute_tool", fake_execute)

    async def say(self, text: str):
        from app.message_pipeline import process_incoming_message

        result = await process_incoming_message(
            IncomingMessage(channel="whatsapp", provider="ycloud", text=text, sender_phone=PHONE,
                            sender_name=self.sender_name, conversation_id=f"wa:{PHONE}"),
            {},
        )
        self.state = dict(result.response_metadata.get("commerce_state") or {})
        return result

    @property
    def pending(self):
        return self.state.get("pending_action")

    @property
    def registration(self):
        return self.state.get("customer_registration") or {}

    def created(self):
        return [call for call in self.calls if call[0] == "create_customer"]


# --- Parte 1: frases que devem iniciar o cadastro --------------------------------

START_PHRASES = [
    "gostaria de me cadastrar", "quero me cadastrar", "gostaria de fazer meu cadastro", "quero fazer o cadastro",
    "preciso me cadastrar", "como faço meu cadastro?", "cadastro", "quero criar cadastro", "queria me cadastrar",
    "posso me cadastrar?",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", START_PHRASES)
async def test_registration_request_starts_the_flow_before_the_llm(monkeypatch, text):
    conversation = Conversation(monkeypatch)
    result = await conversation.say(text)
    assert conversation.pending == "awaiting_customer_registration_data", result.reply_text
    assert result.response_metadata.get("active_topic") == "customer_registration"
    assert result.safety_reason != "factual_validation_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["já tenho cadastro", "quero atualizar meu cadastro", "não quero me cadastrar",
                                  "meu cadastro está desatualizado"])
async def test_non_creation_messages_do_not_start_registration(monkeypatch, text):
    conversation = Conversation(monkeypatch)
    await conversation.say(text)
    assert conversation.pending != "awaiting_customer_registration_data"


# --- Cenarios A-G: fluxo multi-turno ----------------------------------------------


@pytest.mark.asyncio
async def test_full_registration_flow_asks_only_missing_fields_and_creates_once(monkeypatch, capsys):
    conversation = Conversation(monkeypatch, provider_results={
        "lookup_customer_by_document": {"ok": True, "found": False},
        "create_customer": {"ok": True, "customer_id": "7001"},
    })

    # A: inicia; telefone do WhatsApp ja e valido -> nao pede telefone
    started = await conversation.say("gostaria de me cadastrar")
    assert conversation.pending == "awaiting_customer_registration_data"
    asked = started.reply_text.casefold()
    assert "cpf" in asked and "e-mail" not in asked and "nome" not in asked
    assert "telefone" not in asked

    # B: dados naturais -> extraidos; permanece no cadastro; pede so o nome
    partial = await conversation.say(f"meu email é {EMAIL} e meu cpf é {CPF}")
    assert conversation.pending == "awaiting_customer_registration_data"
    draft = conversation.registration["draft"]
    assert draft["email"] == EMAIL and draft["document"] == CPF_DIGITS
    missing = partial.reply_text.casefold()
    assert "nome" in missing and "cpf" not in missing and "e-mail" not in missing

    # greeting no meio nao rouba o turno
    greeting = await conversation.say("oi")
    assert greeting.response_metadata.get("active_topic") == "customer_registration"
    assert conversation.pending == "awaiting_customer_registration_data"

    # C: correcao atualiza o draft
    await conversation.say("na verdade o e-mail é maria.souza@example.com")
    assert conversation.registration["draft"]["email"] == "maria.souza@example.com"

    # D: completo -> revisao mascarada
    review = await conversation.say("meu nome é Maria Souza")
    assert conversation.pending == "awaiting_customer_registration_confirmation"
    for secret in (CPF, CPF_DIGITS, "maria.souza@example.com", PHONE, "988887777"):
        assert secret not in review.reply_text
    assert "confirmo o cadastro" in review.reply_text

    # E: confirmacao -> exatamente uma consulta de duplicidade e UMA criacao
    done = await conversation.say("confirmo o cadastro")
    assert len(conversation.created()) == 1
    assert [name for name, _ in conversation.calls] == ["lookup_customer_by_document", "create_customer"]
    assert conversation.state.get("mercos_customer_id") == "7001"
    assert conversation.pending is None
    assert done.safety_reason != "factual_validation_failed"

    # repetir a confirmacao nao cria de novo
    await conversation.say("confirmo o cadastro")
    assert len(conversation.created()) == 1

    # nenhuma PII completa nos logs do fluxo inteiro
    logs = capsys.readouterr().out
    for secret in (CPF, CPF_DIGITS, EMAIL, "maria.souza@example.com"):
        assert secret not in logs, secret


@pytest.mark.asyncio
async def test_channel_name_is_not_used_as_legal_name(monkeypatch):
    conversation = Conversation(monkeypatch, sender_name="Maria Souza")
    started = await conversation.say("quero me cadastrar")
    assert "nome" not in started.reply_text.casefold().split("envie")[-1] or "Maria" in started.reply_text
    review = await conversation.say(f"cpf {CPF} email {EMAIL}")
    assert conversation.pending == "awaiting_customer_registration_data"
    assert "nome" in review.reply_text.casefold()
    await conversation.say("nome: Maria Aparecida Souza")
    assert conversation.registration["draft"]["legal_name"] == "Maria Aparecida Souza"


@pytest.mark.asyncio
async def test_registration_collects_before_commit_capability_is_ready(monkeypatch):
    conversation = Conversation(monkeypatch, capabilities=frozenset())
    result = await conversation.say("gostaria de me cadastrar")
    assert result.safety_reason is None
    assert "cpf" in result.reply_text.casefold()
    assert conversation.pending == "awaiting_customer_registration_data"
    assert conversation.calls == []


@pytest.mark.asyncio
async def test_create_without_duplicate_check_only_allows_collection(monkeypatch):
    conversation = Conversation(monkeypatch, capabilities=frozenset({"create_customer"}))
    result = await conversation.say("quero me cadastrar")
    assert "CPF" in result.reply_text
    assert conversation.calls == []


async def _reach_review(conversation):
    await conversation.say("quero me cadastrar")
    await conversation.say(f"nome: Maria Souza\ncpf: {CPF}\ne-mail: {EMAIL}")
    assert conversation.pending == "awaiting_customer_registration_confirmation"


@pytest.mark.asyncio
async def test_ambiguous_create_is_never_retried_and_goes_to_a_human(monkeypatch):
    """G: resultado ambiguo -> nenhum retry, handoff, estado desconhecido registrado."""
    conversation = Conversation(monkeypatch, provider_results={
        "lookup_customer_by_document": {"ok": True, "found": False},
        "create_customer": {"ok": False, "error": "commerce_provider_error", "code": "mutation_state_unknown"},
    })
    await _reach_review(conversation)
    result = await conversation.say("confirmo o cadastro")
    assert result.handoff_required is True
    assert conversation.registration["status"] == "unknown"
    assert conversation.pending is None
    await conversation.say("confirmo o cadastro")
    assert len(conversation.created()) == 1


@pytest.mark.asyncio
async def test_existing_document_links_the_customer_instead_of_duplicating(monkeypatch):
    conversation = Conversation(monkeypatch, provider_results={
        "lookup_customer_by_document": {"ok": True, "found": True, "customer_id": "555"},
        "create_customer": {"ok": True, "customer_id": "9999"},
    })
    await _reach_review(conversation)
    await conversation.say("confirmo o cadastro")
    assert conversation.created() == []
    assert conversation.state.get("mercos_customer_id") == "555"


@pytest.mark.asyncio
async def test_failed_duplicate_check_never_creates(monkeypatch):
    conversation = Conversation(monkeypatch, provider_results={
        "lookup_customer_by_document": {"ok": False, "error": "commerce_provider_unavailable"},
        "create_customer": {"ok": True, "customer_id": "9999"},
    })
    await _reach_review(conversation)
    result = await conversation.say("confirmo o cadastro")
    assert conversation.created() == []
    assert result.handoff_required is True


@pytest.mark.asyncio
async def test_customer_can_cancel_registration(monkeypatch):
    conversation = Conversation(monkeypatch)
    await conversation.say("quero me cadastrar")
    await conversation.say("cancelar cadastro")
    assert conversation.pending is None
    assert conversation.registration.get("status") == "cancelled"


@pytest.mark.asyncio
async def test_product_question_during_registration_is_not_trapped(monkeypatch):
    conversation = Conversation(monkeypatch)
    await conversation.say("quero me cadastrar")
    result = await conversation.say("quanto custa o cabo usb-c?")
    assert result.response_metadata.get("active_topic") != "customer_registration"
    assert conversation.pending == "awaiting_customer_registration_data"  # o cadastro continua aberto


# --- Cenario H: Club atravessa a validacao factual ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["quero ser membro", "o que é o club xnamai?", "já sou membro do club"])
async def test_club_reply_survives_factual_validation_in_enforce(monkeypatch, text):
    from app.site_knowledge import CLUB_URL

    conversation = Conversation(monkeypatch)
    result = await conversation.say(text)
    assert result.safety_reason != "factual_validation_failed"
    assert CLUB_URL in result.reply_text
    assert result.response_metadata.get("active_topic") == "xnamai_club"
    assert conversation.calls == []  # Club nao cria assinatura: nenhuma chamada comercial
