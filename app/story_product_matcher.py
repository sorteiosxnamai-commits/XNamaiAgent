"""Match Story visual evidence to real catalog candidates (tenant-scoped)."""

from __future__ import annotations

from typing import Any

from .catalog_index import build_allowed_id_sets, reject_unknown_rerank_ids
from .config import get_settings
from .fact_authority import catalog_item_key_for
from .instagram_story_models import (
    StoryCandidateScore,
    StoryProductCandidate,
    StoryVisualUnderstanding,
)
from .observability import log_event


def _fold(value: Any) -> str:
    return str(value or "").strip().casefold()


def classify_match(
    candidates: list[StoryProductCandidate],
    *,
    multiple_products: bool,
) -> tuple[str, StoryProductCandidate | None]:
    settings = get_settings()
    exact_min = float(
        getattr(settings, "instagram_story_exact_match_min_confidence", None)
        or getattr(settings, "instagram_story_auto_match_min_confidence", 0.95)
        or 0.95
    )
    visual_min = float(
        getattr(settings, "instagram_story_visual_match_min_confidence", 0.96) or 0.96
    )
    amb_min = float(
        getattr(settings, "instagram_story_ambiguous_min_confidence", 0.65) or 0.65
    )
    margin = float(getattr(settings, "instagram_story_match_margin", 0.12) or 0.12)
    if not candidates or multiple_products:
        if candidates and candidates[0].score >= amb_min:
            return "ambiguous", candidates[0]
        return "not_found", None
    ordered = sorted(candidates, key=lambda c: (-c.score, c.product_id))
    top1 = ordered[0]
    top2 = ordered[1] if len(ordered) > 1 else None
    gap = top1.score - (top2.score if top2 else 0.0)
    reasons = " ".join(top1.match_reasons).casefold()
    is_exact = any(
        token in reasons
        for token in ("ean:", "sku:", "reference:", "hash:")
    )
    threshold = exact_min if is_exact else visual_min
    # Appearance-only must meet the stricter visual threshold.
    if top1.score >= threshold and gap >= margin and top1.product_id:
        # Never elevate confidence solely because there is a single result without evidence.
        if len(ordered) == 1 and not is_exact and top1.score < visual_min:
            if top1.score >= amb_min:
                return "ambiguous", top1
            return "not_found", None
        if top1.score_components and top1.score_components.conflicts:
            return "ambiguous", top1
        return "matched", top1
    if top1.score >= amb_min:
        return "ambiguous", top1
    return "not_found", None


def _score_components(
    *,
    catalog_item_key: str,
    product_id: str,
    variant_id: str | None,
    exact: float = 0.0,
    visual: float = 0.0,
    lexical: float = 0.0,
    brand: float = 0.0,
    color: float = 0.0,
    model: float = 0.0,
    quality_penalty: float = 0.0,
    conflict_penalty: float = 0.0,
    reasons: list[str] | None = None,
    conflicts: list[str] | None = None,
    source: str = "catalog",
) -> StoryCandidateScore:
    final = max(
        0.0,
        min(
            1.0,
            exact
            + visual * 0.35
            + lexical * 0.15
            + brand * 0.1
            + color * 0.08
            + model * 0.1
            - quality_penalty
            - conflict_penalty,
        ),
    )
    # Exact identifier dominates.
    if exact >= 0.95:
        final = max(final, exact)
    return StoryCandidateScore(
        catalog_item_key=catalog_item_key,
        product_id=product_id,
        variant_id=variant_id,
        exact_identifier_score=exact,
        visual_similarity_score=visual,
        lexical_score=lexical,
        brand_score=brand,
        color_score=color,
        model_score=model,
        image_quality_penalty=quality_penalty,
        conflict_penalty=conflict_penalty,
        final_score=final,
        reasons=list(reasons or []),
        conflicts=list(conflicts or []),
        source=source,
    )


