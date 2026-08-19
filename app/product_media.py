from __future__ import annotations

from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .commerce_context import CommerceProductReference
from .models import AgentResult


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _https_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme == "https" and parsed.netloc:
        return candidate
    return None


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme == "http" and parsed.netloc:
        return candidate
    return None


def official_product_url(product: dict[str, Any]) -> str | None:
    """Normalize the current string contract and the legacy protocol map."""
    value = product.get("url")
    direct_https = _https_url(value)
    if direct_https:
        return direct_https
    if isinstance(value, dict):
        for key in ("https", "url", "link"):
            found = _https_url(value.get(key))
            if found:
                return found
        direct_http = _http_url(value.get("http"))
        if direct_http:
            return direct_http
        for key in ("url", "link"):
            found = _http_url(value.get(key))
            if found:
                return found
    return _http_url(value)


def _image_url(value: Any) -> str | None:
    direct = _https_url(value)
    if direct:
        return direct
    if isinstance(value, list):
        for item in value:
            found = _image_url(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("url", "src", "link", "https", "image_url"):
            found = _image_url(value.get(key))
            if found:
                return found
    return None


def official_product_image(product: dict[str, Any]) -> str | None:
    for key in (
        "primary_image_url",
        "primary_image",
        "image_url",
        "image",
        "images",
    ):
        found = _image_url(product.get(key))
        if found:
            return found
    return None


async def resolve_product_image(
    *,
    product_reference: CommerceProductReference,
    execute: ToolExecutor,
) -> AgentResult:
    product = await execute(
        "get_product",
        {"product_id": product_reference.product_id},
    )
    if "error" in product:
        return AgentResult(
            reply_text="Não consegui consultar a imagem oficial deste produto agora.",
            intent="commerce",
            handoff_required=False,
            safety_reason="product_media_technical_failure",
            response_metadata={
                "domain": "commerce",
                "image_url_found": False,
                "media_send_supported": False,
                "media_send_failed": False,
                "used_tray": True,
            },
        )

    image_source = "product"
    image_url = None
    if product_reference.variant_id:
        variant = await execute(
            "get_product_variant",
            {"variant_id": product_reference.variant_id},
        )
        if "error" not in variant:
            image_url = official_product_image(variant)
            if image_url:
                image_source = "variant"
    image_url = image_url or official_product_image(product)
    print("[sales.image.resolve]", {
        "has_image": bool(image_url),
        "image_source": image_source if image_url else None,
    })
    active = product_reference.model_copy(update={
        "name": product.get("name") or product_reference.name,
        "reference": product.get("reference") or product_reference.reference,
    })
    if not image_url:
        return AgentResult(
            reply_text="A Tray não informou uma imagem oficial para este produto.",
            intent="commerce",
            handoff_required=False,
            safety_reason="product_image_not_available",
            commercial_data={"products": [product], "image": None},
            response_metadata={
                "domain": "commerce",
                "active_product": active.model_dump(mode="json"),
                "image_url_found": False,
                "media_send_supported": False,
                "media_send_failed": False,
                "used_tray": True,
            },
        )
    name = str(product.get("name") or product_reference.name or "produto")
    return AgentResult(
        reply_text=f"Esta é a imagem oficial de {name}:\n{image_url}",
        intent="commerce",
        handoff_required=False,
        commercial_data={
            "products": [product],
            "image": {"url": image_url, "source": image_source},
        },
        response_metadata={
            "domain": "commerce",
            "active_product": active.model_dump(mode="json"),
            "outbound_image_url": image_url,
            "image_url_found": True,
            "media_send_supported": False,
            "media_send_failed": False,
            "media_supported": False,
            "used_tray": True,
        },
    )
