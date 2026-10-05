"""Ricardo's sequence replayed with synthetic data and no external services."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.direct.registration import registration_turn
from app.models import IncomingMessage

CAPABILITIES = {"create_customer", "lookup_customer_by_document"}


class Conversation:
    def __init__(self, *, ready=True, preview=False, create=None, lookup=None):
        self.state = CommerceConversationState()
        self.previous = {}
        self.history = [{"role": "assistant", "content": "Você quer comprar com CPF ou CNPJ?"}]
        self.provider = SimpleNamespace(runtime_capabilities=CAPABILITIES if ready else set())
        self.calls = []
        self.preview = preview

        async def execute(name, arguments):
            self.calls.append((name, arguments))
            return ((lookup or {"ok": True, "found": False}) if name == "lookup_customer_by_document"
                    else (create or {"ok": True, "customer_id": "synthetic-customer"}))
        self.provider.execute = execute

    async def say(self, text):
        result = await registration_turn(IncomingMessage(text=text,
            provider="test" if self.preview else "ycloud", sender_phone="5511988887777"),
            self.state, self.provider, self.history, self.previous)
        if result:
            self.state = evolve_commerce_state(self.state, result)
            self.previous = result.response_metadata
            self.history += [{"role": "user", "content": text},
                             {"role": "assistant", "content": result.reply_text}]
        return result

    async def review(self):
        for text in ("cpf", "529.982.247-25", "Maria Souza", "maria@example.com"):
            result = await self.say(text)
            assert result is not None
        assert self.state.customer_registration["status"] == "review"
        assert self.calls == []
        return result


@pytest.mark.asyncio
async def test_ricardo_sequence_creates_only_after_delivered_review_and_yes():
    chat = Conversation()
    review = await chat.review()
    assert "529.982.247-25" not in review.reply_text
    result = await chat.say("sim")
    assert result.response_metadata["customer_registration_state"]["status"] == "created"
    await chat.say("quero me cadastrar")
    assert [name for name, _ in chat.calls] == ["lookup_customer_by_document", "create_customer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("preview,ready", [(False, False), (True, True)])
async def test_unavailable_and_preview_never_collect_or_call_provider(preview, ready):
    chat = Conversation(preview=preview, ready=ready)
    result = await chat.say("cpf")
    assert result.safety_reason == "customer_registration_unavailable"
    assert chat.state.customer_registration["draft"] == {}
    assert chat.calls == []


@pytest.mark.asyncio
async def test_existing_customer_is_linked_without_post():
    chat = Conversation(lookup={"ok": True, "found": True, "customer_id": "existing"})
    await chat.review()
    await chat.say("sim")
    assert [name for name, _ in chat.calls] == ["lookup_customer_by_document"]


@pytest.mark.asyncio
async def test_unknown_creation_is_not_retried():
    chat = Conversation(create={"ok": False, "code": "customer_creation_unknown"})
    await chat.review()
    await chat.say("sim")
    await chat.say("quero me cadastrar")
    assert [name for name, _ in chat.calls].count("create_customer") == 1


@pytest.mark.asyncio
async def test_yes_after_detour_requests_review_again():
    chat = Conversation()
    await chat.review()
    chat.previous = {"response_source": "direct"}
    chat.history.append({"role": "assistant", "content": "Quer ver os fones?"})
    result = await chat.say("sim")
    assert chat.calls == []
    assert result.response_metadata["customer_registration_state"]["status"] == "review"


@pytest.mark.asyncio
async def test_pipeline_registration_never_calls_model_and_persists_operation(monkeypatch):
    from app.direct import pipeline
    from app.config import Settings
    workspace = "11111111-1111-4111-8111-111111111111"
    cfg = Settings(_env_file=None, CHATBO_WORKSPACE_ID=workspace)
    monkeypatch.setattr(pipeline, "get_settings", lambda: cfg)
    monkeypatch.setattr(pipeline, "load_persona", lambda *_: (workspace,
        SimpleNamespace(id=1, metadata={})))
    monkeypatch.setattr(pipeline, "load_state", AsyncMock(return_value=CommerceConversationState()))
    monkeypatch.setattr("app.commerce.provider.get_commerce_provider",
        lambda: SimpleNamespace(runtime_capabilities=CAPABILITIES))
    monkeypatch.setattr("app.openai_client.get_async_openai_client",
        lambda: pytest.fail("registration must bypass model"))
    saved = []
    monkeypatch.setattr("app.conversation_lifecycle.persist_state", lambda incoming, state: saved.append(state))
    result = await pipeline.run_direct_turn(IncomingMessage(workspace_id=workspace,
        provider="ycloud", text="quero me cadastrar"), history=[], previous={})
    assert result.response_metadata["direct_agent"]["calls"] == 0
    assert result.response_metadata["conversation_commit"] == {"version": 1}
    assert saved and saved[0].pending_action is None


@pytest.mark.asyncio
async def test_undelivered_review_cannot_authorize_creation():
    from app.conversation_lifecycle import operation_snapshot
    chat = Conversation()
    await chat.say("cpf")
    await chat.say("529.982.247-25")
    await chat.say("Maria Souza")
    before = chat.state.model_copy(deep=True)
    await chat.say("maria@example.com")
    chat.state = operation_snapshot(chat.state, before)
    result = await chat.say("sim")
    assert chat.calls == []
    assert result.response_metadata["customer_registration_state"]["status"] == "review"
