from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from app.knowledge_indexing import index_step, readiness
from app.config import Settings
from app.persona_knowledge import approved_documents


SCOPE = {"tenant_id": "xnamai", "workspace_id": "workspace-a", "persona_key": "commercial"}
DOC = {"id": "minimum", "content": "Pedido mínimo R$ 950.", "status": "approved"}


def fixture():
    persona = NS(**SCOPE, status="draft", metadata={"knowledge_documents": [DOC]})
    client = Mock()
    client.vector_stores.create.return_value = NS(id="vs_one")
    client.vector_stores.retrieve.return_value = NS(metadata=SCOPE)
    client.vector_stores.files.list.return_value = []
    client.files.create.return_value = NS(id="file_one")
    client.vector_stores.files.create.return_value = NS(status="in_progress")
    return persona, client


def test_incremental_index_resumes_pending_upload_without_duplicate():
    persona, client = fixture()
    persona.metadata, report = index_step(client, persona)
    assert report["status"] == "indexing"
    client.files.create.assert_not_called()
    persona.metadata, report = index_step(client, persona)
    assert client.files.create.call_count == 1
    file = NS(id="file_one", status="in_progress", attributes={**SCOPE,
              "source": "minimum", "version": approved_documents([DOC])[0]["version"]})
    client.vector_stores.files.list.return_value = [file]
    persona.metadata, report = index_step(client, persona)
    assert client.files.create.call_count == 1
    assert report["status"] == "indexing"
    file.status = "completed"
    persona.metadata, report = index_step(client, persona)
    assert report["status"] == "ready"
    assert readiness(persona, Settings())["index_ready"]
    persona.metadata["knowledge_documents"][0] = {**DOC, "content": "Changed policy"}
    assert not readiness(persona, Settings())["index_ready"]


def test_other_workspace_or_active_persona_cannot_be_indexed():
    persona, client = fixture()
    persona.status = "active"
    with pytest.raises(ValueError, match="draft_required"):
        index_step(client, persona)
    client.vector_stores.create.assert_not_called()
    persona.status = "draft"
    persona.metadata, _ = index_step(client, persona)
    client.vector_stores.retrieve.return_value = NS(metadata={**SCOPE, "workspace_id": "other"})
    with pytest.raises(ValueError, match="scope_mismatch"):
        index_step(client, persona)
    client.files.create.assert_not_called()


def test_draft_documents_are_never_uploaded():
    persona, client = fixture()
    persona.metadata["knowledge_documents"] = [{**DOC, "status": "draft"}]
    with pytest.raises(ValueError):
        index_step(client, persona)
    client.vector_stores.create.assert_not_called()
