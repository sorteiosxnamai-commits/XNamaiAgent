from tests.persona_fakes import InMemoryPersonaStore


def test_instruction_update_preserves_published_knowledge_and_remains_idempotent(monkeypatch):
    import scripts.publish_xnamai_persona as publish
    store = InMemoryPersonaStore().install(monkeypatch)
    for name, fn in store.fakes.items():
        if hasattr(publish, name):
            monkeypatch.setattr(publish, name, fn)
    original = publish.create_persona_version(instructions="Instruções anteriores", metadata={
        "knowledge_documents": [{"id": "a", "content": "Cadastro CPF"}],
        "knowledge_index": {"vector_store_id": "vs_existing"}, "business_policies": {}})
    publish.activate_persona_version(original.id)
    first = publish.publish()
    active = publish.get_active_persona()
    assert active.metadata["knowledge_documents"] == original.metadata["knowledge_documents"]
    assert active.metadata["knowledge_index"] == original.metadata["knowledge_index"]
    assert first["persona_id"] != original.id
    assert publish.publish()["persona_id"] == first["persona_id"]
