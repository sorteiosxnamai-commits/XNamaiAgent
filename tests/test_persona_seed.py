from __future__ import annotations

import hashlib
from pathlib import Path

import app.persona_repository as repo
import scripts.seed_newstore_persona as seed
from tests.persona_fakes import InMemoryPersonaStore

ROOT = Path(__file__).resolve().parents[1]
PERSONA_PATH = ROOT / "persona NS.txt"
EXPECTED_HASH = "bbdc5b84d3d699f31ca87f9f94c4ce86fcdc977c42a1de95859e0383be025b45"


def test_persona_file_hash_is_stable():
    path, text = seed.load_persona_text(PERSONA_PATH)
    assert path == PERSONA_PATH
    assert "assistente comercial oficial da NewStore" in text.splitlines()[0]
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == EXPECTED_HASH


def test_seed_is_idempotent_and_activates_when_none(monkeypatch):
    store = InMemoryPersonaStore().install(monkeypatch)
    first = seed.seed_persona(persona_path=PERSONA_PATH, activate=True)
    assert first["action"] in {"inserted_v1", "already_present"}
    assert first["hash"] == EXPECTED_HASH
    assert first.get("activated") or first.get("active")
    assert len(store.personas) == 1
    assert store.personas[0]["status"] == "active"
    assert store.personas[0]["instructions_hash"] == EXPECTED_HASH

    second = seed.seed_persona(persona_path=PERSONA_PATH, activate=True)
    assert second["action"] == "already_present"
    assert len(store.personas) == 1


def test_seed_does_not_overwrite_different_active(monkeypatch):
    store = InMemoryPersonaStore().install(monkeypatch)
    other = repo.create_persona_version(
        instructions="persona diferente controlada pelo admin\n",
        name="Other",
        created_by="test",
        status="draft",
    )
    repo.activate_persona_version(other.id, activated_by="test")

    result = seed.seed_persona(persona_path=PERSONA_PATH, activate=True)
    assert result.get("note") == "active_persona_preserved" or result["action"] in {
        "inserted_v1",
        "already_present",
        "skipped_existing_different_v1",
    }
    active = [row for row in store.personas if row["status"] == "active"]
    assert len(active) == 1
    assert active[0]["id"] == other.id
