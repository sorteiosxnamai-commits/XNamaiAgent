from __future__ import annotations

import base64
import unicodedata
from typing import Any

import httpx
from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, Field

from .config import get_settings
from .models import AgentResult, IncomingMessage, SalesInterpretation
from .openai_runtime import execute_openai_call
from .turn_runtime import LLMCallBudgetExceeded


IMAGE_IDENTIFY_INSTRUCTIONS = """\
Você identifica relógios em fotos enviadas por clientes da NewStore (loja de relógios).

Extraia o máximo de identidade comercial visível — NÃO fique só em marca + cor:

- marca (brand)
- modelo / linha / coleção (model): nome no mostrador ou linha comercial
  (ex.: Intra-Matic, Prospex Sea Samurai, Sealander, Khaki Field, Ecce Lys, C63)
- referência comercial se aparecer legível (ex.: H38446732, SRPL13K1, C63-36ADA4-S00P0-B0)
- cor do MOSTRADOR (dial) no campo color — só a cor do disco (branco, preto, rosa…)
- acabamento da CAIXA/pulseira no campo case_finish (aço/prata, preto ion, ouro, titânio…),
  separado do mostrador
- funções/atributos visíveis em features[]: chronograph/cronógrafo (submostradores +
  botões), diver/mergulho, GMT, automatic, quartz, etc.

Regras:
- is_watch=false se a imagem não for um relógio de pulso.
- Não invente referência. Se não ler a ref, deixe reference=null.
- reference só quando houver código comercial legível. Nunca coloque cor/descrição
  do mostrador em reference — use color.
- Em color: NÃO inclua pulseira, couro, caixa prata/aço.
  Ex.: mostrador preto + caixa aço → color="preto", case_finish="aço" (ou "prata").
- Em model: priorize linha/coleção legível. Se vir "AUTOMATIC" / "DIVER'S 200m" /
  "CHRONO" no mostrador, inclua no model ou em features — não descarte.
- Se houver submostradores ou botões de cronógrafo, features DEVE incluir "cronógrafo".
- Se o mostrador tiver a palavra AUTOMATIC / AUTOMÁTICO, features DEVE incluir "automático"
  (não confunda com variantes manuais/mecânicas da mesma linha).
- Nunca retorne só brand+color quando a linha ou a função estiver legível na foto.
- confidence entre 0 e 1 conforme legibilidade.
- Preferir nomes comerciais usados em e-commerce BR.
- Se houver legenda do cliente, use-a só como dica complementar — a imagem manda.
"""


class ImageProductIdentification(BaseModel):
    is_watch: bool = True
    brand: str | None = None
    model: str | None = None
    reference: str | None = None
    color: str | None = None
    case_finish: str | None = None
    features: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str | None = None


_FEATURE_MATCH_ALIASES: dict[str, tuple[str, ...]] = {
    "cronografo": ("cronografo", "chronograph", "chrono", "cronograph"),
    "mergulho": ("mergulho", "diver", "divers", "dive", "200m"),
    "gmt": ("gmt",),
}


