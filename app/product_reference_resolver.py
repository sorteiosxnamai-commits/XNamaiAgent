"""Central product reference resolution for Story + objective commerce intents."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .instagram_story_models import StoryConversationReference
from .observability import log_event


class ProductResolution(BaseModel):
    status: Literal[
        "confirmed_association",
        "exact_match",
        "name_match",
        "structured_match",
        "semantic_match",
        "needs_clarification",
        "not_found",
    ] = "not_found"
    tenant_id: str
    product_id: str | None = None
    variant_id: str | None = None
    catalog_item_key: str | None = None
    confidence: float = 0.0
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    source: str | None = None
    clarification_hint: str | None = None


async def resolve_product_reference(
    *,
    tenant_id: str,
    user_text: str,
    story_context: StoryConversationReference | None = None,
    execute_tool: Any | None = None,
) -> ProductResolution:
    """Resolve a product for objective questions without inventing IDs.

    Order: confirmed Story association → exact index → lexical → Tray search.
    """
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id required")

    if (
        story_context is not None
        and story_context.product_id
        and story_context.match_status in {"matched", "manually_confirmed"}
        and (story_context.tenant_id in (None, tid))
    ):
        log_event(
            "product_reference_resolved",
            {"source": "confirmed_story_association", "tenant_present": True},
        )
        return ProductResolution(
            status="confirmed_association",
            tenant_id=tid,
            product_id=story_context.product_id,
            variant_id=story_context.variant_id,
            catalog_item_key=story_context.catalog_item_key,
            confidence=float(story_context.confidence or 1.0),
            source="confirmed_story_association",
        )

    text = str(user_text or "").strip()
    try:
        from .catalog_index_repository import CatalogIndexRepository, row_to_product_dict

        repo = CatalogIndexRepository()
        # Exact-looking tokens (SKU/ref heuristics)
        tokens = [t for t in text.replace(",", " ").split() if len(t) >= 4]
        for token in tokens[:5]:
            for row in repo.search_exact(tenant_id=tid, reference=token):
                product = row_to_product_dict(row)
                return ProductResolution(
                    status="exact_match",
                    tenant_id=tid,
                    product_id=str(product.get("id")),
                    variant_id=(
                        str(product["variant_id"])
                        if product.get("variant_id") is not None
                        else None
                    ),
                    catalog_item_key=str(product.get("_catalog_item_key") or ""),
                    confidence=0.97,
                    source="exact_reference",
                    candidates=[product],
                )
            for row in repo.search_exact(tenant_id=tid, sku=token):
                product = row_to_product_dict(row)
                return ProductResolution(
                    status="exact_match",
                    tenant_id=tid,
                    product_id=str(product.get("id")),
                    variant_id=(
                        str(product["variant_id"])
                        if product.get("variant_id") is not None
                        else None
                    ),
                    catalog_item_key=str(product.get("_catalog_item_key") or ""),
                    confidence=0.98,
                    source="exact_sku",
                    candidates=[product],
                )
        if text:
            rows = repo.search_lexical(tenant_id=tid, query=text, limit=5)
            if rows:
                products = [row_to_product_dict(r) for r in rows]
                top = products[0]
                return ProductResolution(
                    status="name_match" if len(products) == 1 else "needs_clarification",
                    tenant_id=tid,
                    product_id=str(top.get("id")) if len(products) == 1 else None,
                    variant_id=(
                        str(top["variant_id"])
                        if len(products) == 1 and top.get("variant_id") is not None
                        else None
                    ),
                    catalog_item_key=(
                        str(top.get("_catalog_item_key") or "")
                        if len(products) == 1
                        else None
                    ),
                    confidence=0.8 if len(products) == 1 else 0.7,
                    source="lexical",
                    candidates=products,
                    clarification_hint=(
                        None
                        if len(products) == 1
                        else "Encontrei mais de um modelo parecido. Qual referência ou cor você quer?"
                    ),
                )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "product_reference_index_error",
            {"error_type": type(exc).__name__},
        )

    if execute_tool is not None and text:
        try:
            result = await execute_tool("search_products", {"query": text, "limit": 5})
            products = result.get("products") if isinstance(result, dict) else None
            if isinstance(products, list) and products:
                return ProductResolution(
                    status="needs_clarification" if len(products) > 1 else "structured_match",
                    tenant_id=tid,
                    product_id=str(products[0].get("id")) if len(products) == 1 else None,
                    confidence=0.75 if len(products) == 1 else 0.6,
                    source="tray_search",
                    candidates=list(products)[:5],
                    clarification_hint=(
                        None
                        if len(products) == 1
                        else "Encontrei algumas opções. Qual modelo exatamente?"
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            log_event(
                "product_reference_tray_error",
                {"error_type": type(exc).__name__},
            )

    return ProductResolution(
        status="not_found",
        tenant_id=tid,
        clarification_hint=(
            "Não identifiquei o produto com segurança. "
            "Pode me passar a referência ou um print?"
        ),
    )
