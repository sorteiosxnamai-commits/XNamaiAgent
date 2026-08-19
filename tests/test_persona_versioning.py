from __future__ import annotations

import app.persona_repository as repo
from tests.persona_fakes import InMemoryPersonaStore


def test_version_numbers_increment(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    v1 = repo.create_persona_version(instructions="one\n", name="1")
    v2 = repo.create_persona_version(instructions="two\n", name="2")
    v3 = repo.create_persona_version(instructions="three\n", name="3")
    assert [v1.version, v2.version, v3.version] == [1, 2, 3]


def test_activate_archives_previous(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    v1 = repo.create_persona_version(instructions="one\n", name="1")
    v2 = repo.create_persona_version(instructions="two\n", name="2")
    repo.activate_persona_version(v1.id, activated_by="a")
    repo.activate_persona_version(v2.id, activated_by="b")
    versions = {item.id: item for item in repo.list_persona_versions()}
    assert versions[v1.id].status == "archived"
    assert versions[v2.id].status == "active"
    assert versions[v2.id].activated_by == "b"


def test_rollback_restores_archived_version(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    v1 = repo.create_persona_version(instructions="one\n", name="1")
    v2 = repo.create_persona_version(instructions="two\n", name="2")
    repo.activate_persona_version(v1.id)
    repo.activate_persona_version(v2.id)
    repo.rollback_persona_version(v1.id, activated_by="rollback")
    assert repo.get_active_persona().id == v1.id
    assert repo.get_persona_version(v2.id).status == "archived"
