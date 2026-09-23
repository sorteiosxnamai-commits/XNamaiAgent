from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .agent_contracts import AgentDecision
from .fact_sources import (
    FactSource,
    StructuredFact,
    infer_source_for_payload_key,
)
from .fact_authority import (
    authorize_products_for_responder,
    filter_commerce_safe_evidence,
)
from .models import AgentResult


_URL_RE = re.compile(r"https?://[^\s<>()]+", flags=re.IGNORECASE)
_MONEY_RE = re.compile(
    r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})|[0-9]+(?:[.,][0-9]{1,2})?)",
    flags=re.IGNORECASE,
)
_ORDER_RE = re.compile(
    r"\bpedido(?:\s+n[ºo°.]*)?\s*#?\s*"
    r"((?=[A-Za-z0-9._/-]*\d)[A-Za-z0-9][A-Za-z0-9._/-]{1,})",
    flags=re.IGNORECASE,
)
_STOCK_POSITIVE_RE = re.compile(
    r"\b(em estoque|dispon[ií]vel|pronto para envio)\b",
    flags=re.IGNORECASE,
)
_STOCK_NEGATIVE_RE = re.compile(
    r"\b(esgotado|sem estoque|indispon[ií]vel)\b",
    flags=re.IGNORECASE,
)
_PROMO_RE = re.compile(
    r"\b(promo[cç][aã]o|desconto|oferta|por tempo limitado)\b",
    flags=re.IGNORECASE,
)
_PAID_RE = re.compile(
    r"\b(pago|pagamento (?:aprovado|confirmado)|pedido pago)\b",
    flags=re.IGNORECASE,
)
#: Commercial conditions and delivery promises: facts, not style. Each one must
#: be backed by the provider payload of this turn.
#: Needs installment CONTEXT: a bare "2x" also appears in product names
#: ("Kit 2x Cabo USB-C") and must not count as a payment condition.
_INSTALLMENT_RE = re.compile(
    r"(\bem\s+(?:at[eé]\s+)?\d{1,2}\s*x\b"
    r"|\b\d{1,2}\s*x\s+(?:de|sem|com)\b"
    r"|\b\d{1,2}\s+vezes\b"
    r"|\bparcel(?:a|as|ado|ada|amento)\b"
    r"|\bsem juros\b)",
    flags=re.IGNORECASE,
)
_FREE_SHIPPING_RE = re.compile(
    r"\b(frete gr[aá]tis|frete zero|entrega gr[aá]tis|sem custo de frete)\b",
    flags=re.IGNORECASE,
)
_PERCENT_DISCOUNT_RE = re.compile(
    r"\b\d{1,2}\s*%\s*(?:de\s+)?(?:desconto|off)\b",
    flags=re.IGNORECASE,
)
_IMMEDIATE_DELIVERY_RE = re.compile(
    r"\b(pronta entrega|entrega imediata|envio imediato|sai hoje)\b",
    flags=re.IGNORECASE,
)
_IMAGE_SENT_RE = re.compile(
    r"\b(segue|enviei|mandei|aqui est[aá]|estou enviando)\s+(?:a\s+|uma\s+)?(foto|imagem)\b",
    flags=re.IGNORECASE,
)
_INSTALLMENT_KEYS = ("installment", "parcel", "interest")
_URL_KEYS = ("url", "link", "checkout")
_ORDER_KEYS = ("order_id", "order_code", "pedido_id", "pedido_codigo")
_MONEY_KEYS = (
    "price",
    "total",
    "subtotal",
    "amount",
    "value",
    "installment",
)
_STOCK_KEYS = ("stock", "inventory", "available", "disponib")
_PROMO_KEYS = ("promotional", "promo", "discount", "sale_price")
_PAYMENT_STATUS_KEYS = ("payment_status", "order_payment_status", "status")


