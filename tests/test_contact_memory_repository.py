from __future__ import annotations

from app.contact_memory_repository import (
    format_customer_memory_block,
    select_relevant_memories,
)
from app.memory_models import ContactMemory
from tests.memory_fakes import InMemoryMemoryStore


def test_format_customer_memory_block_is_factual():
    block = format_customer_memory_block(
        [
            ContactMemory(
                id=1,
                tenant_id="newstore",
                sender_key="whatsapp:1",
                memory_key="preferred_brands",
                memory_kind="brand_preference",
                value={"value": "Tissot"},
                safe_summary="Tissot",
                use_in_instructions=True,
            )
        ]
    )
    assert "preferred_brands: Tissot" in block
    assert "sempre" not in block.lower()


def test_select_relevant_memories_prefers_commerce_kinds(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.contact_memory_repository as repo

    store.memories.extend(
        [
            {
                "id": 1,
                "tenant_id": "newstore",
                "sender_key": "whatsapp:1",
                "memory_key": "preferred_brands",
                "memory_kind": "brand_preference",
                "value": {"value": "Tissot"},
                "safe_summary": "Tissot",
                "status": "active",
                "importance": 0.9,
                "confidence": 0.9,
                "use_in_instructions": True,
                "sensitive": False,
            },
            {
                "id": 2,
                "tenant_id": "newstore",
                "sender_key": "whatsapp:1",
                "memory_key": "preferred_name",
                "memory_kind": "preferred_name",
                "value": {"value": "Paulo"},
                "safe_summary": "Paulo",
                "status": "active",
                "importance": 0.8,
                "confidence": 0.9,
                "use_in_instructions": True,
                "sensitive": False,
            },
        ]
    )
    monkeypatch.setattr(
        repo,
        "get_active_contact_memories",
        lambda **kwargs: [
            ContactMemory.model_validate(row)
            for row in store.memories
            if row["status"] == "active"
        ],
    )
    selected = select_relevant_memories(
        tenant_id="newstore",
        sender_key="whatsapp:1",
        domain="commerce",
        limit=5,
    )
    keys = {item.memory_key for item in selected}
    assert "preferred_brands" in keys
