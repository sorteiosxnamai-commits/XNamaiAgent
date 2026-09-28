from types import SimpleNamespace

import pytest

from app.models import AgentResult, IncomingMessage
from app.persona_consultation import consult_active_persona


def _active(**overrides):
    values = {
        "id": 12,
        "version": 3,
        "instructions": "A XNamai aceita cadastro por CPF e por CNPJ.",
        "metadata": {
            "knowledge_documents": [
                {
                    "id": "entrega-v1",
                    "status": "approved",
                    "content": "As formas de entrega incluem transportadora e retirada.",
                }
            ]
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_consults_persona_body_before_auxiliary_source():
    result = consult_active_persona(_active(), "Posso comprar usando CPF?")

    assert result.checked is True
    assert result.active_persona_found is True
    assert result.relevant_information_found is True
    assert result.fallback_to_auxiliary is False
    assert result.persona_version_id == 12
    assert result.source_ids == ["persona:12"]


def test_consults_published_persona_knowledge_before_auxiliary_source():
    result = consult_active_persona(_active(), "Quais são as formas de entrega?")

    assert result.relevant_information_found is True
    assert result.fallback_to_auxiliary is False
    assert "entrega-v1" in result.source_ids


def test_marks_auxiliary_fallback_when_persona_has_no_relevant_information():
    result = consult_active_persona(_active(), "Vocês vendem guarda-chuva?")

    assert result.active_persona_found is True
    assert result.relevant_information_found is False
    assert result.fallback_to_auxiliary is True
    assert result.reason == "persona_has_no_relevant_information"


def test_marks_auxiliary_fallback_when_active_persona_is_missing():
    result = consult_active_persona(None, "Como funciona?")

    assert result.checked is True
    assert result.active_persona_found is False
    assert result.fallback_to_auxiliary is True
    assert result.reason == "active_persona_missing"


@pytest.mark.asyncio
async def test_pipeline_checks_persona_even_for_deterministic_reply(monkeypatch):
    from app import message_pipeline

    monkeypatch.setattr(
        message_pipeline,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="test-only",
            agent_db_persona_enabled=True,
            agent_persona_tenant_id="xnamai",
            agent_persona_key="xnamai_commercial",
            chatbo_workspace_id="aa774d20-509f-4d54-865b-7a5de22b6d30",
        ),
    )
    monkeypatch.setattr(
        "app.persona_repository.get_active_persona", lambda *args: _active()
    )

    async def deterministic_reply(*args):
        return AgentResult(
            reply_text="Sim, atendemos por CPF.",
            response_metadata={"response_source": "deterministic_fallback"},
        )

    monkeypatch.setattr(
        message_pipeline, "_process_incoming_message", deterministic_reply
    )

    result = await message_pipeline.process_incoming_message(
        IncomingMessage(
            workspace_id="aa774d20-509f-4d54-865b-7a5de22b6d30",
            text="Posso comprar com CPF?",
        ),
        {},
    )

    consultation = result.response_metadata["persona_consultation"]
    assert consultation["checked"] is True
    assert consultation["relevant_information_found"] is True
    assert consultation["fallback_to_auxiliary"] is False
    assert result.response_metadata["business_configuration"]["persona_version_id"] == 12