class FactClaim(BaseModel):
    kind: str
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class FactPack(BaseModel):
    trusted_urls: set[str] = Field(default_factory=set)
    order_ids: set[str] = Field(default_factory=set)
    monetary_values: set[Decimal] = Field(default_factory=set)
    stock_available: bool | None = None
    has_promotional_price: bool = False
    payment_confirmed: bool | None = None
    product_ids: set[str] = Field(default_factory=set)
    product_names: set[str] = Field(default_factory=set)
    evidence: list[StructuredFact] = Field(default_factory=list)
    source_payload: dict[str, Any] = Field(default_factory=dict, exclude=True)


class FactualViolation(BaseModel):
    kind: Literal[
        "url",
        "order_id",
        "money",
        "stock",
        "promo",
        "payment",
        "condition",
        "availability",
        "image",
        "product_mix",
        "other",
    ]
    claim: str
    reason: str


RiskLevel = Literal["low", "medium", "high", "critical"]


class FactualValidationReport(BaseModel):
    valid: bool = True
    mode: Literal["off", "shadow", "enforce"] = "shadow"
    risk_level: RiskLevel = "low"
    checked_claims: int = 0
    supported_claims: list[FactClaim] = Field(default_factory=list)
    unsupported_claims: list[FactClaim] = Field(default_factory=list)
    conflicting_claims: list[FactClaim] = Field(default_factory=list)
    missing_evidence: list[FactClaim] = Field(default_factory=list)
    violations: list[FactualViolation] = Field(default_factory=list)
    fallback_required: bool = False
    fallback_applied: bool = False
    evidence_count: int = 0
    evidence_sources: list[str] = Field(default_factory=list)
    evidence_preview: list[dict[str, Any]] = Field(default_factory=list)


def _clean_url(value: str) -> str:
    return value.rstrip(".,;:!?)]}\"'")


def _money_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace("R$", "").strip()
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _truthy_available(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value if isinstance(value, bool) else None
    if isinstance(value, (int, float, Decimal)):
        return float(value) > 0
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "sim", "available", "disponivel", "disponível"}:
        return True
    if text in {"0", "false", "no", "nao", "não", "unavailable", "esgotado"}:
        return False
    amount = _money_decimal(value)
    if amount is not None:
        return amount > 0
    return None


