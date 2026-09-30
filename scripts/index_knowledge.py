"""Index approved persona documents; print the metadata patch for publication.

Run only in an environment with OPENAI_API_KEY. This never activates a persona
or uploads customer conversations. Reusing a store is idempotent by source/hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.persona_knowledge import approved_documents


def index_documents(client, payload: dict, *, vector_store_id: str | None = None) -> dict:
    scope = {key: str(payload.get(key) or "").strip()
             for key in ("tenant_id", "workspace_id", "persona_key")}
    if any(not value or len(value) > 256 for value in scope.values()):
        raise ValueError("tenant_id, workspace_id and persona_key are required (max 256 characters)")
    raw_documents = payload.get("knowledge_documents")
    if not isinstance(raw_documents, list):
        raise ValueError("knowledge_documents must be a list")
    documents = approved_documents(raw_documents)
    if not documents or len(documents) > 1000:
        raise ValueError("Provide between 1 and 1000 approved, unexpired documents")
    if any(len(doc["id"]) > 256 for doc in documents):
        raise ValueError("Document IDs must have at most 256 characters")
    if vector_store_id:
        store = client.vector_stores.retrieve(vector_store_id)
        if any((store.metadata or {}).get(k) != v for k, v in scope.items()):
            raise ValueError("Vector store belongs to another workspace or persona")
    else:
        store = client.vector_stores.create(name=f"Knowledge {scope['persona_key']}", metadata=scope)
    print(json.dumps({"vector_store_id": store.id, "status": "indexing"}), file=sys.stderr)
    existing = {}
    for file in client.vector_stores.files.list(vector_store_id=store.id):
        attributes = file.attributes or {}
        if file.status == "completed" and all(attributes.get(k) == v for k, v in scope.items()):
            existing[(attributes.get("source"), attributes.get("version"))] = file.id
    manifest = {}
    for document in documents:
        source, version = document["id"], document["version"]
        file_id = existing.get((source, version))
        if file_id is None:
            filename = hashlib.sha256(source.encode()).hexdigest()[:20] + ".txt"
            uploaded = client.files.create(file=(filename, document["content"].encode("utf-8"), "text/plain"), purpose="assistants")
            file_id = uploaded.id
            try:
                indexed = client.vector_stores.files.create(
                    vector_store_id=store.id, file_id=file_id,
                    attributes={**scope, "source": source, "version": version},
                    chunking_strategy={"type": "static", "static": {
                        "max_chunk_size_tokens": 400, "chunk_overlap_tokens": 80}},
                )
                deadline = time.monotonic() + 120
                while indexed.status == "in_progress" and time.monotonic() < deadline:
                    time.sleep(1)
                    indexed = client.vector_stores.files.retrieve(file_id, vector_store_id=store.id)
                if indexed.status != "completed":
                    raise RuntimeError("knowledge_file_indexing_incomplete")
            except Exception:
                # Delete only the file created by this invocation. Existing files
                # remain available for the currently published persona.
                try:
                    client.files.delete(file_id)
                except Exception:
                    print(json.dumps({"cleanup_failed_file_id": file_id, "vector_store_id": store.id}), file=sys.stderr)
                raise
        manifest[file_id] = {"source": source, "version": version}
    return {"knowledge_index": {**scope, "vector_store_id": store.id, "files": manifest}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON with scope and knowledge_documents")
    parser.add_argument("--vector-store-id", help="Reuse a store owned by the same scope")
    args = parser.parse_args()
    from app.config import get_settings
    from app.openai_client import get_sync_openai_client
    if not get_settings().openai_api_key:
        print("OPENAI_API_KEY is required in the execution environment.", file=sys.stderr)
        return 2
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
        patch = index_documents(get_sync_openai_client().with_options(max_retries=0, timeout=30),
                                payload, vector_store_id=args.vector_store_id)
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "status": "index_not_published"}), file=sys.stderr)
        return 1
    print(json.dumps(patch, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
