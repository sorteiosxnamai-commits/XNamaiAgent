from __future__ import annotations

import unicodedata
from typing import Any


def fold_text(value: Any) -> str:
    text = str(value or "")
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text).lower()
        if not unicodedata.combining(char)
    )


def candidate_text(candidate: dict[str, Any]) -> str:
    fields = (
        "name",
        "brand",
        "model",
        "reference",
        "ean",
        "description",
        "category",
        "attributes",
        "color",
        "style",
    )
    return fold_text(" ".join(str(candidate.get(field) or "") for field in fields))


def candidate_price(candidate: dict[str, Any]) -> float | None:
    for key in ("current_price", "promotional_price", "price"):
        value = candidate.get(key)
        try:
            if value is not None:
                if isinstance(value, str):
                    text = value.replace("R$", "").strip()
                    text = (
                        text.replace(".", "").replace(",", ".")
                        if "," in text
                        else text
                    )
                    return float(text)
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def score_candidate(candidate: dict[str, Any], plan: dict[str, Any]) -> float:
    subject = plan.get("subject") or {}
    constraints = plan.get("constraints") or {}
    text = candidate_text(candidate)
    score = 0.0
    brand = fold_text(subject.get("brand") or (plan.get("filters") or {}).get("brand"))
    model = fold_text(subject.get("model") or (plan.get("filters") or {}).get("model"))
    reference = fold_text(
        subject.get("reference") or (plan.get("filters") or {}).get("reference")
    )
    ean = fold_text(subject.get("ean") or (plan.get("filters") or {}).get("ean"))
    query = fold_text(subject.get("query") or plan.get("query"))
    if brand:
        if brand not in text:
            return float("-inf")
        score += 300
    if model:
        model_tokens = [token for token in model.split() if len(token) > 1]
        if model_tokens and not all(token in text for token in model_tokens):
            return float("-inf")
        score += 500
    if reference and reference not in text:
        return float("-inf")
    if reference:
        score += 1000
    if ean and ean not in text:
        return float("-inf")
    if ean:
        score += 1200
    query_tokens = [token for token in query.split() if len(token) > 2]
    score += sum(50 for token in query_tokens if token in text)
    attributes = (
        constraints.get("attributes")
        or (plan.get("filters") or {}).get("attributes")
        or []
    )
    for attribute in attributes if isinstance(attributes, list) else [attributes]:
        if fold_text(attribute) in text:
            score += 40
    price = candidate_price(candidate)
    budget_max = (
        constraints.get("budget_max")
        or plan.get("budget_max")
        or (plan.get("filters") or {}).get("budget_max")
    )
    if budget_max is not None and price is not None:
        try:
            if price > float(budget_max):
                return float("-inf")
            score += 80
        except (TypeError, ValueError):
            pass
    return score


def rank_candidates(
    candidates: list[dict[str, Any]],
    plan: dict[str, Any],
    limit: int = 3,
) -> list[dict[str, Any]]:
    ranked = [
        (score_candidate(candidate, plan), candidate)
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    ranked = [
        (score, candidate)
        for score, candidate in ranked
        if score != float("-inf")
    ]
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in ranked[:limit]]