def _payment_confirmed(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().casefold()
    if any(token in text for token in ("approved", "paid", "pago", "aprovado")):
        return True
    if any(
        token in text
        for token in ("pending", "awaiting", "aguard", "unpaid", "rejected", "cancel")
    ):
        return False
    return None


def _append_evidence(
    pack: FactPack,
    *,
    source: FactSource,
    entity_type: str,
    key: str,
    value: Any,
    entity_id: str | None = None,
    confidence: float | None = None,
    tenant_id: str | None = None,
    revalidation_status: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    # Persona/memory must never enter commercial evidence bags.
    if entity_type in {
        "product",
        "variant",
        "price",
        "inventory",
        "url",
        "payment",
        "shipping",
        "cart",
    } and source in {FactSource.APPROVED_PERSONA, FactSource.CUSTOMER_MEMORY}:
        return
    pack.evidence.append(
        StructuredFact(
            source=source,
            entity_type=entity_type,  # type: ignore[arg-type]
            entity_id=entity_id,
            key=key,
            value=value,
            confidence=confidence,
            tenant_id=tenant_id,
            revalidation_status=revalidation_status,
            metadata=dict(metadata or {}),
        )
    )


def _collect_facts(
    value: Any,
    *,
    key: str = "",
    pack: FactPack,
    used_commerce_provider: bool = False,
    from_commerce_state: bool = False,
    entity_id: str | None = None,
    factual_source: str | None = None,
    revalidated: bool = False,
    tenant_id: str | None = None,
) -> None:
    if isinstance(value, dict):
        local_entity_id = entity_id
        for id_key in ("id", "product_id", "order_id", "variant_id"):
            if value.get(id_key) is not None:
                local_entity_id = str(value.get(id_key))
                break
        local_factual = (
            str(value.get("_factual_source") or factual_source or "").strip() or None
        )
        local_revalidated = bool(value.get("_revalidated")) or revalidated
        local_tenant = (
            str(value.get("tenant_id") or tenant_id or "").strip() or tenant_id
        )
        for child_key, child_value in value.items():
            if str(child_key).startswith("_"):
                continue
            _collect_facts(
                child_value,
                key=str(child_key).lower(),
                pack=pack,
                used_commerce_provider=used_commerce_provider,
                from_commerce_state=from_commerce_state,
                entity_id=local_entity_id,
                factual_source=local_factual,
                revalidated=local_revalidated,
                tenant_id=local_tenant,
            )
        name = value.get("name") or value.get("title") or value.get("product_name")
        if name:
            pack.product_names.add(str(name).strip().casefold())
            if local_entity_id:
                pack.product_ids.add(str(local_entity_id))
        return
    if isinstance(value, (list, tuple, set)):
        for child in value:
            _collect_facts(
                child,
                key=key,
                pack=pack,
                used_commerce_provider=used_commerce_provider,
                from_commerce_state=from_commerce_state,
                entity_id=entity_id,
                factual_source=factual_source,
                revalidated=revalidated,
                tenant_id=tenant_id,
            )
        return

    if value is None:
        return
    text = str(value).strip()
    source = infer_source_for_payload_key(
        key,
        used_commerce_provider=used_commerce_provider,
        from_commerce_state=from_commerce_state,
        factual_source=factual_source,
        revalidated=revalidated,
    )
    revalidation_status = (
        "revalidated"
        if revalidated or source == FactSource.COMMERCE_LIVE
        else ("pending" if source == FactSource.COMMERCE_PROVIDER else "not_applicable")
    )
    confidence = (
        0.95
        if source == FactSource.COMMERCE_LIVE
        else (0.75 if source == FactSource.COMMERCE_PROVIDER else 0.5)
    )
    entity_type = "other"
    handled = False
    if text and any(token in key for token in _URL_KEYS):
        entity_type = "url"
        for match in _URL_RE.findall(text):
            cleaned = _clean_url(match)
            pack.trusted_urls.add(cleaned)
            _append_evidence(
                pack,
                source=source,
                entity_type="url",
                key=key or "url",
                value=cleaned,
                entity_id=entity_id,
                confidence=confidence,
                tenant_id=tenant_id,
                revalidation_status=revalidation_status,
                metadata={"revalidated": revalidated},
            )
            handled = True
    if text and key in _ORDER_KEYS:
        entity_type = "order"
        pack.order_ids.add(text.casefold())
        _append_evidence(
            pack,
            source=source,
            entity_type="order",
            key=key,
            value=text,
            entity_id=text,
            confidence=confidence,
            tenant_id=tenant_id,
            revalidation_status=revalidation_status,
        )
        handled = True
    if any(token in key for token in _MONEY_KEYS):
        amount = _money_decimal(value)
        if amount is not None:
            entity_type = "price"
            pack.monetary_values.add(amount)
            _append_evidence(
                pack,
                source=source,
                entity_type="price",
                key=key,
                value=str(amount),
                entity_id=entity_id,
                confidence=confidence,
                tenant_id=tenant_id,
                revalidation_status=revalidation_status,
                metadata={"revalidated": revalidated},
            )
            if any(token in key for token in _PROMO_KEYS):
                pack.has_promotional_price = True
            handled = True
    if any(token in key for token in _STOCK_KEYS):
        available = _truthy_available(value)
        if available is not None:
            entity_type = "inventory"
            pack.stock_available = (
                available
                if pack.stock_available is None
                else (pack.stock_available or available)
            )
            _append_evidence(
                pack,
                source=source,
                entity_type="inventory",
                key=key,
                value=available,
                entity_id=entity_id,
                confidence=confidence,
                tenant_id=tenant_id,
                revalidation_status=revalidation_status,
                metadata={"revalidated": revalidated},
            )
            handled = True
    if any(token in key for token in _PAYMENT_STATUS_KEYS) and "payment" in key:
        confirmed = _payment_confirmed(value)
        if confirmed is not None:
            entity_type = "payment"
            pack.payment_confirmed = confirmed
            _append_evidence(
                pack,
                source=source,
                entity_type="payment",
                key=key,
                value=text,
                entity_id=entity_id,
                confidence=confidence,
                tenant_id=tenant_id,
                revalidation_status=revalidation_status,
            )
            handled = True
    if not handled and entity_type == "other" and key and text:
        _append_evidence(
            pack,
            source=source,
            entity_type="other",
            key=key,
            value=text,
            entity_id=entity_id,
            confidence=confidence,
            tenant_id=tenant_id,
            revalidation_status=revalidation_status,
        )


def build_fact_pack(
    result: AgentResult,
    *,
    commerce_state: dict[str, Any] | None = None,
) -> FactPack:
    metadata = result.response_metadata or {}
    used_commerce_provider = bool(metadata.get("used_commerce_provider"))
    source_payload = {
        "commercial_data": result.commercial_data or {},
        "verified_facts": metadata.get("verified_facts", {}),
        "outbound_image_url": metadata.get("outbound_image_url"),
    }
    pack = FactPack(source_payload=source_payload)
    _collect_facts(source_payload, pack=pack, used_commerce_provider=used_commerce_provider)

    payment = (result.commercial_data or {}).get("payment")
    if isinstance(payment, dict):
        status = payment.get("status") or payment.get("payment_status")
        confirmed = _payment_confirmed(status)
        if confirmed is not None:
            pack.payment_confirmed = confirmed
            _append_evidence(
                pack,
                source=FactSource.COMMERCE_PROVIDER if used_commerce_provider else FactSource.COMMERCE_STATE,
                entity_type="payment",
                key="payment.status",
                value=status,
                entity_id=str(
                    (result.commercial_data or {}).get("order_id") or ""
                )
                or None,
            )
        url = payment.get("payment_url") or payment.get("url")
        if url:
            cleaned = _clean_url(str(url))
            pack.trusted_urls.add(cleaned)

    if commerce_state:
        state_slice = {
            "order_id": commerce_state.get("order_id"),
            "order_lookup_id": commerce_state.get("order_lookup_id"),
            "order_payment_url": commerce_state.get("order_payment_url"),
            "order_payment_status": commerce_state.get("order_payment_status"),
            "pending_action": commerce_state.get("pending_action"),
            "purchase_stage": commerce_state.get("purchase_stage"),
            "active_product_id": commerce_state.get("active_product_id"),
            "active_product_name": commerce_state.get("active_product_name"),
        }
        _collect_facts(
            state_slice,
            pack=pack,
            used_commerce_provider=False,
            from_commerce_state=True,
        )
        state_payment = _payment_confirmed(commerce_state.get("order_payment_status"))
        if state_payment is not None and pack.payment_confirmed is None:
            pack.payment_confirmed = state_payment

    pack.evidence = filter_commerce_safe_evidence(pack.evidence)
    return pack


def _trusted_domain(url: str, trusted_domains: set[str]) -> bool:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in trusted_domains
    )


def _risk_from_violations(violations: list[FactualViolation]) -> RiskLevel:
    if not violations:
        return "low"
    kinds = {item.kind for item in violations}
    if "payment" in kinds or "order_id" in kinds:
        return "critical"
    if kinds & {"money", "url", "promo", "stock", "condition", "availability", "image"}:
        return "high"
    if "product_mix" in kinds:
        return "medium"
    return "medium"


def _add_violation(
    report: FactualValidationReport,
    *,
    kind: FactualViolation.__annotations__["kind"],
    claim: str,
    reason: str,
) -> None:
    violation = FactualViolation(kind=kind, claim=claim, reason=reason)
    report.violations.append(violation)
    unsupported = FactClaim(kind=kind, claim=claim, reason=reason)
    report.unsupported_claims.append(unsupported)
    if reason.endswith("_missing_evidence") or "not_present" in reason:
        report.missing_evidence.append(unsupported)


def _payload_values(payload: Any, key_tokens: tuple[str, ...]) -> list[Any]:
    """Every value whose key contains one of ``key_tokens`` (recursive)."""
    found: list[Any] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if any(token in str(key).casefold() for token in key_tokens):
                found.append(value)
            found.extend(_payload_values(value, key_tokens))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_payload_values(item, key_tokens))
    return found


