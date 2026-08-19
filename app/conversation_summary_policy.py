"""Criteria and safety for conversation summary deltas (Etapa 8).

Incremental merge only when criteria fire. Never persist sensitive data,
URLs, or volatile commercial facts (price/stock/freight/order status).
Summary is continuity context — not factual authority.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from .memory_models import ConversationSummaryDelta


class SummaryFact(BaseModel):
    field: str
    value: str
    expired_commercial: bool = False


class Preference(BaseModel):
    key: str
    value: str


class ProductReference(BaseModel):
    product_id: str | None = None
    variant_id: str | None = None
    label: str | None = None


class ConversationSummary(BaseModel):
    """Structured continuity summary — never factual authority for commerce."""

    confirmed_facts: list[SummaryFact] = Field(default_factory=list)
    customer_preferences: list[Preference] = Field(default_factory=list)
    products_presented: list[ProductReference] = Field(default_factory=list)
    confirmed_decisions: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    unresolved_hypotheses: list[str] = Field(default_factory=list)
    expired_commercial_references: list[str] = Field(default_factory=list)


_SENSITIVE_RE = re.compile(
    r"\b(cvv|cvc|cart[aã]o|password|senha|token|api[_-]?key|cpf)\b"
    r"|\b\d{13,19}\b",
    flags=re.IGNORECASE,
)
_INJECTION_RE = re.compile(
    r"\b(ignore|ignorar|desconsidere|override|system\s+message|"
    r"developer\s+message|ignore previous|finja que|mude as regras)\b"
    r"|<\s*script\b|javascript:|\bdrop\s+table\b|\bunion\s+select\b",
    flags=re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://|www\.", flags=re.IGNORECASE)
# Live commercial claims — preferences like "até 5000" without R$/estoque are OK.
_COMMERCIAL_VOLATILE_RE = re.compile(
    r"R\$\s*\d"
    r"|\b(estoque|esgotado|dispon[ií]vel|pronta\s+entrega)\b"
    r"|\bfrete\b"
    r"|\b(status|rastreio)\s+(do\s+)?pedido\b"
    r"|\bpedido\s*#?\s*\d{3,}\b"
    r"|\bpagamento\.php\b",
    flags=re.IGNORECASE,
)


def text_has_summary_safety_violation(text: str) -> list[str]:
    """Return rejection codes for a single summary field string."""
    value = (text or "").strip()
    if not value:
        return []
    codes: list[str] = []
    if _SENSITIVE_RE.search(value):
        codes.append("sensitive")
    if _INJECTION_RE.search(value):
        codes.append("prompt_injection")
    if _URL_RE.search(value):
        codes.append("url_blocked")
    if _COMMERCIAL_VOLATILE_RE.search(value):
        codes.append("commercial_volatile")
    return codes


def _scrub_text(value: str | None) -> tuple[str | None, list[str]]:
    if value is None:
        return None, []
    text = str(value).strip()
    if not text:
        return None, []
    codes = text_has_summary_safety_violation(text)
    if codes:
        return None, codes
    return text[:240], []


def _scrub_list(items: list[str] | None) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    codes: list[str] = []
    for item in items or []:
        cleaned, item_codes = _scrub_text(str(item) if item is not None else None)
        codes.extend(item_codes)
        if cleaned:
            kept.append(cleaned)
    return kept, codes


def sanitize_summary_delta(
    delta: ConversationSummaryDelta | None,
) -> tuple[ConversationSummaryDelta | None, list[str]]:
    """Strip unsafe fields. Returns (cleaned_delta_or_None, rejection_codes)."""
    if delta is None:
        return None, ["empty_delta"]

    codes: list[str] = []
    goal, goal_codes = _scrub_text(delta.current_goal)
    codes.extend(goal_codes)
    failure, fail_codes = _scrub_text(delta.last_failure)
    codes.extend(fail_codes)
    open_q, open_codes = _scrub_list(delta.open_questions)
    codes.extend(open_codes)
    resolved, resolved_codes = _scrub_list(delta.resolved_points)
    codes.extend(resolved_codes)
    corrections, corr_codes = _scrub_list(delta.user_corrections)
    codes.extend(corr_codes)
    commitments, commit_codes = _scrub_list(delta.commitments)
    codes.extend(commit_codes)

    cleaned = ConversationSummaryDelta(
        current_goal=goal,
        open_questions=open_q,
        resolved_points=resolved,
        user_corrections=corrections,
        commitments=commitments,
        last_failure=failure,
    )
    meaningful = bool(
        (cleaned.current_goal or "").strip()
        or cleaned.open_questions
        or cleaned.resolved_points
        or cleaned.user_corrections
        or cleaned.commitments
        or (cleaned.last_failure or "").strip()
    )
    if not meaningful:
        ordered: list[str] = []
        seen: set[str] = set()
        for code in [*codes, "empty_after_sanitize"]:
            if code not in seen:
                seen.add(code)
                ordered.append(code)
        return None, ordered

    ordered_codes: list[str] = []
    seen_codes: set[str] = set()
    for code in codes:
        if code not in seen_codes:
            seen_codes.add(code)
            ordered_codes.append(code)
    return cleaned, ordered_codes


def should_apply_summary_delta(
    delta: ConversationSummaryDelta | None,
    *,
    existing: dict[str, Any] | None = None,
) -> bool:
    """Return True only when the delta carries meaningful conversation progress."""
    if delta is None:
        return False
    if (delta.last_failure or "").strip():
        return True
    if any(str(item).strip() for item in (delta.user_corrections or [])):
        return True
    if any(str(item).strip() for item in (delta.commitments or [])):
        return True
    if any(str(item).strip() for item in (delta.resolved_points or [])):
        return True
    goal = (delta.current_goal or "").strip()
    if goal:
        previous = str((existing or {}).get("current_goal") or "").strip()
        if goal != previous:
            return True
    open_questions = [
        str(item).strip() for item in (delta.open_questions or []) if str(item).strip()
    ]
    if len(open_questions) >= 2:
        return True
    return False


def evaluate_summary_delta(
    delta: ConversationSummaryDelta | None,
    *,
    existing: dict[str, Any] | None = None,
) -> tuple[bool, ConversationSummaryDelta | None, list[str]]:
    """Full gate: sanitize → criteria. Returns (apply, cleaned_delta, codes)."""
    cleaned, scrub_codes = sanitize_summary_delta(delta)
    if cleaned is None:
        return False, None, scrub_codes or ["rejected"]
    if not should_apply_summary_delta(cleaned, existing=existing):
        return False, cleaned, ["criteria_not_met"]
    return True, cleaned, scrub_codes


def format_conversation_summary_block(row: dict[str, Any] | None) -> str:
    """Compact prompt block. Explicitly non-authoritative for commerce."""
    if not row:
        return (
            "<conversation_summary>\n"
            "</conversation_summary>"
        )
    summary = str(row.get("summary") or "").strip()
    goal = str(row.get("current_goal") or "").strip()
    open_q = row.get("open_questions") or []
    resolved = row.get("resolved_points") or []
    corrections = row.get("user_corrections") or []
    commitments = row.get("commitments") or []
    lines = [
        "<conversation_summary>",
        "Contexto de continuidade apenas. NÃO use como fonte de preço, estoque,",
        "frete, URL, pedido ou pagamento — esses fatos vêm só de tools/Tray/FACTS.",
    ]
    if goal:
        lines.append(f"- goal: {goal[:200]}")
    if summary:
        lines.append(f"- summary: {summary[:500]}")
    if isinstance(open_q, list) and open_q:
        lines.append(f"- open: {'; '.join(str(x)[:80] for x in open_q[:4])}")
    if isinstance(resolved, list) and resolved:
        lines.append(
            f"- resolved: {'; '.join(str(x)[:80] for x in resolved[:4])}"
        )
    if isinstance(corrections, list) and corrections:
        lines.append(
            f"- corrections: {'; '.join(str(x)[:80] for x in corrections[:3])}"
        )
    if isinstance(commitments, list) and commitments:
        lines.append(
            f"- commitments: {'; '.join(str(x)[:80] for x in commitments[:3])}"
        )
    lines.append("</conversation_summary>")
    return "\n".join(lines)


def compare_summary_delta_to_facts(
    delta: ConversationSummaryDelta | None,
    *,
    commercial_data: dict[str, Any] | None = None,
) -> list[str]:
    """Shadow-mode divergence codes — summary must not invent commerce facts."""
    if delta is None:
        return ["empty_delta"]
    divergences: list[str] = []
    blobs = [
        delta.current_goal or "",
        delta.last_failure or "",
        *list(delta.resolved_points or []),
        *list(delta.open_questions or []),
        *list(delta.commitments or []),
    ]
    for blob in blobs:
        codes = text_has_summary_safety_violation(str(blob))
        if "commercial_volatile" in codes:
            divergences.append("summary_asserts_commercial_fact")
        if "url_blocked" in codes:
            divergences.append("summary_asserts_url")
    products = (commercial_data or {}).get("products") if commercial_data else None
    if isinstance(products, list):
        for product in products:
            if not isinstance(product, dict):
                continue
            price = product.get("price") or product.get("current_price")
            if price is not None and any(
                str(price) in str(blob) for blob in blobs if blob
            ):
                divergences.append("summary_repeats_live_price")
    # Deduplicate while preserving order.
    ordered: list[str] = []
    seen: set[str] = set()
    for code in divergences:
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return ordered
