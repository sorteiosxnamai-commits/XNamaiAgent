from datetime import datetime, timezone
from types import SimpleNamespace

from app.persona_knowledge import retrieve_knowledge


def test_retrieves_relevant_current_document_with_version_and_source():
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    documents = [
        {"id": "old", "content": "Trocas antigas", "valid_until": "2026-01-01T00:00:00Z"},
        {"id": "pending", "content": "Trocas pendentes", "status": "draft"},
        {"id": "shipping", "content": "Transportadoras disponíveis"},
        {"id": "valid", "content": "Trocas: a equipe confere o produto antes de autorizar."},
    ]
    result = retrieve_knowledge(documents, "Como funcionam as trocas?", now=now)
    assert [item["source"] for item in result] == ["valid"]
    assert len(result[0]["version"]) == 64
    assert "Trocas" in result[0]["content"]


def test_document_expiration_must_be_valid_and_timezone_aware():
    documents = [{"id": "x", "content": "Entrega", "valid_until": value}
                 for value in ("tomorrow", "2099-01-01")]
    assert retrieve_knowledge(documents, "entrega") == []


def test_knowledge_budget_and_version_change_are_deterministic():
    document = {"id": "policy", "content": "Entrega " * 10000}
    before = retrieve_knowledge([document], "Entrega")
    assert len(before) == 4
    assert sum(len(item["content"]) for item in before) <= 5600
    document["content"] += "updated"
    assert retrieve_knowledge([document], "Entrega")[0]["version"] != before[0]["version"]


def test_active_persona_documents_reach_compiled_prompt(monkeypatch):
    from app import prompt_compiler
    from app.config import get_settings
    from app.models import IncomingMessage
    settings = get_settings().model_copy(update={"agent_db_persona_enabled": True})
    monkeypatch.setattr(prompt_compiler, "get_settings", lambda: settings)
    monkeypatch.setattr(prompt_compiler, "get_active_persona", lambda *args: SimpleNamespace(
        id=7, instructions="Você atende a XNamai.", metadata={"knowledge_documents": [
            {"id": "trocas-v1", "content": "Trocas passam por conferência da equipe."}]}))
    compiled = prompt_compiler.compile_agent_prompt(incoming=IncomingMessage(text="Como funcionam as trocas?"))
    assert '"source": "trocas-v1"' in compiled.instructions
    assert "Trocas passam" in compiled.instructions