def _has_value(values: list[Any]) -> bool:
    return any(value not in (None, "", [], {}, False) for value in values)


def _check_commercial_conditions(
    report: FactualValidationReport,
    *,
    text: str,
    pack: FactPack,
    result: AgentResult,
) -> None:
    """Conditions, delivery promises and "here is the photo" need evidence."""
    payload = pack.source_payload

    def _claim(kind: str, claim: str, supported: bool, reason: str) -> None:
        report.checked_claims += 1
        if supported:
            report.supported_claims.append(FactClaim(kind=kind, claim=claim, reason=f"{claim}_supported"))
        else:
            _add_violation(report, kind=kind, claim=claim, reason=reason)

    if _INSTALLMENT_RE.search(text):
        _claim(
            "condition",
            "installments",
            _has_value(_payload_values(payload, _INSTALLMENT_KEYS)),
            "installments_without_provider_evidence",
        )
    if _FREE_SHIPPING_RE.search(text):
        free = _payload_values(payload, ("free_shipping",))
        prices = _payload_values(payload.get("commercial_data", {}).get("shipping") or {}, ("price", "cost"))
        _claim(
            "condition",
            "free_shipping",
            _has_value(free) or any(_money_decimal(value) == 0 for value in prices),
            "free_shipping_without_provider_evidence",
        )
    if _PERCENT_DISCOUNT_RE.search(text):
        _claim(
            "promo",
            "percent_discount",
            pack.has_promotional_price,
            "percent_discount_without_promotional_price_evidence",
        )
    if _IMMEDIATE_DELIVERY_RE.search(text):
        supported = any(
            value is True for value in _payload_values(payload, ("immediate_delivery_supported",))
        )
        _claim("availability", "immediate_delivery", supported, "immediate_delivery_without_provider_evidence")
    if _IMAGE_SENT_RE.search(text):
        metadata = result.response_metadata or {}
        image = (result.commercial_data or {}).get("image")
        _claim(
            "image",
            "image_sent",
            bool(metadata.get("outbound_image_url") or image),
            "image_claimed_without_outbound_image",
        )


