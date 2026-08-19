from __future__ import annotations

from app.instruction_extension_repository import format_approved_extensions_block
from tests.memory_fakes import InMemoryMemoryStore


def test_format_approved_extensions_block():
    block = format_approved_extensions_block(
        [
            {
                "extension_key": "short_when_frustrated",
                "instruction_text": "No maximo uma pergunta quando houver frustracao.",
            }
        ]
    )
    assert "<approved_instruction_extensions>" in block
    assert "short_when_frustrated" in block


def test_create_extension_stays_pending(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.instruction_extension_repository as repo

    created = repo.create_extension_proposal(
        tenant_id="newstore",
        extension_key="tone_short",
        instruction_text="Seja breve.",
        category="tone",
        scope="tenant",
    )
    assert created["status"] == "pending_review"
    assert len(store.extensions) == 1
