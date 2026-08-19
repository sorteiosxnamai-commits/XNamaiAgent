"""In-memory stores for Phase 5 memory tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self.proposals: list[dict[str, Any]] = []
        self.memories: list[dict[str, Any]] = []
        self.extensions: list[dict[str, Any]] = []
        self.summaries: list[dict[str, Any]] = []
        self._pid = 1
        self._mid = 1
        self._eid = 1
        self._sid = 1

    def install(self, monkeypatch) -> "InMemoryMemoryStore":
        store = self

        def insert_memory_proposal(**kwargs):
            key = kwargs["idempotency_key"]
            for row in store.proposals:
                if row["idempotency_key"] == key:
                    return row["id"]
            row = {"id": store._pid, **kwargs}
            store._pid += 1
            store.proposals.append(row)
            return row["id"]

        def mark_proposal_rejected(proposal_id, *, rejection_codes=None, status="rejected"):
            for row in store.proposals:
                if row["id"] == proposal_id:
                    row["status"] = status
                    row["rejection_codes"] = rejection_codes or []
                    return

        def mark_proposal_duplicate(proposal_id):
            mark_proposal_rejected(
                proposal_id, rejection_codes=["duplicate"], status="duplicate"
            )

        def mark_proposal_applied(
            proposal_id, *, applied_memory_id=None, applied_extension_id=None
        ):
            for row in store.proposals:
                if row["id"] == proposal_id:
                    row["status"] = "applied"
                    row["applied_memory_id"] = applied_memory_id
                    row["applied_extension_id"] = applied_extension_id
                    return

        def mark_proposal_pending_review(proposal_id):
            for row in store.proposals:
                if row["id"] == proposal_id:
                    row["status"] = "pending"
                    return

        def get_active_contact_memories(*, tenant_id, sender_key, limit=20):
            from app.memory_models import ContactMemory

            rows = [
                row
                for row in store.memories
                if row["tenant_id"] == tenant_id
                and row["sender_key"] == sender_key
                and row["status"] == "active"
                and not row.get("sensitive")
            ]
            rows.sort(key=lambda item: item.get("importance", 0), reverse=True)
            return [ContactMemory.model_validate(row) for row in rows[:limit]]

        def upsert_contact_memory(**kwargs):
            from app.memory_models import ContactMemory

            now = datetime.now(timezone.utc)
            for row in store.memories:
                if (
                    row["tenant_id"] == kwargs["tenant_id"]
                    and row["sender_key"] == kwargs["sender_key"]
                    and row["memory_key"] == kwargs["memory_key"]
                    and row["status"] == "active"
                ):
                    row["status"] = "superseded"
                    row["updated_at"] = now
            value = kwargs.get("value")
            if not isinstance(value, (dict, list)):
                value = {"value": value}
            row = {
                "id": store._mid,
                "tenant_id": kwargs["tenant_id"],
                "sender_key": kwargs["sender_key"],
                "memory_key": kwargs["memory_key"],
                "memory_kind": kwargs["memory_kind"],
                "value": value,
                "safe_summary": kwargs.get("safe_summary"),
                "source": kwargs.get("source", "model_proposal"),
                "status": kwargs.get("status", "active"),
                "importance": kwargs.get("importance", 0),
                "confidence": kwargs.get("confidence", 0),
                "use_in_instructions": kwargs.get("use_in_instructions", True),
                "sensitive": kwargs.get("sensitive", False),
                "expires_at": kwargs.get("expires_at"),
                "metadata": deepcopy(kwargs.get("metadata") or {}),
            }
            store._mid += 1
            store.memories.append(row)
            return ContactMemory.model_validate(row)

        def forget_contact_memory(*, tenant_id, sender_key, memory_key):
            count = 0
            for row in store.memories:
                if (
                    row["tenant_id"] == tenant_id
                    and row["sender_key"] == sender_key
                    and row["memory_key"] == memory_key
                    and row["status"] == "active"
                ):
                    row["status"] = "forgotten"
                    row["use_in_instructions"] = False
                    count += 1
            return count

        def create_extension_proposal(**kwargs):
            row = {
                "id": store._eid,
                "tenant_id": kwargs["tenant_id"],
                "scope": kwargs.get("scope", "tenant"),
                "scope_key": kwargs.get("scope_key"),
                "scope_key_norm": kwargs.get("scope_key") or "",
                "extension_key": kwargs["extension_key"],
                "category": kwargs["category"],
                "instruction_text": kwargs["instruction_text"],
                "status": "pending_review",
                "source": kwargs.get("source", "model_proposal"),
                "importance": kwargs.get("importance"),
                "confidence": kwargs.get("confidence"),
                "metadata": deepcopy(kwargs.get("metadata") or {}),
            }
            store._eid += 1
            store.extensions.append(row)
            return dict(row)

        def apply_summary_delta(**kwargs):
            row = {
                "id": store._sid,
                "tenant_id": kwargs["tenant_id"],
                "conversation_key": kwargs["conversation_key"],
                "delta": kwargs["delta"].model_dump(mode="json"),
            }
            store._sid += 1
            store.summaries.append(row)
            return row

        modules = []
        import app.memory_service as memory_service

        modules.append(memory_service)
        try:
            import app.memory_proposal_repository as prop_repo

            modules.append(prop_repo)
        except Exception:
            pass
        try:
            import app.contact_memory_repository as mem_repo

            modules.append(mem_repo)
        except Exception:
            pass
        try:
            import app.instruction_extension_repository as ext_repo

            modules.append(ext_repo)
        except Exception:
            pass
        try:
            import app.conversation_summary_repository as sum_repo

            modules.append(sum_repo)
        except Exception:
            pass

        bindings = {
            "insert_memory_proposal": insert_memory_proposal,
            "mark_proposal_rejected": mark_proposal_rejected,
            "mark_proposal_duplicate": mark_proposal_duplicate,
            "mark_proposal_applied": mark_proposal_applied,
            "mark_proposal_pending_review": mark_proposal_pending_review,
            "get_active_contact_memories": get_active_contact_memories,
            "upsert_contact_memory": upsert_contact_memory,
            "forget_contact_memory": forget_contact_memory,
            "create_extension_proposal": create_extension_proposal,
            "apply_summary_delta": apply_summary_delta,
        }
        for mod in modules:
            for name, fn in bindings.items():
                if hasattr(mod, name):
                    monkeypatch.setattr(mod, name, fn)
        return self