def validate_factual_response(
    result: AgentResult,
    *,
    decision: AgentDecision,
    mode: Literal["off", "shadow", "enforce"] = "shadow",
    trusted_domains: set[str] | None = None,
    commerce_state: dict[str, Any] | None = None,
) -> FactualValidationReport:
    report = FactualValidationReport(mode=mode)
    if mode == "off":
        return report

    pack = build_fact_pack(result, commerce_state=commerce_state)
    report.evidence_count = len(pack.evidence)
    report.evidence_sources = sorted(
        {item.source.value for item in pack.evidence}
    )
    report.evidence_preview = [
        {
            "source": item.source.value,
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "key": item.key,
            "confidence": item.confidence,
            "tenant_id": item.tenant_id,
            "revalidation_status": item.revalidation_status,
            "retrieved_at": item.retrieved_at.isoformat(),
        }
        for item in pack.evidence[:40]
    ]
    # Sem allowlist configurada nenhum domínio é confiável: fail-closed. O
    # fallback anterior confiava nos domínios da marca legada, o que deixaria
    # links dela passarem por fato oficial mesmo sem fonte comercial.
    domains = {
        domain.lower().strip()
        for domain in (trusted_domains or set())
        if domain.strip()
    }
    text = result.reply_text or ""

    # Public entry points are institutional facts; product/payment URLs still
    # require current tool evidence. Do not trust every path on these domains.
    from .site_knowledge import official_public_urls
    institutional_urls = {url.rstrip("/") for url in official_public_urls()}

    for raw_url in _URL_RE.findall(text):
        url = _clean_url(raw_url)
        report.checked_claims += 1
        if url.rstrip("/") in institutional_urls or url in pack.trusted_urls or _trusted_domain(url, domains):
            report.supported_claims.append(
                FactClaim(kind="url", claim=url, reason="url_supported")
            )
        else:
            _add_violation(
                report,
                kind="url",
                claim=url,
                reason="url_not_present_in_verified_facts",
            )

    order_claims = _ORDER_RE.findall(text)
    if (
        order_claims
        and "transactional_facts" in decision.risk.required_validations
    ):
        for order_id in order_claims:
            report.checked_claims += 1
            if order_id.casefold() in pack.order_ids:
                report.supported_claims.append(
                    FactClaim(
                        kind="order_id",
                        claim=order_id,
                        reason="order_id_supported",
                    )
                )
            else:
                _add_violation(
                    report,
                    kind="order_id",
                    claim=order_id,
                    reason="order_id_not_present_in_verified_facts",
                )

    validate_money = bool(
        {"catalog_facts", "transactional_facts"}.intersection(
            decision.risk.required_validations
        )
    )
    if validate_money and decision.domain == "commerce":
        # Only trust commerce-safe monetary evidence (never persona/memory).
        safe_money = {
            _money_decimal(item.value)
            for item in pack.evidence
            if item.entity_type == "price"
            and item.source
            not in {FactSource.APPROVED_PERSONA, FactSource.CUSTOMER_MEMORY}
        }
        safe_money.discard(None)
        trusted_amounts = safe_money or pack.monetary_values
        for amount_text in _MONEY_RE.findall(text):
            amount = _money_decimal(amount_text)
            if amount is None:
                continue
            report.checked_claims += 1
            matching = [
                item
                for item in pack.evidence
                if item.entity_type == "price"
                and _money_decimal(item.value) == amount
                and item.source
                not in {FactSource.APPROVED_PERSONA, FactSource.CUSTOMER_MEMORY}
            ]
            if amount in trusted_amounts and matching:
                best = matching[0]
                report.supported_claims.append(
                    FactClaim(
                        kind="money",
                        claim=str(amount),
                        evidence_ids=[best.entity_id] if best.entity_id else [],
                        reason=(
                            "money_supported_revalidated"
                            if best.revalidation_status == "revalidated"
                            or best.source == FactSource.COMMERCE_LIVE
                            else "money_supported"
                        ),
                    )
                )
            elif amount in trusted_amounts:
                report.supported_claims.append(
                    FactClaim(
                        kind="money",
                        claim=str(amount),
                        reason="money_supported",
                    )
                )
            else:
                _add_violation(
                    report,
                    kind="money",
                    claim=str(amount),
                    reason="money_not_present_in_verified_facts",
                )

    if _PROMO_RE.search(text) and decision.domain == "commerce":
        report.checked_claims += 1
        if pack.has_promotional_price:
            report.supported_claims.append(
                FactClaim(kind="promo", claim="promo", reason="promo_supported")
            )
        else:
            _add_violation(
                report,
                kind="promo",
                claim="promo",
                reason="promo_without_promotional_price_evidence",
            )

    if pack.stock_available is not None and decision.domain == "commerce":
        if _STOCK_POSITIVE_RE.search(text):
            report.checked_claims += 1
            if pack.stock_available:
                report.supported_claims.append(
                    FactClaim(
                        kind="stock",
                        claim="available",
                        reason="stock_supported",
                    )
                )
            else:
                _add_violation(
                    report,
                    kind="stock",
                    claim="available",
                    reason="stock_claim_conflicts_with_evidence",
                )
                report.conflicting_claims.append(
                    FactClaim(
                        kind="stock",
                        claim="available",
                        reason="stock_claim_conflicts_with_evidence",
                    )
                )
        if _STOCK_NEGATIVE_RE.search(text):
            report.checked_claims += 1
            if not pack.stock_available:
                report.supported_claims.append(
                    FactClaim(
                        kind="stock",
                        claim="unavailable",
                        reason="stock_supported",
                    )
                )
            else:
                _add_violation(
                    report,
                    kind="stock",
                    claim="unavailable",
                    reason="stock_claim_conflicts_with_evidence",
                )
                report.conflicting_claims.append(
                    FactClaim(
                        kind="stock",
                        claim="unavailable",
                        reason="stock_claim_conflicts_with_evidence",
                    )
                )

    if _PAID_RE.search(text) and decision.domain == "commerce":
        report.checked_claims += 1
        if pack.payment_confirmed is True:
            report.supported_claims.append(
                FactClaim(
                    kind="payment",
                    claim="paid",
                    reason="payment_supported",
                )
            )
        elif pack.payment_confirmed is False:
            _add_violation(
                report,
                kind="payment",
                claim="paid",
                reason="payment_confirmed_without_factual_signal",
            )
            report.conflicting_claims.append(
                FactClaim(
                    kind="payment",
                    claim="paid",
                    reason="payment_confirmed_without_factual_signal",
                )
            )
        else:
            _add_violation(
                report,
                kind="payment",
                claim="paid",
                reason="payment_confirmed_missing_evidence",
            )

    if decision.domain == "commerce":
        _check_commercial_conditions(report, text=text, pack=pack, result=result)

    if len(pack.product_ids) >= 2 and decision.domain == "commerce":
        mentioned = [
            name
            for name in pack.product_names
            if name and name in text.casefold()
        ]
        if len(mentioned) >= 2 and len(pack.monetary_values) >= 2:
            # Soft signal: multiple products + multiple prices in one reply.
            report.checked_claims += 1
            report.conflicting_claims.append(
                FactClaim(
                    kind="product_mix",
                    claim=",".join(sorted(mentioned)[:3]),
                    reason="multiple_products_and_prices_in_reply",
                )
            )

    report.valid = not report.violations
    report.risk_level = _risk_from_violations(report.violations)
    report.fallback_required = (not report.valid) and report.risk_level in {
        "high",
        "critical",
    }
    return report


