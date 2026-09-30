"""Bounded retrieval from documents published with a versioned persona."""
from datetime import datetime, timezone
import hashlib
import re
import unicodedata


def _tokens(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", text.casefold()).encode("ascii", "ignore").decode()
    stop = {"que", "qual", "quais", "para", "com", "como", "uma", "por", "voces", "tem", "sobre"}
    return {word for word in re.findall(r"[a-z0-9]+", folded) if len(word) > 2 and word not in stop}


def approved_documents(documents: list, *, now: datetime | None = None) -> list[dict]:
    """Shared eligibility rules for lexical retrieval, indexing and semantic hits."""
    now = now or datetime.now(timezone.utc)
    approved = []
    seen = set()
    for document in documents:
        if not isinstance(document, dict) or document.get("status", "approved") not in {"approved", "ready"}:
            continue
        identity = str(document.get("id") or "").strip()
        content = str(document.get("content") or "").strip()
        if not identity or not content or identity in seen:
            continue
        if document.get("valid_until"):
            try:
                deadline = datetime.fromisoformat(str(document["valid_until"]).replace("Z", "+00:00"))
                if deadline.tzinfo is None or deadline <= now:
                    continue
            except ValueError:
                continue
        seen.add(identity)
        approved.append({**document, "id": identity, "content": content,
                         "version": hashlib.sha256(content.encode("utf-8")).hexdigest()})
    return approved


def query_needs_context(query: str) -> bool:
    words = _tokens(str(query or ""))
    references = {"isso", "esse", "essa", "aquele", "aquela", "nesse", "nessa", "tambem"}
    return bool(str(query or "").strip()) and (len(words) <= 2 or bool(words & references))


def contextual_query(query: str, recent_turns: list | None = None) -> str:
    """Resolve short follow-ups without mixing unrelated historical questions."""
    text = str(query or "").strip()
    if query_needs_context(text):
        previous = [str(t.get("content") or "").strip() for t in (recent_turns or [])
                    if isinstance(t, dict) and t.get("role") == "user" and t.get("content")]
        previous = [value for value in previous if value != text]
        if previous:
            return f"Contexto anterior: {previous[-1][:800]}\nPergunta atual: {text[:1200]}"
    return text[:2000]


def retrieve_knowledge(documents: list, query: str, *, now: datetime | None = None) -> list[dict]:
    terms = _tokens(query)
    if not terms:
        return []
    candidates = []
    for document in approved_documents(documents, now=now):
        identity, content, digest = document["id"], document["content"], document["version"]
        title = str(document.get("title") or identity)
        # Overlap protects sentences at chunk boundaries. Do not silently discard
        # the tail of a published document or documents after the first hundred.
        for offset in range(0, len(content), 1200):
            chunk = content[offset:offset + 1400]
            overlap = len(terms & _tokens(title + " " + chunk))
            if overlap:
                candidates.append((overlap, identity, offset, {"source": identity, "title": title[:200],
                                  "version": digest, "chunk": offset // 1200 + 1, "content": chunk}))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    from .business_policy import current_policy
    return [item[3] for item in candidates[:current_policy().knowledge_max_chunks]]
