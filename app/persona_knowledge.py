"""Bounded retrieval from documents published with a versioned persona."""
from datetime import datetime, timezone
import hashlib
import re
import unicodedata


def _tokens(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", text.casefold()).encode("ascii", "ignore").decode()
    stop = {"que", "qual", "quais", "para", "com", "como", "uma", "por", "voces", "tem", "sobre"}
    return {word for word in re.findall(r"[a-z0-9]+", folded) if len(word) > 2 and word not in stop}


def retrieve_knowledge(documents: list, query: str, *, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    terms = _tokens(query)
    if not terms:
        return []
    candidates = []
    for document in documents[:100]:
        if not isinstance(document, dict) or document.get("status", "approved") not in {"approved", "ready"}:
            continue
        identity = str(document.get("id") or "").strip()
        content = str(document.get("content") or "").strip()
        if not identity or not content:
            continue
        if document.get("valid_until"):
            try:
                deadline = datetime.fromisoformat(str(document["valid_until"]).replace("Z", "+00:00"))
                if deadline.tzinfo is None or deadline <= now:
                    continue
            except ValueError:
                continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        title = str(document.get("title") or identity)
        for offset in range(0, min(len(content), 40000), 1400):
            chunk = content[offset:offset + 1400]
            overlap = len(terms & _tokens(title + " " + chunk))
            if overlap:
                candidates.append((overlap, identity, offset, {"source": identity, "title": title[:200],
                                  "version": digest, "chunk": offset // 1400 + 1, "content": chunk}))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    from .business_policy import current_policy
    return [item[3] for item in candidates[:current_policy().knowledge_max_chunks]]