def apply_factual_validation(
    result: AgentResult,
    *,
    decision: AgentDecision,
    mode: Literal["off", "shadow", "enforce"] = "shadow",
    trusted_domains: set[str] | None = None,
    commerce_state: dict[str, Any] | None = None,
) -> AgentResult:
    report = validate_factual_response(
        result,
        decision=decision,
        mode=mode,
        trusted_domains=trusted_domains,
        commerce_state=commerce_state,
    )
    if mode == "enforce" and report.fallback_required and report.violations:
        fallback = str(
            (result.response_metadata or {}).get("factual_fallback_text")
            or ""
        ).strip()
        result.reply_text = fallback or (
            "Não consegui validar com segurança todos os dados desta resposta. "
            "Vou consultar novamente antes de confirmar."
        )
        result.reply_modality = "text"
        result.reply_audio_bytes = None
        result.reply_audio_mime_type = None
        result.reply_audio_url = None
        result.safety_reason = "factual_validation_failed"
        report.fallback_applied = True

    # Authorize commercial products for any downstream composer / metrics.
    products = (result.commercial_data or {}).get("products")
    if isinstance(products, list) and products:
        tenant_id = str(
            (result.response_metadata or {}).get("tenant_id")
            or getattr(decision, "tenant_id", None)
            or "xnamai"
        )
        authorized, grounded = authorize_products_for_responder(
            [p for p in products if isinstance(p, dict)],
            tenant_id=tenant_id,
        )
        result.commercial_data = dict(result.commercial_data or {})
        result.commercial_data["products"] = authorized
        result.response_metadata["grounded_commerce_evidence"] = [
            row.model_dump(mode="json") for row in grounded[:40]
        ]
        result.response_metadata["grounded_commerce_count"] = len(grounded)

    payload = report.model_dump(mode="json")
    result.response_metadata["factual_validation"] = payload
    result.response_metadata["fact_evidence"] = list(report.evidence_preview)
    return result