def normalize_feature_label(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    folded = "".join(
        char
        for char in unicodedata.normalize("NFKD", text).lower()
        if not unicodedata.combining(char)
    )
    if "crono" in folded or "chrono" in folded:
        return "Cronógrafo"
    if "diver" in folded or "mergulho" in folded or "200m" in folded or "200 m" in folded:
        return "Mergulho"
    if "gmt" in folded:
        return "GMT"
    if "automatic" in folded or "automatico" in folded:
        return "Automático"
    if "quartz" in folded:
        return "Quartz"
    return text


def identification_has_catalog_identity(identified: ImageProductIdentification) -> bool:
    """Brand+dial-color alone is too weak for keyword Tray (false siblings)."""
    from .models import ProductPreferences, ProductSubject
    from .product_retrieval import (
        effective_product_reference,
        identity_core_tokens,
        preference_color_tokens,
    )

    if effective_product_reference(identified.reference):
        return True
    features = [
        label
        for label in (normalize_feature_label(item) for item in identified.features)
        if label
    ]
    # Chronograph/diver/etc. give enough signal with brand for a targeted search.
    if any(
        label.casefold() in {"cronógrafo", "cronografo", "mergulho", "gmt"}
        for label in features
    ):
        return True
    model = (identified.model or "").strip()
    if not model:
        return False
    color = (identified.color or "").strip() or None
    probe = SalesInterpretation.model_construct(
        domain="commerce",
        goal="find",
        subject=ProductSubject(product_type="relógio", model=model),
        preferences=ProductPreferences(color=color),
        references_previous_context=False,
        needs_clarification=False,
        confidence=float(identified.confidence or 0.0),
    )
    color_tokens = preference_color_tokens(probe)
    core = identity_core_tokens(model, color_tokens=color_tokens)
    if not core:
        return False
    # model="Preto" alone is not identity — identity_core may fall back to the hue.
    from .product_retrieval import _DIAL_COLOR_TOKENS, _OPTIONAL_MODEL_TOKENS

    non_color = [
        token
        for token in core
        if token not in color_tokens
        and token not in _DIAL_COLOR_TOKENS
        and token not in _OPTIONAL_MODEL_TOKENS
    ]
    return bool(non_color)


def image_search_eligible(message: IncomingMessage) -> bool:
    settings = get_settings()
    if not bool(getattr(settings, "agent_image_search_enabled", True)):
        return False
    if not (message.image_url or "").strip():
        return False
    attachment = (message.attachment_type or "").lower()
    modality = (message.input_modality or "").lower()
    if attachment == "image":
        return True
    return modality in {"image", "text_with_image"}


async def download_image_file(
    url: str,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes, str]:
    settings = get_settings()
    limit = max_bytes or int(
        getattr(settings, "agent_image_download_max_bytes", 8_000_000)
    )
    headers = {
        "User-Agent": "NewStoreAgent/1.0",
        "Accept": "image/*,*/*",
    }
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers=headers,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
    if len(content) > limit:
        raise ValueError("image_too_large")
    content_type = (response.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"
    return content, content_type


def _vision_model() -> str:
    settings = get_settings()
    configured = str(getattr(settings, "agent_image_search_model", "") or "").strip()
    return configured or settings.openai_model


def _caption_hint(message: IncomingMessage) -> str | None:
    text = (message.text or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    if lowered.startswith("[imagem recebida") or lowered.startswith("[sticker recebido"):
        return None
    return text


async def identify_product_from_image(
    message: IncomingMessage,
) -> ImageProductIdentification:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("openai_api_key_missing")
    image_url = (message.image_url or "").strip()
    if not image_url:
        raise ValueError("image_url_missing")

    image_bytes, content_type = await download_image_file(image_url)
    if message.image_mime_type and str(message.image_mime_type).startswith("image/"):
        content_type = str(message.image_mime_type).split(";")[0].strip()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{content_type};base64,{encoded}"
    caption = _caption_hint(message)
    user_text = "Identifique o relógio nesta foto para busca no catálogo da loja."
    if caption:
        user_text += f"\nLegenda do cliente: {caption}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": IMAGE_IDENTIFY_INSTRUCTIONS},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                },
            ],
        },
    ]
    model = _vision_model()
    print("[sales.image.identify.request]", {
        "model": model,
        "has_caption": bool(caption),
        "image_bytes": len(image_bytes),
        "content_type": content_type,
    })
    from .openai_gateway import parse_structured_output

    parse_result = await parse_structured_output(
        model=model,
        text_format=ImageProductIdentification,
        messages=messages,
        temperature=0,
        call_type="image_product_identify",
    )
    identified = parse_result.parsed
    if not isinstance(identified, ImageProductIdentification):
        raise ValueError("image_identify_schema_missing")
    print("[sales.image.identify]", {
        "is_watch": identified.is_watch,
        "has_brand": bool(identified.brand),
        "has_model": bool(identified.model),
        "has_reference": bool(identified.reference),
        "confidence": identified.confidence,
    })
    return identified


