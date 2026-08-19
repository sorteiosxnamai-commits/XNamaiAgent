"""Canonical catalog items + hybrid candidate ranking (Etapa 4).

The LLM never searches the catalog freely. It may only rerank IDs that already
exist in a deterministic candidate pool. Commercial facts are revalidated from
Tray before display (see ``revalidate_products``).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import get_settings
from .models import SalesInterpretation

FactualSource = Literal[
    "tray_live",
    "tray_search",
    "catalog_cache",
    "catalog_index",
    "conversation_ref",
]


class CanonicalCatalogItem(BaseModel):
    tenant_id: str = "newstore"
    catalog_item_key: str = ""
    product_id: str
    variant_id: str | None = None
    sku: str | None = None
    ean: str | None = None
    reference: str | None = None
    brand: str | None = None
    collection: str | None = None
    model: str | None = None
    title_normalized: str = ""
    category: str | None = None
    gender: str | None = None
    mechanism: str | None = None
    case_size: str | None = None
    dial_color: str | None = None
    strap_color: str | None = None
    material: str | None = None
    strap_type: str | None = None
    colors_normalized: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    price: float | None = None
    promotional_price: float | None = None
    stock: int | None = None
    available: bool | None = None
    available_in_store: bool | None = None
    url: str | None = None
    image_url: str | None = None
    freshness_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    factual_source: FactualSource = "tray_search"
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class ProductCandidate(BaseModel):
    product_id: str
    variant_id: str | None = None
    score: float = 0.0
    exact_matches: list[str] = Field(default_factory=list)
    soft_matches: list[str] = Field(default_factory=list)
    mismatches: list[str] = Field(default_factory=list)
    exclusion_reason: str | None = None
    factual_source: str = "tray_search"
    freshness_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    score_explanation: str = ""
    product: dict[str, Any] = Field(default_factory=dict, exclude=True)


class CandidateTrace(BaseModel):
    """Internal scoring trace — observability only, never customer-facing."""

    catalog_item_key: str
    initial_score: float = 0.0
    exact_matches: list[str] = Field(default_factory=list)
    hard_constraints_passed: list[str] = Field(default_factory=list)
    hard_constraints_failed: list[str] = Field(default_factory=list)
    soft_preferences_matched: list[str] = Field(default_factory=list)
    soft_preferences_missed: list[str] = Field(default_factory=list)
    score_components: dict[str, float] = Field(default_factory=dict)
    excluded: bool = False
    exclusion_reason: str | None = None


def _fold(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _tokens(value: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", _fold(value))


def trigram_similarity(left: str, right: str) -> float:
    """Lightweight trigram Jaccard (no DB extension required)."""
    a = _fold(left)
    b = _fold(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) < 3 or len(b) < 3:
        return 1.0 if a in b or b in a else 0.0

    def grams(text: str) -> set[str]:
        padded = f"  {text} "
        return {padded[i : i + 3] for i in range(len(padded) - 2)}

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def effective_price(product: dict[str, Any]) -> float | None:
    for key in ("promotional_price", "current_price", "price"):
        value = product.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def to_canonical_item(
    product: dict[str, Any],
    *,
    tenant_id: str | None = None,
    factual_source: FactualSource = "tray_search",
    freshness_at: datetime | None = None,
) -> CanonicalCatalogItem | None:
    if not isinstance(product, dict) or product.get("id") is None:
        return None
    settings = get_settings()
    title = str(product.get("name") or "")
    brand = product.get("brand")
    model = product.get("model")
    color = product.get("color")
    colors = [_fold(color)] if color else []
    # Harvest color-ish tokens from title for alias matching later.
    for token in _tokens(title):
        if token in {
            "azul",
            "blue",
            "preto",
            "black",
            "branco",
            "white",
            "rosa",
            "pink",
            "verde",
            "green",
            "vermelho",
            "red",
            "dourado",
            "gold",
            "prata",
            "silver",
        }:
            if token not in colors:
                colors.append(token)
    promo = product.get("promotional_price")
    price = effective_price(product)
    try:
        promo_f = float(promo) if promo is not None and promo != "" else None
    except (TypeError, ValueError):
        promo_f = None
    stock_raw = product.get("stock")
    try:
        stock = int(stock_raw) if stock_raw is not None else None
    except (TypeError, ValueError):
        stock = None
    item = CanonicalCatalogItem(
        tenant_id=(
            tenant_id
            or getattr(settings, "agent_persona_tenant_id", None)
            or "newstore"
        ),
        product_id=str(product.get("id")),
        variant_id=(
            str(product.get("variant_id"))
            if product.get("variant_id") is not None
            else None
        ),
        catalog_item_key="",  # filled below
        sku=(
            str(product.get("sku"))
            if product.get("sku") is not None
            else None
        ),
        ean=(
            str(product.get("ean"))
            if product.get("ean") is not None
            else None
        ),
        reference=(
            str(product.get("reference"))
            if product.get("reference") is not None
            else None
        ),
        brand=str(brand) if brand else None,
        collection=(
            str(product.get("collection"))
            if product.get("collection")
            else None
        ),
        model=str(model) if model else None,
        title_normalized=_fold(title),
        category=str(
            product.get("category") or product.get("category_name") or ""
        )
        or None,
        gender=None,
        mechanism=None,
        case_size=None,
        dial_color=str(color) if color else None,
        strap_color=None,
        material=(
            str(product.get("material"))
            if product.get("material")
            else None
        ),
        strap_type=None,
        colors_normalized=[c for c in colors if c],
        aliases=[],
        price=price,
        promotional_price=promo_f,
        stock=stock,
        available=(
            bool(product.get("available"))
            if product.get("available") is not None
            else None
        ),
        available_in_store=(
            bool(product.get("available_in_store"))
            if product.get("available_in_store") is not None
            else None
        ),
        url=str(product.get("url")) if product.get("url") else None,
        image_url=str(
            product.get("primary_image_url") or product.get("image_url") or ""
        )
        or None,
        freshness_at=freshness_at or datetime.now(timezone.utc),
        factual_source=factual_source,
        raw=product,
    )
    from .fact_authority import catalog_item_key_for

    item.catalog_item_key = catalog_item_key_for(item.product_id, item.variant_id)
    return item


def _hard_constraints_from_interpretation(
    interpretation: SalesInterpretation,
) -> dict[str, Any]:
    """Merge TurnUnderstanding hard constraints with SalesInterpretation subject."""
    subject = interpretation.subject
    prefs = interpretation.preferences
    try:
        from .product_retrieval import effective_product_reference

        safe_reference = effective_product_reference(subject.reference)
    except Exception:
        safe_reference = subject.reference
    hard: dict[str, Any] = {
        "brand": subject.brand,
        "model": subject.model,
        "reference": safe_reference,
        "ean": subject.ean,
        "category": subject.product_type,
        "budget_min": prefs.budget_min,
        "budget_max": prefs.budget_max,
        "brand_exclusive": False,
        "exact_only": False,
        "dial_color": None,
        "gender": None,
        "material": None,
        "must_match_fields": [],
    }
    attrs = [str(item) for item in (prefs.attributes or [])]
    if any(item.startswith("somente") for item in attrs) or "somente" in attrs:
        hard["exact_only"] = True
        hard["brand_exclusive"] = bool(subject.brand)
    for item in attrs:
        if item.startswith("somente:") and len(item) > 8:
            hard["brand"] = item.split(":", 1)[1]
            hard["brand_exclusive"] = True
            hard["exact_only"] = True
    try:
        from .turn_understanding import get_turn_understanding

        turn = get_turn_understanding(interpretation)
        if turn is not None:
            hc = turn.hard_constraints
            try:
                from .product_retrieval import effective_product_reference

                turn_ref = effective_product_reference(hc.reference)
            except Exception:
                turn_ref = hc.reference
            hard.update(
                {
                    "brand": hc.brand or hard["brand"],
                    "model": hc.model or hard["model"],
                    "reference": turn_ref or hard["reference"],
                    "ean": hc.ean or hard["ean"],
                    "category": hc.category or hard["category"],
                    "budget_min": hc.budget_min
                    if hc.budget_min is not None
                    else hard["budget_min"],
                    "budget_max": hc.budget_max
                    if hc.budget_max is not None
                    else hard["budget_max"],
                    "brand_exclusive": hard["brand_exclusive"] or hc.brand_exclusive,
                    "exact_only": hard["exact_only"] or hc.exact_only,
                    "dial_color": hc.dial_color or hc.strap_color,
                    "gender": hc.gender,
                    "material": hc.material,
                    "must_match_fields": list(hc.must_match_fields or []),
                }
            )
    except Exception:
        pass
    return hard


def evaluate_hard_constraints(
    item: CanonicalCatalogItem,
    hard: dict[str, Any],
    *,
    mode: Literal["exact", "recommendation"] = "recommendation",
) -> tuple[bool, str | None, list[str]]:
    """Return (ok, exclusion_reason, exact_matches)."""
    exact: list[str] = []
    text = " ".join(
        part
        for part in (
            item.title_normalized,
            _fold(item.brand),
            _fold(item.model),
            _fold(item.reference),
            _fold(item.ean),
            " ".join(item.colors_normalized),
        )
        if part
    )

    expected_brand = _fold(hard.get("brand"))
    if expected_brand:
        candidate_brand = _fold(item.brand)
        if candidate_brand and candidate_brand != expected_brand:
            return False, "brand_mismatch", exact
        if not candidate_brand and expected_brand not in text:
            return False, "brand_missing", exact
        if candidate_brand == expected_brand:
            exact.append("brand")

    if hard.get("brand_exclusive") and expected_brand:
        candidate_brand = _fold(item.brand)
        if candidate_brand != expected_brand:
            return False, "brand_exclusive_mismatch", exact

    expected_ref = _fold(hard.get("reference"))
    if expected_ref:
        if _fold(item.reference) != expected_ref:
            return False, "reference_mismatch", exact
        exact.append("reference")

    expected_ean = _fold(hard.get("ean"))
    if expected_ean:
        if _fold(item.ean) != expected_ean:
            return False, "ean_mismatch", exact
        exact.append("ean")

    expected_sku = _fold(hard.get("sku"))
    if expected_sku:
        if _fold(item.sku) != expected_sku:
            return False, "sku_mismatch", exact
        exact.append("sku")

    budget_max = hard.get("budget_max")
    if budget_max is not None:
        price = item.promotional_price if item.promotional_price is not None else item.price
        if price is None or float(price) > float(budget_max):
            return False, "over_budget", exact
        exact.append("budget_max")

    budget_min = hard.get("budget_min")
    if budget_min is not None:
        price = item.promotional_price if item.promotional_price is not None else item.price
        if price is None or float(price) < float(budget_min):
            return False, "under_budget", exact

    # exact_only / "somente": color & material become hard when provided.
    if hard.get("exact_only") or mode == "exact":
        dial = _fold(hard.get("dial_color"))
        if dial:
            color_blob = " ".join(item.colors_normalized) + " " + text
            try:
                from .product_retrieval import expand_color_aliases

                aliases = expand_color_aliases(dial)
            except Exception:
                aliases = frozenset({dial})
            if not any(alias in color_blob for alias in aliases):
                return False, "color_hard_mismatch", exact
            exact.append("color")
        material = _fold(hard.get("material"))
        if material and material not in text and material not in _fold(item.material):
            return False, "material_hard_mismatch", exact

    if mode == "recommendation" and item.available is False:
        return False, "unavailable", exact

    return True, None, exact


def score_soft_preferences(
    item: CanonicalCatalogItem,
    interpretation: SalesInterpretation,
) -> tuple[float, list[str], list[str], str]:
    """Soft ranking — never excludes alone."""
    prefs = interpretation.preferences
    soft_matches: list[str] = []
    mismatches: list[str] = []
    score = 0.0
    text = item.title_normalized
    parts: list[str] = []

    # Exact identity boosts already counted elsewhere; soft lexical overlap.
    query_bits = [
        interpretation.subject.model,
        prefs.style,
        prefs.color,
        prefs.material,
        prefs.occasion,
        prefs.recipient,
        *(prefs.attributes or []),
    ]
    for bit in query_bits:
        folded = _fold(bit)
        if not folded or folded.startswith("somente"):
            continue
        if folded in text or any(folded == c for c in item.colors_normalized):
            score += 1.0
            soft_matches.append(folded)
            parts.append(f"+1 lexical:{folded}")
        else:
            # trigram soft signal
            sim = trigram_similarity(folded, text)
            if sim >= 0.35:
                score += sim
                soft_matches.append(f"trigram:{folded}:{sim:.2f}")
                parts.append(f"+{sim:.2f} trigram:{folded}")
            elif bit in (prefs.color, prefs.style, prefs.material) and bit:
                mismatches.append(folded)

    try:
        from .preference_normalize import preference_gender_label
        from .product_retrieval import (
            expand_color_aliases,
            preference_color_tokens,
            preference_gender_tokens,
            product_matches_color_tokens,
            product_matches_gender_tokens,
        )

        gender_tokens = preference_gender_tokens(interpretation)
        if gender_tokens and product_matches_gender_tokens(item.raw or {}, gender_tokens):
            score += 3.0
            soft_matches.append("gender")
            parts.append("+3 gender")
        color_tokens = preference_color_tokens(interpretation)
        if color_tokens and product_matches_color_tokens(item.raw or {}, color_tokens):
            score += 4.0
            soft_matches.append("color")
            parts.append("+4 color")
        elif color_tokens:
            # Try aliases against normalized colors
            blob = " ".join(item.colors_normalized) + " " + text
            hit = False
            for token in color_tokens:
                for alias in expand_color_aliases(token):
                    if alias in blob:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                score += 4.0
                soft_matches.append("color_alias")
                parts.append("+4 color_alias")
            else:
                mismatches.append("color")
                # Soft penalty only — unless exact_only handled in hard path.
                score -= 0.5
                parts.append("-0.5 color_miss")
        _ = preference_gender_label
    except Exception:
        pass

    # Brand soft preference (non-exclusive): mild boost / penalty.
    expected_brand = _fold(interpretation.subject.brand)
    if expected_brand:
        if _fold(item.brand) == expected_brand:
            score += 2.0
            soft_matches.append("brand")
            parts.append("+2 brand")
        elif item.brand:
            score -= 2.0
            mismatches.append("brand")
            parts.append("-2 brand_divergent")

    # Budget soft: already hard-filtered usually; small penalty near miss unused.
    explanation = "; ".join(parts) if parts else "no_soft_signal"
    return score, soft_matches, mismatches, explanation


def hybrid_rank_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    mode: Literal["exact", "recommendation"] = "recommendation",
    factual_source: FactualSource = "tray_search",
    limit: int | None = None,
) -> list[ProductCandidate]:
    """
    Priority within an already-fetched pool:

    1. Exact identity (EAN/SKU/reference/ID already filtered upstream)
    2. Lexical / token overlap
    3. Color aliases (via soft scorer)
    4. Trigram similarity
    5. Soft preference boosts / brand penalties

    Hard constraints exclude. Soft preferences only score.
    """
    hard = _hard_constraints_from_interpretation(interpretation)
    settings = get_settings()
    pool_limit = limit
    if pool_limit is None:
        try:
            pool_limit = int(
                getattr(settings, "agent_candidate_pool_limit", 20) or 20
            )
        except (TypeError, ValueError):
            pool_limit = 20

    kept: list[ProductCandidate] = []
    excluded_log: list[dict[str, Any]] = []
    traces: list[CandidateTrace] = []

    for product in products:
        item = to_canonical_item(product, factual_source=factual_source)
        if item is None:
            continue
        ok, reason, exact_matches = evaluate_hard_constraints(
            item, hard, mode=mode
        )
        soft_score, soft_matches, mismatches, explanation = score_soft_preferences(
            item, interpretation
        )
        soft_missed = [m for m in mismatches if m]
        components = {
            "exact_boost": float(len(exact_matches) * 10.0),
            "soft_score": float(soft_score),
        }
        score = components["exact_boost"] + components["soft_score"]
        trace = CandidateTrace(
            catalog_item_key=item.catalog_item_key
            or (
                f"variant:{item.variant_id}"
                if item.variant_id
                else f"product:{item.product_id}"
            ),
            initial_score=score,
            exact_matches=list(exact_matches),
            hard_constraints_passed=list(exact_matches) if ok else [],
            hard_constraints_failed=[reason] if not ok and reason else [],
            soft_preferences_matched=list(soft_matches),
            soft_preferences_missed=soft_missed,
            score_components=components,
            excluded=not ok,
            exclusion_reason=reason if not ok else None,
        )
        traces.append(trace)
        if not ok:
            excluded_log.append(
                {"product_id": item.product_id, "reason": reason}
            )
            continue
        if exact_matches:
            explanation = (
                f"exact={exact_matches}; {explanation}"
                if explanation
                else f"exact={exact_matches}"
            )
        kept.append(
            ProductCandidate(
                product_id=item.product_id,
                variant_id=item.variant_id,
                score=score,
                exact_matches=exact_matches,
                soft_matches=soft_matches,
                mismatches=mismatches,
                factual_source=item.factual_source,
                freshness_at=item.freshness_at,
                score_explanation=explanation,
                product=product,
            )
        )

    kept.sort(key=lambda c: (-c.score, c.product_id))
    if excluded_log:
        print(
            "[catalog.hybrid.excluded]",
            {
                "count": len(excluded_log),
                "sample": excluded_log[:8],
                "mode": mode,
            },
        )
    print(
        "[catalog.hybrid.ranked]",
        {
            "input": len(products),
            "kept": len(kept),
            "excluded": len(excluded_log),
            "top_score": kept[0].score if kept else None,
            "mode": mode,
            "traces_sample": [
                t.model_dump(mode="json")
                for t in traces[:5]
            ],
        },
    )
    return kept[: max(1, pool_limit)]


def hybrid_rank_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    mode: Literal["exact", "recommendation"] = "recommendation",
    factual_source: FactualSource = "tray_search",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return Tray product dicts in hybrid order, with retrieval metadata attached."""
    candidates = hybrid_rank_candidates(
        products,
        interpretation,
        mode=mode,
        factual_source=factual_source,
        limit=limit,
    )
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        product = dict(candidate.product or {})
        catalog_key = (
            f"variant:{candidate.variant_id}"
            if candidate.variant_id
            else f"product:{candidate.product_id}"
        )
        product["_catalog_item_key"] = catalog_key
        product["_retrieval"] = {
            "score": candidate.score,
            "exact_matches": list(candidate.exact_matches),
            "soft_matches": list(candidate.soft_matches),
            "mismatches": list(candidate.mismatches),
            "score_explanation": candidate.score_explanation,
            "factual_source": candidate.factual_source,
            "freshness_at": candidate.freshness_at.isoformat(),
            "catalog_item_key": catalog_key,
            "candidate_trace": {
                "catalog_item_key": catalog_key,
                "initial_score": candidate.score,
                "exact_matches": list(candidate.exact_matches),
                "soft_preferences_matched": list(candidate.soft_matches),
                "soft_preferences_missed": list(candidate.mismatches),
                "excluded": False,
                "exclusion_reason": None,
            },
        }
        ranked.append(product)
    return ranked


