from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from scripts.index_knowledge import index_documents
from app.persona_knowledge import approved_documents


SCOPE = {"tenant_id": "xnamai", "workspace_id": "workspace-a", "persona_key": "commercial"}
DOCUMENT = {"id": "cadastro", "content": "Cadastro permitido para pessoas físicas."}


def client_for_index():
    client = Mock()
    client.vector_stores.create.return_value = NS(id="vs_new", metadata=SCOPE)
    client.vector_stores.retrieve.return_value = NS(id="vs_new", metadata=SCOPE)
    client.vector_stores.files.list.return_value = []
    client.files.create.return_value = NS(id="file_new")
    client.vector_stores.files.create.return_value = NS(status="completed")
    return client


def test_only_approved_documents_are_uploaded_and_manifest_is_reusable():
    client = client_for_index()
    payload = {**SCOPE, "knowledge_documents": [DOCUMENT,
        {"id": "draft", "status": "draft", "content": "Não publicar"},
        {"id": "expired", "valid_until": "2000-01-01T00:00:00Z", "content": "Vencido"}]}
    patch = index_documents(client, payload)
    assert client.files.create.call_count == 1
    index = patch["knowledge_index"]
    version = approved_documents([DOCUMENT])[0]["version"]
    assert index["files"] == {"file_new": {"source": "cadastro", "version": version}}
    assert client.vector_stores.files.create.call_args.kwargs["attributes"] == {
        **SCOPE, "source": "cadastro", "version": version}

    client.vector_stores.files.list.return_value = [NS(
        id="file_new", status="completed", attributes={**SCOPE, "source": "cadastro", "version": version})]
    assert index_documents(client, payload, vector_store_id="vs_new") == patch
    assert client.files.create.call_count == 1


def test_wrong_scope_fails_before_upload():
    client = client_for_index()
    client.vector_stores.retrieve.return_value = NS(id="vs_other", metadata={**SCOPE, "workspace_id": "other"})
    with pytest.raises(ValueError, match="another workspace"):
        index_documents(client, {**SCOPE, "knowledge_documents": [DOCUMENT]}, vector_store_id="vs_other")
    client.files.create.assert_not_called()


def test_failed_index_cleans_up_new_file_and_does_not_publish():
    client = client_for_index()
    client.vector_stores.files.create.return_value = NS(status="failed")
    with pytest.raises(RuntimeError):
        index_documents(client, {**SCOPE, "knowledge_documents": [DOCUMENT]})
    client.files.delete.assert_called_once_with("file_new")


def test_missing_scope_does_not_create_store():
    client = client_for_index()
    with pytest.raises(ValueError):
        index_documents(client, {"knowledge_documents": [DOCUMENT]})
    client.vector_stores.create.assert_not_called()
