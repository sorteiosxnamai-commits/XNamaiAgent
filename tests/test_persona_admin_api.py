from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.persona_admin_api as admin
import app.persona_repository as repo
import app.prompt_compiler as compiler
from app.persona_admin_api import router
from app.security import verify_admin_token
from tests.persona_fakes import InMemoryPersonaStore


async def _allow_admin() -> None:
    return None


def _settings():
    return SimpleNamespace(
        agent_db_persona_enabled=True,
        agent_prompt_compilation_audit_enabled=False,
        agent_debug_store_compiled_prompt=False,
        agent_max_recent_turns=8,
        openai_api_mode="chat_completions",
        agent_persona_tenant_id="newstore",
        agent_persona_key="newstore_commercial",
    )


@pytest.fixture()
def client(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    app = FastAPI()
    app.dependency_overrides[verify_admin_token] = _allow_admin
    app.include_router(router)
    monkeypatch.setattr(admin, "get_settings", _settings)
    monkeypatch.setattr(compiler, "get_settings", _settings)
    return TestClient(app)


def test_admin_create_activate_list(client):
    created = client.post(
        "/api/admin/agents/newstore/personas",
        json={"name": "NS", "instructions": "persona admin\n", "created_by": "tester"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["ok"] is True
    persona_id = body["persona"]["id"]
    assert body["persona"]["status"] == "draft"

    activated = client.post(
        f"/api/admin/agents/newstore/personas/{persona_id}/activate"
    )
    assert activated.status_code == 200
    assert activated.json()["persona"]["status"] == "active"

    listed = client.get("/api/admin/agents/newstore/personas")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1

    active = client.get("/api/admin/agents/newstore/personas/active")
    assert active.status_code == 200
    assert active.json()["persona"]["id"] == persona_id


def test_admin_archive_and_rollback(client):
    v1 = repo.create_persona_version(instructions="v1\n", name="V1")
    v2 = repo.create_persona_version(instructions="v2\n", name="V2")
    repo.activate_persona_version(v1.id)
    repo.activate_persona_version(v2.id)

    archived = client.post(f"/api/admin/agents/newstore/personas/{v2.id}/archive")
    assert archived.status_code == 200
    assert archived.json()["persona"]["status"] == "archived"

    rolled = client.post(f"/api/admin/agents/newstore/personas/{v1.id}/rollback")
    assert rolled.status_code == 200
    assert rolled.json()["persona"]["status"] == "active"
    assert rolled.json()["persona"]["id"] == v1.id


def test_admin_prompt_preview(client):
    created = repo.create_persona_version(instructions="PREVIEW_PERSONA\n", name="P")
    repo.activate_persona_version(created.id)
    preview = client.get(
        "/api/admin/agents/newstore/prompt-preview",
        params={"channel": "instagram", "sender_key": "instagram:secret123", "text": "oi"},
    )
    assert preview.status_code == 200
    data = preview.json()
    assert data["ok"] is True
    assert data["used_db_persona"] is True
    assert data["blocks"]["fixed_safety_policy"]
    assert "secret123" not in str(data)
    assert data["sender_key_masked"].endswith("***")
