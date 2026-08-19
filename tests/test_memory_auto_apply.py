from __future__ import annotations

from types import SimpleNamespace

from app.memory_models import (
    AgentTurnEnvelope,
    MemoryAction,
    MemoryKind,
    MemoryProposal,
    MemoryScope,
)
from app.memory_policy import evaluate_memory_proposal, is_sender_auto_apply_allowed
from app.memory_service import process_agent_memory_proposals
from app.prompt_compiler import resolve_system_instructions
from tests.memory_fakes import InMemoryMemoryStore


def _settings(**overrides):
    base = dict(
        agent_memory_proposals_enabled=True,
        agent_memory_auto_apply_enabled=True,
        agent_memory_auto_apply_sender_allowlist="whatsapp:allowed",
        agent_memory_auto_apply_min_confidence=0.85,
        agent_memory_auto_apply_min_importance=0.70,
        agent_instruction_extension_proposals_enabled=False,
        agent_conversation_summary_enabled=False,
        agent_contact_memory_in_prompt_enabled=False,
        agent_max_active_contact_memories=20,
        agent_max_conversation_summary_chars=2500,
        agent_max_contact_memory_chars=3000,
        agent_persona_tenant_id="newstore",
        agent_db_persona_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _brand(value: str, *, reason="explicit_user_preference") -> MemoryProposal:
    return MemoryProposal(
        action=MemoryAction.upsert,
        scope=MemoryScope.contact,
        kind=MemoryKind.brand_preference,
        key="preferred_brands",
        value=value,
        importance=0.9,
        confidence=0.95,
        reason_code=reason,
        use_in_instructions=True,
        safe_summary=value,
    )


def test_allowlist_empty_blocks_auto_apply(monkeypatch):
    import app.memory_policy as policy

    monkeypatch.setattr(
        policy,
        "get_settings",
        lambda: _settings(
            agent_memory_auto_apply_sender_allowlist="",
        ),
    )
    assert is_sender_auto_apply_allowed("whatsapp:1") is False
    decision = evaluate_memory_proposal(
        proposal=_brand("Tissot"),
        sender_key="whatsapp:1",
    )
    assert decision.accepted is True
    assert decision.auto_apply is False


def test_allowlist_star_allows_all(monkeypatch):
    import app.memory_policy as policy

    monkeypatch.setattr(
        policy,
        "get_settings",
        lambda: _settings(agent_memory_auto_apply_sender_allowlist="*"),
    )
    assert is_sender_auto_apply_allowed("instagram:xyz") is True


def test_explicit_brand_auto_applies_for_allowlisted_sender(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory_consolidation as consolidation
    import app.memory_policy as policy
    import app.memory_service as service

    monkeypatch.setattr(service, "get_settings", lambda: _settings())
    monkeypatch.setattr(policy, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        consolidation,
        "consolidate_contact_memories",
        lambda **_k: {"expired": 0, "pruned": 0},
    )

    result = process_agent_memory_proposals(
        envelope=AgentTurnEnvelope(reply="ok", memory_proposals=[_brand("Tissot")]),
        tenant_id="newstore",
        conversation_key="c1",
        sender_key="whatsapp:allowed",
        inbound_id=1,
    )
    assert result.proposals_applied == 1
    assert store.memories[0]["value"]["value"] == "Tissot"
    assert store.memories[0]["use_in_instructions"] is True


def test_sender_outside_allowlist_stays_pending(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory_policy as policy
    import app.memory_service as service

    monkeypatch.setattr(service, "get_settings", lambda: _settings())
    monkeypatch.setattr(policy, "get_settings", lambda: _settings())

    result = process_agent_memory_proposals(
        envelope=AgentTurnEnvelope(reply="ok", memory_proposals=[_brand("Tissot")]),
        tenant_id="newstore",
        conversation_key="c1",
        sender_key="whatsapp:other",
        inbound_id=2,
    )
    assert result.proposals_applied == 0
    assert result.proposals_pending_review == 1
    assert store.memories == []


def test_correction_supersedes_previous_brand(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory_consolidation as consolidation
    import app.memory_policy as policy
    import app.memory_service as service

    settings = _settings(agent_memory_auto_apply_sender_allowlist="*")
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(policy, "get_settings", lambda: settings)
    monkeypatch.setattr(
        consolidation,
        "consolidate_contact_memories",
        lambda **_k: {"expired": 0, "pruned": 0},
    )

    process_agent_memory_proposals(
        envelope=AgentTurnEnvelope(reply="ok", memory_proposals=[_brand("Tissot")]),
        tenant_id="newstore",
        conversation_key="c",
        sender_key="whatsapp:1",
        inbound_id=1,
    )
    process_agent_memory_proposals(
        envelope=AgentTurnEnvelope(
            reply="ok",
            memory_proposals=[
                _brand("Hamilton", reason="explicit_user_correction"),
            ],
        ),
        tenant_id="newstore",
        conversation_key="c",
        sender_key="whatsapp:1",
        inbound_id=2,
    )
    active = [m for m in store.memories if m["status"] == "active"]
    superseded = [m for m in store.memories if m["status"] == "superseded"]
    assert len(active) == 1
    assert active[0]["value"]["value"] == "Hamilton"
    assert len(superseded) == 1
    assert superseded[0]["value"]["value"] == "Tissot"


def test_explicit_no_preference_structured(monkeypatch):
    import app.memory_policy as policy

    monkeypatch.setattr(
        policy,
        "get_settings",
        lambda: _settings(agent_memory_auto_apply_sender_allowlist="*"),
    )
    decision = evaluate_memory_proposal(
        proposal=MemoryProposal(
            action=MemoryAction.upsert,
            scope=MemoryScope.contact,
            kind=MemoryKind.explicit_no_preference,
            key="explicit_no_preference_color",
            value="color",
            importance=0.9,
            confidence=0.9,
            reason_code="explicit_user_preference",
        ),
        sender_key="whatsapp:1",
    )
    assert decision.accepted is True
    assert decision.auto_apply is True
    assert decision.normalized_value == {
        "preference": "color",
        "state": "no_preference",
    }


def test_forget_removes_active_memory(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory_consolidation as consolidation
    import app.memory_policy as policy
    import app.memory_service as service

    settings = _settings(agent_memory_auto_apply_sender_allowlist="*")
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(policy, "get_settings", lambda: settings)
    monkeypatch.setattr(
        consolidation,
        "consolidate_contact_memories",
        lambda **_k: {"expired": 0, "pruned": 0},
    )

    process_agent_memory_proposals(
        envelope=AgentTurnEnvelope(reply="ok", memory_proposals=[_brand("Tissot")]),
        tenant_id="newstore",
        conversation_key="c",
        sender_key="whatsapp:1",
        inbound_id=1,
    )
    result = process_agent_memory_proposals(
        envelope=AgentTurnEnvelope(
            reply="ok",
            memory_proposals=[
                MemoryProposal(
                    action=MemoryAction.forget,
                    scope=MemoryScope.contact,
                    kind=MemoryKind.brand_preference,
                    key="preferred_brands",
                    importance=1.0,
                    confidence=1.0,
                    reason_code="explicit_user_forget_request",
                )
            ],
        ),
        tenant_id="newstore",
        conversation_key="c",
        sender_key="whatsapp:1",
        inbound_id=2,
    )
    assert result.proposals_applied == 1
    assert all(m["status"] != "active" for m in store.memories)


def test_channel_memories_are_isolated(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory_consolidation as consolidation
    import app.memory_policy as policy
    import app.memory_service as service

    settings = _settings(agent_memory_auto_apply_sender_allowlist="*")
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(policy, "get_settings", lambda: settings)
    monkeypatch.setattr(
        consolidation,
        "consolidate_contact_memories",
        lambda **_k: {"expired": 0, "pruned": 0},
    )

    process_agent_memory_proposals(
        envelope=AgentTurnEnvelope(reply="ok", memory_proposals=[_brand("Tissot")]),
        tenant_id="newstore",
        conversation_key="ig",
        sender_key="instagram:123",
        inbound_id=1,
    )
    process_agent_memory_proposals(
        envelope=AgentTurnEnvelope(reply="ok", memory_proposals=[_brand("Hamilton")]),
        tenant_id="newstore",
        conversation_key="fb",
        sender_key="facebook:123",
        inbound_id=2,
    )
    ig = [m for m in store.memories if m["sender_key"] == "instagram:123" and m["status"] == "active"]
    fb = [m for m in store.memories if m["sender_key"] == "facebook:123" and m["status"] == "active"]
    assert ig[0]["value"]["value"] == "Tissot"
    assert fb[0]["value"]["value"] == "Hamilton"


def test_prompt_injects_active_memory_when_auto_apply_enabled(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.contact_memory_repository as repo
    import app.prompt_compiler as compiler
    from app.memory_models import ContactMemory
    from app.models import IncomingMessage

    store.memories.append(
        {
            "id": 9,
            "tenant_id": "newstore",
            "sender_key": "whatsapp:allowed",
            "memory_key": "preferred_brands",
            "memory_kind": "brand_preference",
            "value": {"value": "Tissot"},
            "safe_summary": "Tissot",
            "status": "active",
            "importance": 0.9,
            "confidence": 0.9,
            "use_in_instructions": True,
            "sensitive": False,
        }
    )
    monkeypatch.setattr(
        compiler,
        "get_settings",
        lambda: _settings(
            agent_memory_auto_apply_enabled=True,
            agent_contact_memory_in_prompt_enabled=False,
        ),
    )
    monkeypatch.setattr(
        repo,
        "select_relevant_memories",
        lambda **_k: [ContactMemory.model_validate(store.memories[0])],
    )
    text = resolve_system_instructions(
        fallback_instructions="BASE_PROMPT",
        incoming=IncomingMessage(
            channel="whatsapp",
            sender_key="whatsapp:allowed",
            text="oi",
        ),
    )
    assert text.startswith("BASE_PROMPT")
    assert "preferred_brands: Tissot" in text


def test_below_threshold_does_not_auto_apply(monkeypatch):
    import app.memory_policy as policy

    monkeypatch.setattr(
        policy,
        "get_settings",
        lambda: _settings(agent_memory_auto_apply_sender_allowlist="*"),
    )
    decision = evaluate_memory_proposal(
        proposal=MemoryProposal(
            action=MemoryAction.upsert,
            scope=MemoryScope.contact,
            kind=MemoryKind.brand_preference,
            key="preferred_brands",
            value="Tissot",
            importance=0.4,
            confidence=0.5,
            reason_code="explicit_user_preference",
        ),
        sender_key="whatsapp:1",
    )
    assert decision.accepted is True
    assert decision.auto_apply is False
