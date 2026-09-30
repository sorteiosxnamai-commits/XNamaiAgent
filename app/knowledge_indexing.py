"""Incremental indexing for serverless requests; no sleep or customer data upload."""
from __future__ import annotations

import hashlib
from copy import deepcopy

from .persona_knowledge import approved_documents


def index_step(client, persona):
    scope = {"tenant_id": persona.tenant_id, "workspace_id": persona.workspace_id,
             "persona_key": persona.persona_key}
    if persona.status != "draft" or any(not v for v in scope.values()):
        raise ValueError("scoped_draft_required")
    metadata = deepcopy(persona.metadata or {})
    raw = metadata.get("knowledge_documents")
    if not isinstance(raw, list):
        raise ValueError("knowledge_documents_required")
    documents = approved_documents(raw)
    if not 1 <= len(documents) <= 100 or any(len(d["id"]) > 256 or len(d["content"]) > 100000 for d in documents):
        raise ValueError("provide_1_to_100_documents_max_100000_chars_each")
    index = metadata.get("knowledge_index") or {}
    if not index:
        store = client.vector_stores.create(name=f"Knowledge {persona.persona_key}", metadata=scope)
        metadata["knowledge_index"] = {**scope, "vector_store_id": store.id, "files": {}, "status": "indexing"}
        return metadata, {"status": "indexing", "completed": 0, "total": len(documents)}
    if any(index.get(k) != v for k, v in scope.items()):
        raise ValueError("index_scope_mismatch")
    store_id = index["vector_store_id"]
    store = client.vector_stores.retrieve(store_id)
    if any((store.metadata or {}).get(k) != v for k, v in scope.items()):
        raise ValueError("store_scope_mismatch")
    # One bounded page. Each draft gets its own store; no legacy file pagination.
    page = client.vector_stores.files.list(vector_store_id=store_id, limit=100)
    if getattr(page, "has_more", False) is True:
        raise ValueError("index_store_too_large")
    files = list(page)
    by_version = {}
    for file in files:
        attrs = file.attributes or {}
        if all(attrs.get(k) == v for k, v in scope.items()):
            by_version[(attrs.get("source"), attrs.get("version"))] = file
    manifest, missing, failed = {}, [], []
    for doc in documents:
        file = by_version.get((doc["id"], doc["version"]))
        if file is None:
            missing.append(doc)
        elif file.status == "completed":
            manifest[file.id] = {"source": doc["id"], "version": doc["version"]}
        elif file.status in {"failed", "cancelled"}:
            failed.append(doc["id"])
    if missing and not failed:
        doc = missing[0]
        filename = hashlib.sha256(doc["id"].encode()).hexdigest()[:20] + ".txt"
        upload = client.files.create(file=(filename, doc["content"].encode(), "text/plain"), purpose="assistants")
        try:
            file = client.vector_stores.files.create(vector_store_id=store_id, file_id=upload.id,
                    attributes={**scope, "source": doc["id"], "version": doc["version"]},
                    chunking_strategy={"type": "static", "static": {
                        "max_chunk_size_tokens": 400, "chunk_overlap_tokens": 80}})
        except Exception:
            client.files.delete(upload.id)
            raise
        if file.status == "completed":
            manifest[upload.id] = {"source": doc["id"], "version": doc["version"]}
        elif file.status in {"failed", "cancelled"}:
            failed.append(doc["id"])
    status = "failed" if failed else "ready" if len(manifest) == len(documents) else "indexing"
    metadata["knowledge_index"] = {**scope, "vector_store_id": store_id, "files": manifest, "status": status}
    return metadata, {"status": status, "completed": len(manifest), "total": len(documents), "failed_sources": failed}


def readiness(persona, settings):
    from .openai_models import resolve_openai_model
    documents = approved_documents((persona.metadata or {}).get("knowledge_documents") or []) if persona else []
    index = ((persona.metadata or {}).get("knowledge_index") or {}) if persona else {}
    indexed = {(f.get("source"), f.get("version")) for f in index.get("files", {}).values() if isinstance(f, dict)}
    scope_ok = bool(persona and all(index.get(k) == getattr(persona, k) for k in ("tenant_id", "workspace_id", "persona_key")))
    return {"active_persona": bool(persona and persona.status == "active"),
            "openai_configured": bool(settings.openai_api_key),
            "model": resolve_openai_model("main", settings=settings),
            "approved_documents": len(documents),
            "index_ready": bool(documents and scope_ok and all((d["id"], d["version"]) in indexed for d in documents)),
            "knowledge_search_enabled": settings.agent_knowledge_search_enabled,
            "consultative_enabled": settings.agent_consultative_enabled,
            "consultative_traffic_percent": settings.agent_consultative_traffic_percent,
            "consultative_emergency_off": settings.agent_consultative_emergency_off}
