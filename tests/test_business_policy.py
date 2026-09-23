import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.business_policy import BusinessPolicy, bind_policy, current_policy, reset_policy
from app.turn_cache import begin_turn_cache, cached_turn_read, end_turn_cache, invalidates_turn_reads


def test_business_controls_reject_credentials_and_unsafe_limits():
    for payload in ({"database_url": "private"}, {"catalog_browse_limit": 1000},
                    {"conversation_repair_handoff_after": 0}, {"repair_handoff": ""}):
        with pytest.raises(ValidationError):
            BusinessPolicy.model_validate(payload)


@pytest.mark.asyncio
async def test_policy_isolation_between_concurrent_conversations():
    async def read(limit):
        token = bind_policy({"business_policies": {"catalog_browse_limit": limit}})
        try:
            await asyncio.sleep(0)
            return current_policy().catalog_browse_limit
        finally:
            reset_policy(token)
    assert await asyncio.gather(read(2), read(8)) == [2, 8]
    assert current_policy().catalog_browse_limit == 10


def test_cached_configuration_does_not_leak_or_survive_publication():
    values = {"version": 1}
    calls = []
    @cached_turn_read
    def read():
        calls.append(1)
        return dict(values)
    @invalidates_turn_reads
    def publish():
        values["version"] = 2
    token = begin_turn_cache()
    try:
        read()["version"] = 100
        assert read()["version"] == 1
        assert len(calls) == 1
        publish()
        assert read()["version"] == 2
        assert len(calls) == 2
    finally:
        end_turn_cache(token)
    read()
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_pipeline_loads_published_policy_and_resets_after_error(monkeypatch):
    from app import message_pipeline
    from app.models import IncomingMessage
    monkeypatch.setattr(message_pipeline, "get_settings", lambda: SimpleNamespace(
        database_url="test-only", agent_db_persona_enabled=True, agent_persona_tenant_id="xnamai",
        agent_persona_key="commercial"))
    monkeypatch.setattr("app.persona_repository.get_active_persona", lambda *args: SimpleNamespace(
        id=8, version=2, metadata={"business_policies": {"catalog_browse_limit": 3}}))
    async def fail(*args):
        assert current_policy().catalog_browse_limit == 3
        raise RuntimeError("test failure")
    monkeypatch.setattr(message_pipeline, "_process_incoming_message", fail)
    with pytest.raises(RuntimeError, match="test failure"):
        await message_pipeline.process_incoming_message(IncomingMessage(text="catálogo"), {})
    assert current_policy().catalog_browse_limit == 10


def test_persona_api_model_rejects_invalid_policy_before_saving():
    from app.persona_models import PersonaVersionCreate
    with pytest.raises(ValidationError):
        PersonaVersionCreate(instructions="Atenda a XNamai", metadata={"business_policies": {"openai_api_key": "invalid"}})