def interpretation_from_identification(
    identified: ImageProductIdentification,
) -> SalesInterpretation:
    from .product_retrieval import effective_product_reference, normalize_pt_catalog_query

    color = (identified.color or "").strip() or None
    case_finish = (identified.case_finish or "").strip() or None
    raw_reference = (identified.reference or "").strip() or None
    reference = effective_product_reference(raw_reference)
    # Vision sometimes puts dial color phrases into reference.
    if raw_reference and reference is None:
        if not color:
            color = raw_reference
        elif color.casefold() not in raw_reference.casefold():
            color = f"{color} {raw_reference}".strip()

    features = []
    seen_features: set[str] = set()
    for item in list(identified.features or []) + [
        identified.model,
        identified.notes,
    ]:
        label = normalize_feature_label(item)
        if not label:
            continue
        key = label.casefold()
        if key in seen_features:
            continue
        seen_features.add(key)
        features.append(label)

    model = (identified.model or "").strip() or None
    if model:
        model = normalize_pt_catalog_query(model)
        # Append dial color only when model already has identity (never model="Preto").
        if color and color.casefold() not in model.casefold():
            model = f"{model} {color}".strip()
        for feature in features:
            if feature.casefold() not in model.casefold():
                # Keep distinctive functions in the model string for probes.
                if feature.casefold() in {"cronógrafo", "cronografo", "mergulho", "gmt"}:
                    model = f"{model} {feature}".strip()

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={
            "product_type": "relógio",
            "brand": (identified.brand or "").strip() or None,
            "model": model,
            "reference": reference,
        },
        preferences={
            "color": color,
            "material": case_finish,
            "attributes": features,
        },
        information_needed=["catalog"],
        references_previous_context=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        stop_clarification=True,
        needs_clarification=False,
        clarification_question=None,
        confidence=float(identified.confidence or 0.0),
        active_topic="product_search",
        purchase_stage="discovery",
    )
    interpretation._source = "image_vision"
    return interpretation


def _clarification_result(
    *,
    reason: str,
    identified: ImageProductIdentification | None = None,
) -> AgentResult:
    if identified and not identified.is_watch:
        text = (
            "Recebi a imagem, mas não parece ser a foto de um relógio. "
            "Pode enviar a foto do relógio ou me dizer a marca e o modelo?"
        )
    elif identified and (identified.brand or identified.model):
        hint = " ".join(
            part
            for part in (identified.brand, identified.model)
            if part
        ).strip()
        text = (
            f"Não consegui confirmar a referência com segurança pela foto"
            f"{f' ({hint})' if hint else ''}. "
            "Me confirma a marca e o modelo, ou envia uma foto mais nítida do mostrador?"
        )
    else:
        text = (
            "Recebi a imagem, mas não consegui ler marca/modelo com segurança. "
            "Pode me dizer a marca e o modelo, ou enviar uma foto mais nítida?"
        )
    return AgentResult(
        reply_text=text,
        intent="commerce",
        handoff_required=False,
        safety_reason=reason,
        response_metadata={
            "domain": "commerce",
            "image_search": True,
            "image_identify": (
                identified.model_dump(mode="json") if identified is not None else None
            ),
        },
    )


def _visual_candidates_result(
    products: list[dict[str, Any]],
    *,
    identified: ImageProductIdentification | None,
    trigger: str,
) -> AgentResult:
    from .commerce_router import _product_lines

    numbered_lines = [
        f"{position}. {line}"
        for position, line in enumerate(_product_lines(products, compact=True), start=1)
    ]
    reply = (
        "Pela foto, estes parecem os mais próximos no catálogo:\n"
        + "\n".join(numbered_lines[:2])
        + "\n\nÉ algum desses?"
    )
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason="visual_nearest_neighbor",
        commercial_data={
            "products": products,
            "match_status": "ambiguous",
        },
        response_metadata={
            "domain": "commerce",
            "image_search": True,
            "visual_search": True,
            "visual_trigger": trigger,
            "presented_products": True,
            "product_resolution_state": "plausible_matches",
            "clear_active_product": True,
            "image_identify": (
                identified.model_dump(mode="json") if identified is not None else None
            ),
        },
    )


