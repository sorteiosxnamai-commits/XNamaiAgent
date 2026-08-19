from __future__ import annotations

from app.memory_consolidation import consolidate_contact_memories


def test_consolidate_returns_counts_without_db(monkeypatch):
    import app.memory_consolidation as consolidation

    monkeypatch.setattr(consolidation, "expire_contact_memories", lambda **_k: 2)
    monkeypatch.setattr(consolidation, "prune_excess_active_memories", lambda **_k: 1)
    monkeypatch.setattr(
        consolidation,
        "get_settings",
        lambda: type("S", (), {"agent_persona_tenant_id": "newstore"})(),
    )
    result = consolidate_contact_memories(
        tenant_id="newstore",
        sender_key="whatsapp:1",
    )
    assert result["expired"] == 2
    assert result["pruned"] == 1
    assert result["tenant_id"] == "newstore"
