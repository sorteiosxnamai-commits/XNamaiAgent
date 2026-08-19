from __future__ import annotations

from types import SimpleNamespace

from app.memory_models import (
    AgentTurnEnvelope,
    InstructionExtensionProposal,
    MemoryAction,
    MemoryKind,
    MemoryProposal,
    MemoryScope,
)
from app.memory_service import process_agent_memory_proposals
from tests.memory_fakes import InMemoryMemoryStore


def _settings(**overrides):
    base = dict(
        agent_memory_proposals_enabled=True,
        agent_memory_auto_apply_enabled=False,
        agent_memory_auto_apply_sender_allowlist="",
        agent_memory_auto_apply_min_confidence=0.85,
        agent_memory_auto_apply_min_importance=0.70,
        agent_instruction_extension_proposals_enabled=True,
        agent_conversation_summary_enabled=False,
        agent_contact_memory_in_prompt_enabled=False,
        agent_max_active_contact_memories=20,
        agent_max_conversation_summary_chars=2500,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_proposals_persist_without_auto_apply(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory_service as service

    monkeypatch.setattr(service, "get_settings", lambda: _settings())

    envelope = AgentTurnEnvelope(
        reply="Perfeito, anotei que voce prefere Tissot.",
        memory_proposals=[
            MemoryProposal(
                action=MemoryAction.upsert,
                scope=MemoryScope.contact,
                kind=MemoryKind.brand_preference,
                key="preferred_brands",
                value="Tissot",
                importance=0.9,
                confidence=0.95,
                reason_code="explicit_user_preference",
                use_in_instructions=True,
            )
        ],
    )
    result = process_agent_memory_proposals(
        envelope=envelope,
        tenant_id="newstore",
        conversation_key="conv-1",
        sender_key="whatsapp:5511999999999",
        inbound_id=10,
    )
    assert result.proposals_persisted == 1
    assert result.proposals_applied == 0
    assert result.proposals_pending_review == 1
    assert len(store.proposals) == 1
    assert store.proposals[0]["status"] == "pending"
    assert len(store.memories) == 0


def test_disabled_flag_is_noop(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory_service as service

    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: _settings(agent_memory_proposals_enabled=False),
    )
    envelope = AgentTurnEnvelope(
        reply="ok",
        memory_proposals=[
            MemoryProposal(
                action=MemoryAction.upsert,
                scope=MemoryScope.contact,
                kind=MemoryKind.brand_preference,
                key="preferred_brands",
                value="Hamilton",
                importance=0.9,
                confidence=0.9,
                reason_code="explicit_user_preference",
            )
        ],
    )
    result = process_agent_memory_proposals(
        envelope=envelope,
        tenant_id="newstore",
        conversation_key="c",
        sender_key="whatsapp:1",
    )
    assert result.proposals_persisted == 0
    assert store.proposals == []


def test_extension_proposal_stays_pending_review(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory_policy as policy
    import app.memory_service as service

    monkeypatch.setattr(service, "get_settings", lambda: _settings())
    monkeypatch.setattr(policy, "get_settings", lambda: _settings())
    envelope = AgentTurnEnvelope(
        reply="Certo.",
        instruction_extension_proposals=[
            InstructionExtensionProposal(
                extension_key="short_when_frustrated",
                proposed_instruction="Use no maximo uma pergunta quando houver frustracao.",
                scope="tenant",
                category="tone",
                importance=0.8,
                confidence=0.8,
                evidence_summary="cliente pediu objetividade",
            )
        ],
    )
    result = process_agent_memory_proposals(
        envelope=envelope,
        tenant_id="newstore",
        conversation_key="c",
        sender_key="whatsapp:1",
        inbound_id=3,
    )
    assert result.proposals_pending_review >= 1
    assert len(store.extensions) == 1
    assert store.extensions[0]["status"] == "pending_review"


def test_auto_apply_when_enabled(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory_consolidation as consolidation
    import app.memory_policy as policy
    import app.memory_service as service

    settings = _settings(
        agent_memory_auto_apply_enabled=True,
        agent_memory_auto_apply_sender_allowlist="*",
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(policy, "get_settings", lambda: settings)
    monkeypatch.setattr(
        consolidation,
        "consolidate_contact_memories",
        lambda **_k: {"expired": 0, "pruned": 0},
    )
    envelope = AgentTurnEnvelope(
        reply="Anotei Hamilton.",
        memory_proposals=[
            MemoryProposal(
                action=MemoryAction.upsert,
                scope=MemoryScope.contact,
                kind=MemoryKind.brand_preference,
                key="preferred_brands",
                value="Hamilton",
                importance=0.9,
                confidence=0.95,
                reason_code="explicit_user_correction",
                use_in_instructions=True,
            )
        ],
    )
    result = process_agent_memory_proposals(
        envelope=envelope,
        tenant_id="newstore",
        conversation_key="c",
        sender_key="whatsapp:1",
        inbound_id=7,
    )
    assert result.proposals_applied == 1
    assert len(store.memories) == 1
    assert store.memories[0]["value"]["value"] == "Hamilton"
