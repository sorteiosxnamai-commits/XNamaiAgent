"""Deterministic, allowlisted Mercos customer creation errors (no LLM or PII)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

FIELDS = frozenset({"razao_social", "tipo", "cnpj", "nome_fantasia",
                    "inscricao_estadual", "suframa", "cep", "rua", "numero",
                    "complemento", "bairro", "cidade", "estado", "emails", "telefones"})
ALIASES = {"email": "emails", "e-mail": "emails", "telefone": "telefones",
           "razao social": "razao_social", "nome fantasia": "nome_fantasia",
           "inscricao estadual": "inscricao_estadual", "numero": "numero"}


def _fold(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def parse_customer_validation(details: Any, message: str = "") -> tuple[str, ...]:
    """Return only documented fields with clear required-field evidence."""
    found: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str):
            line = _fold(value)
            required = bool(re.search(r"obrigatori|required|no minimo\s*1|at least\s*1", line))
            if not required:
                return
            label = _fold(key).replace("-", "_").replace(" ", "_")
            if label in FIELDS:
                found.add(label)
            elif label in ALIASES:
                found.add(ALIASES[label])
            for candidate in FIELDS | set(ALIASES):
                if re.search(r"(?<!\w)" + re.escape(candidate.replace("_", " ")) + r"(?!\w)", line):
                    found.add(ALIASES.get(candidate, candidate))

    visit(details)
    visit(message)
    return tuple(sorted(found))


def is_duplicate_document_error(details: Any, message: str = "") -> bool:
    text = _fold(details) + " " + _fold(message)
    return bool(re.search(r"(?:cnpj|cpf|documento).{0,80}(?:ja existe|duplicad|ja cadastrad|already exists)", text)
                or re.search(r"(?:ja existe|duplicad|ja cadastrad|already exists).{0,80}(?:cnpj|cpf|documento)", text))