async def _try_visual_fallback(
    message: IncomingMessage,
    *,
    identified: ImageProductIdentification | None,
    trigger: str,
) -> AgentResult | None:
    settings = get_settings()
    if not bool(getattr(settings, "agent_visual_search_enabled", True)):
        return None
    if not str(getattr(settings, "database_url", "") or "").strip():
        return None

    from .product_image_index import (
        caption_from_identification,
        visual_search_from_caption,
        visual_search_from_image_url,
    )

    products: list[dict[str, Any]] = []
    try:
        caption = ""
        if identified is not None:
            caption = caption_from_identification(identified)
        if caption:
            products = await visual_search_from_caption(caption)
        if not products and (message.image_url or "").strip():
            products = await visual_search_from_image_url(str(message.image_url).strip())
    except (
        APIError,
        LLMCallBudgetExceeded,
        httpx.HTTPError,
        ValueError,
        RuntimeError,
    ) as exc:
        print("[sales.image.visual.fallback.error]", {
            "trigger": trigger,
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })
        return None

    if not products:
        return None

    top_k = int(getattr(settings, "agent_visual_top_k", 3) or 3)
    selected = products[: max(1, top_k)]
    print("[sales.image.visual.fallback]", {
        "trigger": trigger,
        "match_count": len(selected),
    })
    return _visual_candidates_result(
        selected,
        identified=identified,
        trigger=trigger,
    )


def products_match_required_features(
    products: list[dict[str, Any]],
    features: list[str],
) -> bool:
    """True when every distinctive feature has evidence in at least one product."""
    required: list[tuple[str, ...]] = []
    for item in features:
        label = normalize_feature_label(item)
        if not label:
            continue
        folded = "".join(
            char
            for char in unicodedata.normalize("NFKD", label).lower()
            if not unicodedata.combining(char)
        )
        aliases = _FEATURE_MATCH_ALIASES.get(folded)
        if aliases:
            required.append(aliases)
    if not required:
        return True
    if not products:
        return False
    for aliases in required:
        found = False
        for product in products:
            text = " ".join(
                str(product.get(key) or "")
                for key in ("name", "model", "reference", "description", "attributes")
            )
            folded_text = "".join(
                char
                for char in unicodedata.normalize("NFKD", text).lower()
                if not unicodedata.combining(char)
            )
            if any(alias in folded_text for alias in aliases):
                found = True
                break
        if not found:
            return False
    return True


def _product_id(product: dict[str, Any]) -> str | None:
    value = product.get("id") or product.get("product_id")
    return str(value) if value is not None else None


