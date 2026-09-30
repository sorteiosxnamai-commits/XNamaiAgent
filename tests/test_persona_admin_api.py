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
        agent_persona_tenant_id="xnamai",
        agent_persona_key="xnamai_commercial",
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
        "/api/admin/agents/xnamai/personas",
        json={"name": "Xnamai", "instructions": "persona admin\n", "created_by": "tester"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["ok"] is True
    persona_id = body["persona"]["id"]
    assert body["persona"]["status"] == "draft"

    activated = client.post(
        f"/api/admin/agents/xnamai/personas/{persona_id}/activate"
    )
    assert activated.status_code == 200
    assert activated.json()["persona"]["status"] == "active"

    listed = client.get("/api/admin/agents/xnamai/personas")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1

    active = client.get("/api/admin/agents/xnamai/personas/active")
    assert active.status_code == 200
    assert active.json()["persona"]["id"] == persona_id


def test_knowledge_draft_preserves_metadata_and_requires_matching_workspace(client):
    workspace = "aa774d20-509f-4d54-865b-7a5de22b6d30"
    original = repo.create_persona_version(instructions="Persona atual", metadata={
        "custom_setting": "preserved", "knowledge_index": {"vector_store_id": "vs_old"}})
    repo.activate_persona_version(original.id)
    url = f"/api/admin/agents/xnamai/personas/{original.id}/knowledge-draft"
    body = {"knowledge_documents": [{"id": "cadastro", "content": "Cadastro CPF.", "status": "approved"}]}
    wrong = client.post(url, params={"workspace_id": "bb774d20-509f-4d54-865b-7a5de22b6d30"}, json=body)
    assert wrong.status_code == 404
    response = client.post(url, params={"workspace_id": workspace}, json=body)
    assert response.status_code == 200
    draft = repo.get_persona_version(response.json()["persona_id"])
    assert draft.status == "draft"
    assert draft.instructions == original.instructions
    assert draft.metadata["custom_setting"] == "preserved"
    assert "knowledge_index" not in draft.metadata
    assert repo.get_active_persona().id == original.id


def test_knowledge_draft_rejects_implicit_approval_and_conflicting_policies(client):
    original = repo.create_persona_version(instructions="Persona")
    url = f"/api/admin/agents/xnamai/personas/{original.id}/knowledge-draft?workspace_id={original.workspace_id}"
    assert client.post(url, json={"knowledge_documents": [{"id": "a", "content": "foo"}]}).status_code == 422
    docs = [{"id": identity, "content": "foo", "topic": "minimum_order", "status": "approved"} for identity in ("a", "b")]
    assert client.post(url, json={"knowledge_documents": docs}).status_code == 400


def test_new_admin_routes_are_protected(monkeypatch):
    import app.security as security
    monkeypatch.setattr(security, "get_settings", lambda: SimpleNamespace(admin_api_token="test-secret"))
    app = FastAPI()
    app.include_router(router)
    unauthenticated = TestClient(app)
    paths = ["readiness", "quality-evaluation", "personas/1/knowledge-index", "personas/1/knowledge-draft"]
    for path in paths:
        method = unauthenticated.get if path == "readiness" else unauthenticated.post
        assert method(f"/api/admin/agents/xnamai/{path}").status_code == 401


def test_admin_archive_and_rollback(client):
    v1 = repo.create_persona_version(instructions="v1\n", name="V1")
    v2 = repo.create_persona_version(instructions="v2\n", name="V2")
    repo.activate_persona_version(v1.id)
    repo.activate_persona_version(v2.id)

    archived = client.post(f"/api/admin/agents/xnamai/personas/{v2.id}/archive")
    assert archived.status_code == 200
    assert archived.json()["persona"]["status"] == "archived"

    rolled = client.post(f"/api/admin/agents/xnamai/personas/{v1.id}/rollback")
    assert rolled.status_code == 200
    assert rolled.json()["persona"]["status"] == "active"
    assert rolled.json()["persona"]["id"] == v1.id


def test_admin_prompt_preview(client):
    created = repo.create_persona_version(instructions="PREVIEW_PERSONA\n", name="P")
    repo.activate_persona_version(created.id)
    preview = client.get(
        "/api/admin/agents/xnamai/prompt-preview",
        params={"channel": "instagram", "sender_key": "instagram:secret123", "text": "oi"},
    )
    assert preview.status_code == 200
    data = preview.json()
    assert data["ok"] is True
    assert data["used_db_persona"] is True
    assert data["blocks"]["fixed_safety_policy"]
    assert "secret123" not in str(data)
    assert data["sender_key_masked"].endswith("***")
