"""Deterministic commercial truth policy for Story product answers.

Vision and LLMs never authorize price/stock. Tray/catalog evidence does.
Money is represented in integer cents — never float for authority.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProductEvidence(BaseModel):
    tenant_id: str
    product_id: str
    variant_id: str | None = None
    source: Literal[
        "confirmed_story_association",
        "exact_catalog_match",
        "tray_api",
        "catalog_database",
        "visual_candidate",
    ]
    source_record_id: str | None = None
    catalog_updated_at: datetime | None = None
    product_name: str = ""
    sku: str | None = None
    price_cents: int | None = None
    stock_quantity: int | None = None
    product_url: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    currency: str = "BRL"

    def authorizes_price(self) -> bool:
        return self.source in {"tray_api", "catalog_database"} and self.price_cents is not None

    def authorizes_stock(self) -> bool:
        return self.source == "tray_api" and self.stock_quantity is not None

    def authorizes_url(self) -> bool:
        return self.source in {"tray_api", "catalog_database"} and bool(self.product_url)


class CommercialValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def price_to_cents(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # Heuristic: values >= 1000 with no decimal often already reais*100 from adapters
        # that send integer reais — treat plain int as reais when small, else cents if tagged.
        return int(value) * 100
    try:
        # Accept decimal reais from Tray adapters as float/str, convert once to cents.
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(as_float * 100))


def evidence_from_tray_product(
    product: dict[str, Any],
    *,
    tenant_id: str,
    confidence: float = 1.0,
    source: Literal["tray_api", "catalog_database"] = "tray_api",
) -> ProductEvidence:
    pid = str(product.get("id") or product.get("product_id") or "").strip()
    if not pid:
        raise CommercialValidationError("product_id_missing")
    price = product.get("promotional_price")
    if price is None:
        price = product.get("price") or product.get("current_price")
    stock = product.get("stock")
    stock_qty = int(stock) if isinstance(stock, int) or (
        isinstance(stock, str) and str(stock).isdigit()
    ) else None
    if isinstance(stock, float) and stock.is_integer():
        stock_qty = int(stock)
    return ProductEvidence(
        tenant_id=str(tenant_id),
        product_id=pid,
        variant_id=(
            str(product["variant_id"])
            if product.get("variant_id") not in (None, "")
            else None
        ),
        source=source,
        source_record_id=pid,
        product_name=str(product.get("name") or product.get("title") or ""),
        sku=str(product["sku"]) if product.get("sku") not in (None, "") else None,
        price_cents=price_to_cents(price),
        stock_quantity=stock_qty,
        product_url=str(product["url"]) if product.get("url") else None,
        confidence=float(confidence),
    )


def validate_commercial_answer(
    proposed_answer: str,
    selected_product: dict[str, Any] | None,
    evidence: ProductEvidence | None,
    tenant_id: str,
    *,
    min_confidence: float = 0.65,
    allow_price: bool = True,
    allow_stock: bool = True,
) -> list[str]:
    """Return list of violation codes. Empty list = safe to send."""
    violations: list[str] = []
    text = proposed_answer or ""
    tid = str(tenant_id or "").strip()
    if not tid:
        violations.append("tenant_missing")
        return violations
    if evidence is None:
        if _mentions_price(text) or _mentions_stock(text):
            violations.append("commercial_claim_without_evidence")
        return violations
    if evidence.tenant_id != tid:
        violations.append("tenant_mismatch")
    if evidence.confidence < min_confidence and evidence.source == "visual_candidate":
        violations.append("confidence_below_threshold")
    if selected_product is not None:
        pid = str(selected_product.get("id") or selected_product.get("product_id") or "")
        if pid and pid != evidence.product_id:
            violations.append("product_mismatch")
        sel_tenant = str(selected_product.get("tenant_id") or tid)
        if sel_tenant != tid:
            violations.append("selected_product_tenant_mismatch")
        url = selected_product.get("url")
        if url and evidence.product_url and str(url) != evidence.product_url:
            violations.append("url_mismatch")
    if allow_price and _mentions_price(text):
        if not evidence.authorizes_price():
            violations.append("price_not_authorized")
        else:
            # Detect invented amounts: if answer contains an R$ value not matching cents.
            cents = evidence.price_cents
            if cents is not None and not _price_matches_text(text, cents):
                # Soft: only flag when a clear monetary amount is present and differs.
                if _extract_reais_cents(text) is not None:
                    violations.append("price_differs_from_evidence")
    if allow_stock and _mentions_affirmative_stock(text):
        if not evidence.authorizes_stock():
            violations.append("stock_not_authorized")
    if evidence.source == "visual_candidate" and (
        _mentions_price(text) or _mentions_affirmative_stock(text)
    ):
        violations.append("visual_only_commercial_forbidden")
    return violations


def _mentions_price(text: str) -> bool:
    lowered = text.casefold()
    return "r$" in lowered or "preço" in lowered or "preco" in lowered or "valor" in lowered


def _mentions_stock(text: str) -> bool:
    lowered = text.casefold()
    return any(
        token in lowered
        for token in ("estoque", "disponível", "disponivel", "indisponível", "indisponivel")
    )


def _mentions_affirmative_stock(text: str) -> bool:
    lowered = text.casefold()
    if "vou confirmar" in lowered or "não consegui confirmar" in lowered:
        return False
    return "está disponível" in lowered or "esta disponivel" in lowered or "em estoque" in lowered


def _extract_reais_cents(text: str) -> int | None:
    import re

    match = re.search(
        r"r\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)",
        text.casefold(),
    )
    if not match:
        return None
    raw = match.group(1).replace(".", "").replace(",", ".")
    try:
        return int(round(float(raw) * 100))
    except ValueError:
        return None


def _price_matches_text(text: str, price_cents: int) -> bool:
    found = _extract_reais_cents(text)
    if found is None:
        return True
    return found == int(price_cents)