def filter_products_to_interpretation_family(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    """Keep candidates that share brand + model identity with the Vision ask."""
    from .product_retrieval import (
        identity_core_tokens,
        preference_color_tokens,
        product_compatible_with_requested_movement,
        product_matches_feature_tokens,
        preference_feature_tokens,
        _fold,
        _product_text,
    )

    brand = _fold(interpretation.subject.brand)
    color_tokens = preference_color_tokens(interpretation)
    core = identity_core_tokens(
        interpretation.subject.model,
        color_tokens=color_tokens,
    )
    feature_tokens = preference_feature_tokens(interpretation)
    kept: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        text = _product_text(product)
        candidate_brand = _fold(product.get("brand"))
        if brand:
            if candidate_brand and candidate_brand != brand:
                continue
            if not candidate_brand and brand not in text:
                continue
        if core and not all(token in text for token in core):
            continue
        if not product_compatible_with_requested_movement(
            product,
            interpretation.subject.model,
            interpretation.preferences.attributes,
        ):
            continue
        if feature_tokens and not product_matches_feature_tokens(product, feature_tokens):
            continue
        kept.append(product)
    return kept


def merge_tray_with_visual_neighbors(
    tray_products: list[dict[str, Any]],
    visual_products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Prefer visual nearest neighbors within the same model family."""
    family_visual = filter_products_to_interpretation_family(
        visual_products,
        interpretation,
    )
    family_tray = filter_products_to_interpretation_family(
        tray_products,
        interpretation,
    )
    if not family_visual and not family_tray:
        return tray_products[:limit]

    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _add(product: dict[str, Any]) -> None:
        product_id = _product_id(product)
        if not product_id:
            return
        if product_id not in by_id:
            order.append(product_id)
            by_id[product_id] = product
            return
        # Keep visual_distance when present.
        existing = by_id[product_id]
        if product.get("visual_distance") is not None and existing.get(
            "visual_distance"
        ) is None:
            merged = dict(existing)
            merged["visual_distance"] = product.get("visual_distance")
            by_id[product_id] = merged

    # Visual family first (true photo similarity), then tray leftovers.
    for product in family_visual:
        _add(product)
    for product in family_tray:
        _add(product)

    ranked = [by_id[product_id] for product_id in order]
    # If visual distances exist, stable-sort by distance among known ones.
    with_distance = [
        product for product in ranked if product.get("visual_distance") is not None
    ]
    without = [
        product for product in ranked if product.get("visual_distance") is None
    ]
    with_distance.sort(key=lambda item: float(item.get("visual_distance") or 99))
    return (with_distance + without)[: max(1, limit)]


async def _disambiguate_with_visual(
    message: IncomingMessage,
    *,
    identified: ImageProductIdentification,
    interpretation: SalesInterpretation,
    tray_products: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Re-rank / replace Tray siblings using visual nearest neighbors."""
    settings = get_settings()
    if not bool(getattr(settings, "agent_visual_search_enabled", True)):
        filtered = filter_products_to_interpretation_family(tray_products, interpretation)
        return (filtered or tray_products), None
    if not str(getattr(settings, "database_url", "") or "").strip():
        filtered = filter_products_to_interpretation_family(tray_products, interpretation)
        return (filtered or tray_products), None
    image_url = (message.image_url or "").strip()
    if not image_url:
        filtered = filter_products_to_interpretation_family(tray_products, interpretation)
        return (filtered or tray_products), None

    try:
        from .product_image_index import visual_search_from_image_url

        visual_products = await visual_search_from_image_url(image_url)
    except (
        APIError,
        LLMCallBudgetExceeded,
        httpx.HTTPError,
        ValueError,
        RuntimeError,
    ) as exc:
        print("[sales.image.visual.disambiguate.error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })
        filtered = filter_products_to_interpretation_family(tray_products, interpretation)
        return (filtered or tray_products), None

    if not visual_products:
        filtered = filter_products_to_interpretation_family(tray_products, interpretation)
        return (filtered or tray_products), None

    merged = merge_tray_with_visual_neighbors(
        tray_products,
        visual_products,
        interpretation,
        limit=2,
    )
    print("[sales.image.visual.disambiguate]", {
        "tray_count": len(tray_products),
        "visual_count": len(visual_products),
        "merged_ids": [_product_id(item) for item in merged],
        "best_distance": merged[0].get("visual_distance") if merged else None,
    })
    return merged, "image_visual_disambiguate"


async def handle_image_product_search(
    message: IncomingMessage,
) -> AgentResult | None:
    """Identify a watch from an inbound image and search the Tray catalog."""
    if not image_search_eligible(message):
        return None

    settings = get_settings()
    min_confidence = float(
        getattr(settings, "agent_image_search_min_confidence", 0.55)
    )
    try:
        identified = await identify_product_from_image(message)
    except Exception as exc:
        from .openai_errors import OpenAIGatewayError

        if not isinstance(
            exc,
            (
                APIError,
                OpenAIGatewayError,
                LLMCallBudgetExceeded,
                httpx.HTTPError,
                ValueError,
                RuntimeError,
            ),
        ):
            raise
        print("[sales.image.identify.error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })
        visual = await _try_visual_fallback(
            message,
            identified=None,
            trigger="image_identify_failed",
        )
        if visual is not None:
            return visual
        return AgentResult(
            reply_text=(
                "Recebi a imagem, mas não consegui analisar agora. "
                "Pode me dizer a marca e o modelo do relógio?"
            ),
            intent="commerce",
            handoff_required=False,
            safety_reason="image_identify_failed",
            response_metadata={"domain": "commerce", "image_search": True},
        )

    if not identified.is_watch:
        return _clarification_result(
            reason="image_identify_low_confidence",
            identified=identified,
        )

    low_confidence = (
        float(identified.confidence or 0.0) < min_confidence
        or not any(
            (
                identified.brand,
                identified.model,
                identified.reference,
                identified.features,
            )
        )
    )
    if low_confidence:
        visual = await _try_visual_fallback(
            message,
            identified=identified,
            trigger="image_identify_low_confidence",
        )
        if visual is not None:
            return visual
        return _clarification_result(
            reason="image_identify_low_confidence",
            identified=identified,
        )

    # Brand+color (or color-only model) keyword hits invent wrong siblings.
    # Prefer visual nearest-neighbor before Tray when identity is thin.
    if not identification_has_catalog_identity(identified):
        print("[sales.image.weak_identity]", {
            "brand": identified.brand,
            "model": identified.model,
            "color": identified.color,
            "features": identified.features[:4],
        })
        visual = await _try_visual_fallback(
            message,
            identified=identified,
            trigger="image_identify_weak_identity",
        )
        if visual is not None:
            return visual

    interpretation = interpretation_from_identification(identified)
    print("[sales.image.retrieval]", {
        "brand": interpretation.subject.brand,
        "model": interpretation.subject.model,
        "reference": interpretation.subject.reference,
        "attributes": interpretation.preferences.attributes[:4],
        "case_finish": interpretation.preferences.material,
        "confidence": identified.confidence,
        "has_catalog_identity": identification_has_catalog_identity(identified),
    })

    from .sales_agent import (
        _execute_compiled_product_retrieval,
        _mark_sales_result,
    )

    tray_result = await _execute_compiled_product_retrieval(interpretation)
    if tray_result is None:
        visual = await _try_visual_fallback(
            message,
            identified=identified,
            trigger="image_retrieval_empty",
        )
        if visual is not None:
            return visual
        return _clarification_result(
            reason="image_retrieval_empty",
            identified=identified,
        )

    tray_products = (
        (tray_result.commercial_data or {}).get("products")
        if isinstance(tray_result.commercial_data, dict)
        else None
    )
    if (
        isinstance(tray_products, list)
        and tray_products
        and not products_match_required_features(
            tray_products,
            list(interpretation.preferences.attributes or []),
        )
    ):
        visual = await _try_visual_fallback(
            message,
            identified=identified,
            trigger="image_feature_mismatch",
        )
        if visual is not None:
            return visual

    # Keyword Tray often returns several siblings of the same line. Re-rank with
    # visual nearest neighbors so we don't send the mechanical/wrong SKU.
    if isinstance(tray_products, list) and tray_products:
        disambiguated, visual_trigger = await _disambiguate_with_visual(
            message,
            identified=identified,
            interpretation=interpretation,
            tray_products=tray_products,
        )
        if disambiguated:
            tray_products = disambiguated
            if isinstance(tray_result.commercial_data, dict):
                tray_result.commercial_data["products"] = disambiguated
                if visual_trigger:
                    tray_result.commercial_data["visual_disambiguated"] = True
                    tray_result.response_metadata["visual_trigger"] = visual_trigger
                    # One clear visual winner → treat as exact for assertive reply.
                    if len(disambiguated) == 1:
                        tray_result.commercial_data["match_status"] = "exact"
                    elif (
                        disambiguated[0].get("visual_distance") is not None
                        and (
                            len(disambiguated) == 1
                            or float(disambiguated[0].get("visual_distance") or 99)
                            + 0.08
                            < float(disambiguated[1].get("visual_distance") or 99)
                        )
                    ):
                        tray_result.commercial_data["products"] = disambiguated[:1]
                        tray_products = disambiguated[:1]
                        tray_result.commercial_data["match_status"] = "exact"

    label = " ".join(
        part
        for part in (
            interpretation.subject.brand,
            interpretation.subject.model or interpretation.subject.reference,
        )
        if part
    ).strip()
    if tray_result.safety_reason in {
        "product_not_found",
        "exact_product_ambiguous_brand",
    }:
        visual = await _try_visual_fallback(
            message,
            identified=identified,
            trigger=str(tray_result.safety_reason),
        )
        if visual is not None:
            return visual
        products = (
            (tray_result.commercial_data or {}).get("products")
            if isinstance(tray_result.commercial_data, dict)
            else None
        )
        if isinstance(products, list) and products:
            from .commerce_router import _product_lines

            shown = products[:2]
            numbered_lines = [
                f"{position}. {line}"
                for position, line in enumerate(
                    _product_lines(shown, compact=True),
                    start=1,
                )
            ]
            tray_result.reply_text = (
                f"Pela foto, identifiquei {label or 'esse modelo'}, "
                "mas não confirmei a combinação exata. Opções próximas:\n"
                + "\n".join(numbered_lines)
                + "\n\nQuer ver alguma dessas?"
            )
            if isinstance(tray_result.commercial_data, dict):
                tray_result.commercial_data["match_status"] = "ambiguous"
        else:
            color_hint = (identified.color or "").strip()
            tray_result.reply_text = (
                f"Pela foto, identifiquei {label or 'esse modelo'}"
                f"{f' ({color_hint})' if color_hint else ''}, "
                "mas ainda não localizei essa combinação exata no catálogo. "
                "Quer que eu mostre as opções mais próximas dessa linha?"
            )
    elif tray_result.commercial_data and isinstance(
        tray_result.commercial_data.get("products"),
        list,
    ) and tray_result.commercial_data["products"]:
        # Exact/ambiguous catalog hit after Vision — confirm with the customer.
        from .commerce_router import _product_lines

        products = tray_result.commercial_data["products"][:2]
        match_status = tray_result.commercial_data.get("match_status")
        color_tokens = (
            (identified.color or "").strip().casefold()
        )
        color_matched = True
        if color_tokens:
            color_matched = any(
                color_tokens.split()[0] in str(product.get("name") or "").casefold()
                for product in products
            )
        numbered_lines = [
            f"{position}. {line}"
            for position, line in enumerate(
                _product_lines(products, compact=True),
                start=1,
            )
        ]
        # Photo matches always need customer confirmation — never auto-price
        # the first sibling (e.g. Ecce Smalt vs Ecce Lys).
        multi = (
            len(products) >= 2
            or match_status == "ambiguous"
            or not color_matched
        )
        if multi:
            tray_result.reply_text = (
                f"Pela foto, parece {label or 'este modelo'}. "
                "Encontrei estas opções próximas:\n"
                + "\n".join(numbered_lines)
                + "\n\nÉ algum desses?"
            )
            if isinstance(tray_result.commercial_data, dict):
                tray_result.commercial_data["match_status"] = "ambiguous"
        else:
            tray_result.reply_text = (
                f"Pela foto, parece {label or 'este modelo'}. "
                "Encontrei no catálogo:\n"
                + "\n".join(numbered_lines)
                + "\n\nÉ esse que você procura?"
            )

    # Vision turns never activate a SKU — wait for explicit confirmation.
    tray_result.response_metadata.update({
        "image_search": True,
        "image_identify": identified.model_dump(mode="json"),
        "domain": "commerce",
        "used_tray": True,
        "presented_products": True,
        "clear_active_product": True,
        "product_resolution_state": "plausible_matches",
    })
    # Skip OpenAI responder here: Vision already spent the critical latency budget.
    return _mark_sales_result(
        tray_result,
        interpretation=interpretation,
        goal="find",
        response_source="image_vision",
        used_openai_responder=False,
        used_tray=True,
        fallback_reason=tray_result.safety_reason,
    )