def reject_unknown_rerank_ids(
    selected_ids: list[str],
    allowed_ids: set[str],
    *,
    limit: int,
) -> tuple[list[str], int]:
    """Keep only IDs present in the candidate pool; count inventions."""
    ordered: list[str] = []
    seen: set[str] = set()
    invalid = 0
    for raw in selected_ids:
        normalized = str(raw)
        if normalized in seen:
            continue
        seen.add(normalized)
        if normalized not in allowed_ids:
            invalid += 1
            continue
        ordered.append(normalized)
        if len(ordered) >= limit:
            break
    return ordered, invalid


def build_allowed_id_sets(
    products: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Closed sets the LLM may reference — never invent outside these."""
    product_ids: set[str] = set()
    variant_ids: set[str] = set()
    catalog_keys: set[str] = set()
    for product in products:
        if not isinstance(product, dict) or product.get("id") is None:
            continue
        pid = str(product["id"])
        product_ids.add(pid)
        vid = product.get("variant_id")
        if vid is not None and str(vid).strip():
            variant_ids.add(str(vid))
            catalog_keys.add(f"variant:{vid}")
        else:
            catalog_keys.add(f"product:{pid}")
        key = product.get("catalog_item_key") or product.get("_catalog_item_key")
        if key:
            catalog_keys.add(str(key))
    return {
        "allowed_product_ids": product_ids,
        "allowed_variant_ids": variant_ids,
        "allowed_catalog_item_keys": catalog_keys,
    }


def filter_products_to_allowed(
    products: list[dict[str, Any]],
    allowed: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], int]:
    """Drop products / variants outside the closed evidence set."""
    allowed_pids = allowed.get("allowed_product_ids") or set()
    allowed_vids = allowed.get("allowed_variant_ids") or set()
    kept: list[dict[str, Any]] = []
    rejected = 0
    for product in products:
        if not isinstance(product, dict) or product.get("id") is None:
            rejected += 1
            continue
        pid = str(product["id"])
        if pid not in allowed_pids:
            rejected += 1
            continue
        vid = product.get("variant_id")
        if (
            vid is not None
            and str(vid).strip()
            and allowed_vids
            and str(vid) not in allowed_vids
        ):
            rejected += 1
            continue
        kept.append(product)
    return kept, rejected


def upsert_canonical_items(items: list[CanonicalCatalogItem]) -> int:
    """Best-effort durable index write (no-op without database)."""
    if not items:
        return 0
    try:
        from .db import ensure_tables, get_conn, to_jsonb

        ensure_tables()
        written = 0
        with get_conn() as conn:
            with conn.cursor() as cur:
                for item in items:
                    payload = item.model_dump(mode="json", exclude={"raw"})
                    cur.execute(
                        """
                        INSERT INTO public.ai_catalog_index (
                            tenant_id, catalog_item_key, product_id, variant_id, sku, ean, reference,
                            brand, collection, model, title_normalized, category,
                            gender, mechanism, case_size, dial_color, strap_color,
                            material, strap_type, colors_normalized, aliases,
                            price, promotional_price, stock, available,
                            available_in_store, url, image_url, freshness_at,
                            factual_source, payload
                        ) VALUES (
                            %(tenant_id)s, %(catalog_item_key)s, %(product_id)s, %(variant_id)s, %(sku)s,
                            %(ean)s, %(reference)s, %(brand)s, %(collection)s,
                            %(model)s, %(title_normalized)s, %(category)s,
                            %(gender)s, %(mechanism)s, %(case_size)s, %(dial_color)s,
                            %(strap_color)s, %(material)s, %(strap_type)s,
                            %(colors_normalized)s::jsonb, %(aliases)s::jsonb,
                            %(price)s, %(promotional_price)s, %(stock)s, %(available)s,
                            %(available_in_store)s, %(url)s, %(image_url)s,
                            %(freshness_at)s, %(factual_source)s, %(payload)s::jsonb
                        )
                        ON CONFLICT (tenant_id, catalog_item_key)
                        DO UPDATE SET
                            product_id = EXCLUDED.product_id,
                            variant_id = EXCLUDED.variant_id,
                            sku = EXCLUDED.sku,
                            ean = EXCLUDED.ean,
                            reference = EXCLUDED.reference,
                            brand = EXCLUDED.brand,
                            model = EXCLUDED.model,
                            title_normalized = EXCLUDED.title_normalized,
                            category = EXCLUDED.category,
                            colors_normalized = EXCLUDED.colors_normalized,
                            price = EXCLUDED.price,
                            promotional_price = EXCLUDED.promotional_price,
                            stock = EXCLUDED.stock,
                            available = EXCLUDED.available,
                            available_in_store = EXCLUDED.available_in_store,
                            url = EXCLUDED.url,
                            image_url = EXCLUDED.image_url,
                            freshness_at = EXCLUDED.freshness_at,
                            factual_source = EXCLUDED.factual_source,
                            payload = EXCLUDED.payload,
                            updated_at = now()
                        """,
                        {
                            **payload,
                            "catalog_item_key": payload.get("catalog_item_key")
                            or f"product:{payload.get('product_id')}",
                            "colors_normalized": to_jsonb(payload.get("colors_normalized") or []),
                            "aliases": to_jsonb(payload.get("aliases") or []),
                            "payload": to_jsonb(payload),
                        },
                    )
                    written += 1
            conn.commit()
        return written
    except Exception as exc:
        print("[catalog.index.upsert.error]", {"error_type": type(exc).__name__})
        return 0


def index_products_best_effort(
    products: list[dict[str, Any]],
    *,
    factual_source: FactualSource = "tray_search",
) -> int:
    items = [
        item
        for item in (
            to_canonical_item(product, factual_source=factual_source)
            for product in products
        )
        if item is not None
    ]
    return upsert_canonical_items(items)
