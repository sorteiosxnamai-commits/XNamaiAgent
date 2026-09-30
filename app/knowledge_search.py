"""OpenAI retrieval with published-document authority and turn-local isolation.

The vector store ranks passages; the active persona still decides which documents
and versions may be used. An unavailable or stale index falls back to local search.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import re

from .persona_knowledge import approved_documents, contextual_query, query_needs_context, retrieve_knowledge


@dataclass
class KnowledgeEvidence:
    tenant_id: str
    workspace_id: str
    persona_key: str
    query: str
    passages: list[dict] = field(default_factory=list)
    status: str = "lexical"

    def report(self) -> dict:
        return {"status": self.status, "sources": list(dict.fromkeys(
            passage["source"] for passage in self.passages)), "chunks": len(self.passages)}


_EVIDENCE: ContextVar[KnowledgeEvidence | None] = ContextVar("knowledge_evidence", default=None)


def bind_evidence(evidence: KnowledgeEvidence | None):
    return _EVIDENCE.set(evidence)


def reset_evidence(token):
    _EVIDENCE.reset(token)


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def merge_passages(semantic: list[dict], lexical: list[dict]) -> list[dict]:
    from .business_policy import current_policy

    merged = []
    seen = set()
    # Alternate rankings so both semantic paraphrases and exact identifiers survive.
    for index in range(max(len(semantic), len(lexical))):
        for ranked in (semantic, lexical):
            if index >= len(ranked):
                continue
            item = ranked[index]
            key = (item["source"], _text(item["content"]))
            if key not in seen:
                merged.append(item)
                seen.add(key)
    return merged[:current_policy().knowledge_max_chunks]


def prompt_passages(documents: list, query: str, *, tenant_id: str,
                    workspace_id: str | None, persona_key: str, recent_turns=None) -> list[dict]:
    """Never reuse evidence across a workspace, persona, query or document version."""
    evidence = _EVIDENCE.get()
    lexical = retrieve_knowledge(documents, contextual_query(query, recent_turns))
    if evidence is None or (
        evidence.tenant_id, evidence.workspace_id, evidence.persona_key, evidence.query
    ) != (tenant_id, str(workspace_id or ""), persona_key, str(query or "")):
        return lexical
    current = {doc["id"]: doc for doc in approved_documents(documents)}
    valid = [item for item in evidence.passages if item["source"] in current
             and current[item["source"]]["version"] == item["version"]]
    return merge_passages(valid, lexical)


async def prepare_knowledge(active, query: str, *, tenant_id: str,
                            workspace_id: str | None, persona_key: str,
                            settings, recent_turns=None, incoming=None) -> KnowledgeEvidence:
    evidence = KnowledgeEvidence(tenant_id, str(workspace_id or ""), persona_key, str(query or ""))
    metadata = getattr(active, "metadata", None) or {}
    documents = metadata.get("knowledge_documents") or []
    if not isinstance(documents, list):
        documents = []
    search_query = contextual_query(query, recent_turns)
    evidence.passages = retrieve_knowledge(documents, search_query)
    if not getattr(settings, "agent_knowledge_search_enabled", False):
        return evidence
    index = metadata.get("knowledge_index")
    scope = {"tenant_id": tenant_id, "workspace_id": str(workspace_id or ""), "persona_key": persona_key}
    if not workspace_id or not isinstance(index, dict) or any(index.get(k) != v for k, v in scope.items()):
        evidence.status = "index_missing_or_scope_mismatch"
        return evidence
    store_id = index.get("vector_store_id")
    manifest = index.get("files")
    if not isinstance(store_id, str) or not store_id.startswith("vs_") or not isinstance(manifest, dict):
        evidence.status = "index_invalid"
        return evidence
    current = {doc["id"]: doc for doc in approved_documents(documents)}
    if not current or not search_query.strip() or not getattr(settings, "openai_api_key", ""):
        evidence.status = "search_not_ready"
        return evidence
    from .openai_client import get_async_openai_client
    timeout = float(getattr(settings, "agent_knowledge_search_timeout_seconds", 5.0))
    threshold = float(getattr(settings, "agent_knowledge_search_min_score", 0.35))
    try:
        if recent_turns is None and incoming is not None and query_needs_context(query):
            from .db import load_recent_conversation_turns
            raw_id = (incoming.raw or {}).get("inbound_id")
            try:
                inbound_id = int(raw_id) if raw_id is not None else None
            except (TypeError, ValueError):
                inbound_id = None
            recent_turns = load_recent_conversation_turns(
                conversation_id=incoming.conversation_id, sender_phone=incoming.sender_phone,
                sender_key=incoming.sender_key, before_inbound_id=inbound_id, limit=4, hard_cap=4,
            )
            search_query = contextual_query(query, recent_turns)
            evidence.passages = retrieve_knowledge(documents, search_query)
        # One bounded read, no generation call and no retry loop. Shared client hooks
        # account for the HTTP attempt in the existing per-turn transport budget.
        client = get_async_openai_client().with_options(max_retries=0, timeout=timeout)
        response = await asyncio.wait_for(client.vector_stores.search(
            vector_store_id=store_id, query=search_query, max_num_results=12,
            rewrite_query=True,
            ranking_options={"ranker": "auto", "score_threshold": threshold},
            filters={"type": "and", "filters": [
                {"type": "eq", "key": key, "value": value} for key, value in scope.items()
            ]},
        ), timeout=timeout)
        semantic = []
        for hit in response.data:
            entry = manifest.get(hit.file_id)
            attributes = hit.attributes or {}
            if not isinstance(entry, dict) or any(attributes.get(k) != v for k, v in scope.items()):
                continue
            document = current.get(entry.get("source"))
            if document is None or entry.get("version") != document["version"]:
                continue
            if attributes.get("source") != document["id"] or attributes.get("version") != document["version"]:
                continue
            if hit.score < threshold:
                continue
            for part in hit.content:
                content = _text(part.text) if part.type == "text" else ""
                # Remote text cannot introduce material absent from the approved source.
                if not content or content not in _text(document["content"]):
                    continue
                semantic.append({"source": document["id"], "title": str(document.get("title") or document["id"])[:200],
                                 "version": document["version"], "content": content[:1400],
                                 "chunk": _text(document["content"]).index(content) // 1200 + 1,
                                 "file_id": hit.file_id, "retrieval": "semantic"})
        evidence.passages = merge_passages(semantic, evidence.passages)
        evidence.status = "hybrid" if semantic else "semantic_no_valid_hits"
    except Exception as exc:
        # No raw exception: request bodies may contain personal information.
        from .observability import log_event
        log_event("knowledge.search_fallback", {"error_type": type(exc).__name__})
        evidence.status = "search_failed_lexical_fallback"
    return evidence