async def match_story_to_catalog(
    *,
    tenant_id: str,
    analysis: StoryVisualUnderstanding,
    execute_tool: Any | None = None,
    media_bytes: bytes | None = None,
) -> list[StoryProductCandidate]:
    """Build candidates from exact identifiers → visual index → lexical → Tray.

    Never invents product IDs. Scores reflect real evidence components.
    """
    if not str(tenant_id or "").strip():
        raise ValueError("tenant_id required")
    _ = media_bytes
    settings = get_settings()
    limit = int(getattr(settings, "instagram_story_max_candidates", 10) or 10)
    candidates: list[StoryProductCandidate] = []
    seen: set[str] = set()
    quality_penalty = 0.15 if analysis.image_quality == "poor" else 0.0

    def _add_scored(product: dict[str, Any], components: StoryCandidateScore) -> None:
        if not isinstance(product, dict) or product.get("id") is None:
            return
        # Tenant isolation — reject foreign rows if present.
        product_tenant = str(product.get("tenant_id") or tenant_id).strip()
        if product_tenant and product_tenant != tenant_id:
            return
        key = components.catalog_item_key
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            StoryProductCandidate(
                catalog_item_key=key,
                product_id=components.product_id,
                variant_id=components.variant_id,
                score=components.final_score,
                match_reasons=list(components.reasons),
                mismatch_reasons=list(components.conflicts),
                source=components.source,
                score_components=components,
            )
        )

    # Level 1 — exact identifiers via catalog index.
    try:
        from .catalog_index_repository import CatalogIndexRepository, row_to_product_dict

        repo = CatalogIndexRepository()
        for ean in analysis.visible_eans:
            for row in repo.search_exact(tenant_id=tenant_id, ean=str(ean)):
                product = row_to_product_dict(row)
                pid = str(product["id"])
                vid = str(product["variant_id"]) if product.get("variant_id") else None
                _add_scored(
                    product,
                    _score_components(
                        catalog_item_key=catalog_item_key_for(pid, vid),
                        product_id=pid,
                        variant_id=vid,
                        exact=0.99,
                        quality_penalty=quality_penalty,
                        reasons=[f"ean:{ean}"],
                        source="exact_ean",
                    ),
                )
        for sku in analysis.visible_skus:
            for row in repo.search_exact(tenant_id=tenant_id, sku=str(sku)):
                product = row_to_product_dict(row)
                pid = str(product["id"])
                vid = str(product["variant_id"]) if product.get("variant_id") else None
                _add_scored(
                    product,
                    _score_components(
                        catalog_item_key=catalog_item_key_for(pid, vid),
                        product_id=pid,
                        variant_id=vid,
                        exact=0.98,
                        quality_penalty=quality_penalty,
                        reasons=[f"sku:{sku}"],
                        source="exact_sku",
                    ),
                )
        for ref in analysis.visible_references:
            for row in repo.search_exact(tenant_id=tenant_id, reference=str(ref)):
                product = row_to_product_dict(row)
                pid = str(product["id"])
                vid = str(product["variant_id"]) if product.get("variant_id") else None
                _add_scored(
                    product,
                    _score_components(
                        catalog_item_key=catalog_item_key_for(pid, vid),
                        product_id=pid,
                        variant_id=vid,
                        exact=0.97,
                        quality_penalty=quality_penalty,
                        reasons=[f"reference:{ref}"],
                        source="exact_reference",
                    ),
                )
            for brand in analysis.visible_brands or analysis.logo_hypotheses:
                rows = repo.search_lexical(
                    tenant_id=tenant_id,
                    query=f"{brand} {ref}".strip(),
                    brand=brand,
                )
                for row in rows[:5]:
                    product = row_to_product_dict(row)
                    pid = str(product["id"])
                    vid = str(product["variant_id"]) if product.get("variant_id") else None
                    conflicts: list[str] = []
                    brand_score = 0.7
                    row_brand = _fold(product.get("brand"))
                    if row_brand and _fold(brand) and row_brand != _fold(brand):
                        conflicts.append("brand_conflict")
                        brand_score = 0.0
                    _add_scored(
                        product,
                        _score_components(
                            catalog_item_key=catalog_item_key_for(pid, vid),
                            product_id=pid,
                            variant_id=vid,
                            lexical=0.55,
                            brand=brand_score,
                            model=0.4,
                            quality_penalty=quality_penalty,
                            conflict_penalty=0.25 if conflicts else 0.0,
                            reasons=[f"brand_ref:{brand}:{ref}"],
                            conflicts=conflicts,
                            source="lexical",
                        ),
                    )
    except Exception as exc:  # noqa: BLE001
        print("[story.matcher.index.error]", {"error_type": type(exc).__name__})

    # Level 2 — real visual neighbors from caption embedding (tenant filtered when possible).
    try:
        from .product_image_index import visual_search_from_caption

        caption_bits = [
            *analysis.visible_brands[:2],
            *analysis.model_hypotheses[:2],
            *analysis.visible_references[:1],
            *analysis.dial_colors[:1],
            *analysis.strap_colors[:1],
            analysis.visual_description[:180],
        ]
        caption = " ".join(str(x) for x in caption_bits if x).strip()
        if caption:
            neighbors = await visual_search_from_caption(caption)
            for neighbor in neighbors[:limit]:
                pid = str(neighbor.get("product_id") or "").strip()
                if not pid:
                    continue
                distance = float(neighbor.get("distance") or 1.0)
                # Convert distance to similarity in [0,1].
                visual = max(0.0, min(1.0, 1.0 - distance))
                conflicts: list[str] = []
                brand_score = 0.0
                nb_brand = _fold(neighbor.get("brand"))
                if analysis.visible_brands:
                    if nb_brand and nb_brand in {_fold(b) for b in analysis.visible_brands}:
                        brand_score = 0.8
                    elif nb_brand:
                        conflicts.append("brand_conflict")
                color_score = 0.0
                if analysis.dial_colors:
                    caption_l = _fold(neighbor.get("visual_caption") or neighbor.get("name"))
                    if any(_fold(c) in caption_l for c in analysis.dial_colors):
                        color_score = 0.5
                _add_scored(
                    {
                        "id": pid,
                        "variant_id": neighbor.get("variant_id"),
                        "brand": neighbor.get("brand"),
                        "name": neighbor.get("name"),
                        "tenant_id": tenant_id,
                    },
                    _score_components(
                        catalog_item_key=catalog_item_key_for(
                            pid,
                            str(neighbor["variant_id"])
                            if neighbor.get("variant_id") is not None
                            else None,
                        ),
                        product_id=pid,
                        variant_id=(
                            str(neighbor["variant_id"])
                            if neighbor.get("variant_id") is not None
                            else None
                        ),
                        visual=visual,
                        brand=brand_score,
                        color=color_score,
                        quality_penalty=quality_penalty,
                        conflict_penalty=0.3 if conflicts else 0.0,
                        reasons=[f"visual_neighbor:d={distance:.3f}"],
                        conflicts=conflicts,
                        source="visual_index",
                    ),
                )
    except Exception as exc:  # noqa: BLE001
        print("[story.matcher.visual.error]", {"error_type": type(exc).__name__})

    # Level 3 — Tray complementary search (evidence-based score, never flat 0.7).
    if execute_tool is not None and len(candidates) < 3:
        query_bits = [
            *analysis.visible_brands[:1],
            *analysis.model_hypotheses[:1],
            *analysis.visible_references[:1],
            *analysis.dial_colors[:1],
        ]
        query = " ".join(str(x) for x in query_bits if x).strip()
        if query:
            try:
                result = await execute_tool(
                    "search_products", {"query": query, "limit": limit}
                )
                products = result.get("products") if isinstance(result, dict) else None
                if isinstance(products, list):
                    for idx, product in enumerate(products[:limit]):
                        pid = str(product.get("id") or "")
                        if not pid:
                            continue
                        vid = (
                            str(product["variant_id"])
                            if product.get("variant_id") is not None
                            else None
                        )
                        # Decay by rank; require lexical overlap for non-trivial score.
                        name_l = _fold(product.get("name") or product.get("title"))
                        overlap = 0.0
                        for token in query.casefold().split():
                            if len(token) >= 3 and token in name_l:
                                overlap += 0.15
                        lexical = min(0.6, overlap)
                        if lexical <= 0:
                            continue
                        rank_penalty = idx * 0.04
                        _add_scored(
                            {**product, "tenant_id": product.get("tenant_id") or tenant_id},
                            _score_components(
                                catalog_item_key=catalog_item_key_for(pid, vid),
                                product_id=pid,
                                variant_id=vid,
                                lexical=max(0.0, lexical - rank_penalty),
                                quality_penalty=quality_penalty,
                                reasons=[f"tray_query_overlap:{query[:40]}"],
                                source="tray_search",
                            ),
                        )
            except Exception as exc:  # noqa: BLE001
                print("[story.matcher.tray.error]", {"error_type": type(exc).__name__})

    ordered = sorted(candidates, key=lambda c: (-c.score, c.product_id))[:limit]
    log_event(
        "instagram_story.candidates_found",
        {
            "count": len(ordered),
            "top_score": ordered[0].score if ordered else None,
            "multiple_products": analysis.multiple_products,
            "tenant_id": tenant_id,
        },
    )
    return ordered


def reject_invented_rerank_ids(
    selected_ids: list[str],
    candidates: list[StoryProductCandidate],
) -> tuple[list[str], int]:
    allowed = {c.product_id for c in candidates}
    return reject_unknown_rerank_ids(
        selected_ids,
        allowed,
        limit=max(len(selected_ids), len(allowed), 1),
    )


def candidates_to_allowed_sets(
    candidates: list[StoryProductCandidate],
) -> dict[str, set[str]]:
    products = [
        {
            "id": c.product_id,
            "variant_id": c.variant_id,
            "catalog_item_key": c.catalog_item_key,
        }
        for c in candidates
    ]
    return build_allowed_id_sets(products)
