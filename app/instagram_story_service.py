"""Operational Instagram Story product resolution (wired into the sales agent)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import get_settings
from .fact_authority import authorize_products_for_responder, catalog_item_key_for
from .instagram_story_intent import (
    detect_story_question_type,
    should_route_story_question,
)
from .instagram_story_media import (
    StoryMediaError,
    download_story_media,
    extract_video_frames_best_effort,
)
from .instagram_story_models import (
    InstagramStoryContext,
    StoryConversationReference,
    StoryProductCandidate,
    StoryResolutionResult,
    StoryVisualUnderstanding,
    VisualProductRegion,
)
from .models import AgentResult, IncomingMessage
from .observability import log_event
from .request_principal import RequestPrincipal, principal_from_internal
from .story_commercial_policy import (
    evidence_from_tray_product,
    validate_commercial_answer,
)
from .story_product_matcher import classify_match, match_story_to_catalog
from .story_product_repository import StoryProductRepository
from .story_tenant import resolve_story_tenant
from .story_visual_analyzer import (
    analyze_story_image,
    get_cached_visual_analysis,
    put_cached_visual_analysis,
)


def story_rollout_allows(
    *,
    tenant_id: str,
    story: InstagramStoryContext,
    conversation_id: str | None = None,
    incoming: IncomingMessage | None = None,
) -> tuple[bool, str]:
    settings = get_settings()
    mode = str(getattr(settings, "instagram_story_rollout_mode", "off") or "off").casefold()
    meta_live = False
    if incoming is not None and bool(getattr(settings, "meta_webhook_enabled", False)):
        if (incoming.provider or "").lower() == "meta" and (
            bool((incoming.image_url or "").strip())
            or bool(story.operational_media_url())
        ):
            meta_live = True

    if not bool(getattr(settings, "instagram_story_recognition_enabled", False)):
        if meta_live:
            return True, "meta_live_media"
        return False, "recognition_disabled"
    if mode == "off":
        if meta_live:
            return True, "meta_live_media"
        return False, "rollout_off"
    if mode == "diagnostics":
        return False, "diagnostics_only"
    real_ok = bool(getattr(settings, "instagram_story_real_payload_validated", False))
    if mode in {"canary", "full"} and not real_ok:
        if meta_live:
            return True, "meta_live_media"
        return False, "real_payload_not_validated"
    if mode == "full":
        return True, "full"
    if mode == "shadow":
        return True, "shadow"
    if mode == "canary":
        percent = float(getattr(settings, "instagram_story_canary_percent", 5) or 5)
        # Stable per conversation: hash(tenant + conversation) % 100
        conv = str(conversation_id or story.story_message_id or story.story_media_id or "")
        key = f"{tenant_id}:{conv}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % 100
        in_canary = bucket < int(percent)
        return in_canary, "canary" if in_canary else "canary_excluded"
    return False, "unknown_mode"


async def _revalidate_product(
    *,
    product_id: str,
    execute_tool: Any,
    variant_id: str | None = None,
) -> tuple[dict[str, Any] | None, bool, str | None]:
    """Revalidate on Tray. Returns (product, tray_failed, failure_code)."""
    try:
        result = await execute_tool("get_product", {"product_id": str(product_id)})
    except Exception:
        return None, True, "tray_unavailable"
    if not isinstance(result, dict) or result.get("error"):
        return None, True, "product_revalidation_failed"
    product = dict(result)
    product["id"] = str(product.get("id") or product_id)
    if variant_id:
        try:
            variant = await execute_tool(
                "get_product_variant",
                {"product_id": str(product_id), "variant_id": str(variant_id)},
            )
        except Exception:
            return None, True, "tray_unavailable"
        if not isinstance(variant, dict) or variant.get("error"):
            return None, True, "variant_revalidation_failed"
        # Prefer variant commercial fields when present.
        for key in (
            "price",
            "promotional_price",
            "stock",
            "available",
            "url",
            "sku",
            "ean",
            "name",
        ):
            if variant.get(key) is not None:
                product[key] = variant.get(key)
        product["variant_id"] = str(variant_id)
        product["_variant_revalidated"] = True
    product["_revalidated"] = True
    product["_factual_source"] = "tray_live"
    return product, False, None


async def _list_real_variants(
    *,
    product_id: str,
    execute_tool: Any,
) -> list[dict[str, Any]]:
    try:
        result = await execute_tool("list_product_variants", {"product_id": str(product_id)})
    except Exception:
        return []
    if not isinstance(result, dict):
        return []
    variants = result.get("variants") or result.get("items") or []
    if not isinstance(variants, list):
        return []
    out: list[dict[str, Any]] = []
    for item in variants:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        out.append(item)
    return out


def _clarification_from_regions(
    analysis: StoryVisualUnderstanding,
) -> tuple[list[str], str]:
    regions = list(analysis.product_regions or [])
    options: list[str] = []
    for region in regions[:4]:
        if isinstance(region, VisualProductRegion):
            bits = []
            if region.label:
                bits.append(region.label)
            if region.dial_color:
                bits.append(f"mostrador {region.dial_color}")
            if region.position and region.position != "unknown":
                bits.append(f"à {region.position}" if region.position in {"left", "right"} else region.position)
            label = " — ".join(bits) if bits else f"produto {len(options) + 1}"
            options.append(label)
        elif isinstance(region, dict):
            label = str(region.get("label") or "").strip()
            dial = str(region.get("dial_color") or "").strip()
            pos = str(region.get("position") or "").strip()
            bits = [b for b in (label, f"mostrador {dial}" if dial else "", pos if pos and pos != "unknown" else "") if b]
            options.append(" — ".join(bits) if bits else f"produto {len(options) + 1}")
    if len(options) >= 2 and any("mostrador" in o or "left" in o or "right" in o or "esquerda" in o or "direita" in o for o in options):
        reply = (
            f"Nesse Story aparecem {len(options)} relógios. "
            f"Você quer o {options[0]} ou o {options[1]}?"
        )
        return options, reply
    if analysis.watch_count > 1 or analysis.multiple_products:
        reply = (
            "Nesse Story aparecem mais de um relógio. "
            "Você quer saber do primeiro ou do segundo?"
        )
        return options or ["primeiro", "segundo"], reply
    return options, (
        "Nesse Story a identificação ficou parcial. "
        "Pode me dizer a marca ou a referência?"
    )


def _compose_reply(
    *,
    question_type: Any,
    product: dict[str, Any] | None,
    status: str,
    candidates: list[StoryProductCandidate],
    tray_failed: bool = False,
    expired: bool = False,
    clarification_reply: str | None = None,
    variant_lines: list[str] | None = None,
) -> str:
    if expired:
        return (
            "Não consegui mais acessar a imagem desse Story. "
            "Se você enviar um print, eu identifico o modelo e confirmo o valor."
        )
    if clarification_reply:
        return clarification_reply
    if status == "ambiguous" and candidates:
        return (
            "Quero confirmar o modelo para não passar o valor errado. "
            "Você está falando do primeiro ou do segundo modelo parecido desse Story?"
        )
    if status in {"not_found", "failed"}:
        return (
            "Não consegui identificar com segurança o produto desse Story. "
            "Se puder enviar um print mais nítido ou a referência, eu confirmo."
        )
    if tray_failed and product is not None:
        name = str(product.get("name") or "o modelo")
        return (
            f"Identifiquei {name}, mas não consegui confirmar o preço atualizado agora. "
            "Posso tentar novamente em instantes ou encaminhar para o atendimento."
        )
    if tray_failed and product is None:
        return (
            "Não consegui confirmar os dados comerciais atualizados na loja agora. "
            "Posso tentar novamente em instantes ou encaminhar para o atendimento."
        )
    if product is None:
        return (
            "Recebi sua pergunta sobre o Story. "
            "Me confirma a cor ou o modelo para eu consultar o valor atualizado?"
        )
    name = str(product.get("name") or "Esse modelo")
    price = product.get("promotional_price") or product.get("price") or product.get("current_price")
    stock = product.get("stock")
    url = product.get("url")
    available = product.get("available")
    if question_type.value == "product_link" and url:
        return f"Esse é o {name}. Segue o link atualizado: {url}"
    if question_type.value == "availability":
        if available is False or (isinstance(stock, int) and stock <= 0):
            return (
                f"Esse é o {name}. Consultei agora e ele não está disponível neste momento. "
                "Quer que eu veja alternativas parecidas?"
            )
        return f"Esse é o {name}. Consultei agora e ele está disponível neste momento."
    if question_type.value == "color_options":
        if variant_lines:
            listed = "; ".join(variant_lines[:5])
            return (
                f"Esse é o {name}. Consultei as variantes disponíveis agora: {listed}. "
                "Qual você prefere?"
            )
        return (
            f"Esse é o {name}. Não encontrei outras cores/variantes listadas no catálogo agora. "
            "Quer que eu confirme o valor deste modelo?"
        )
    if price is not None:
        try:
            price_txt = f"R$ {float(price):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except (TypeError, ValueError):
            price_txt = str(price)
        avail_txt = "está disponível" if available is not False else "não está disponível"
        return (
            f"Esse é o {name}. Consultei agora e o valor atual é {price_txt}. "
            f"Ele {avail_txt} neste momento. Quer que eu envie o link?"
        )
    return f"Esse é o {name}. Quer que eu confirme o valor atualizado e o link?"


def _reuse_assoc_result(
    *,
    assoc: Any,
    media_id: str,
    tenant: str,
    question_type: Any,
    shadow_only: bool,
    metrics: dict[str, Any],
    product: dict[str, Any] | None,
    tray_failed: bool,
    evidence: list[dict[str, Any]],
    candidates: list[StoryProductCandidate] | None = None,
    clarification_options: list[str] | None = None,
    clarification_reply: str | None = None,
    variant_lines: list[str] | None = None,
) -> StoryResolutionResult:
    status = assoc.match_status
    return StoryResolutionResult(
        resolved=bool(product) and not tray_failed and status in {"matched", "manually_confirmed"},
        tenant_id=tenant,
        story_media_id=media_id,
        match_status=status,
        catalog_item_key=assoc.catalog_item_key,
        product_id=assoc.product_id,
        variant_id=assoc.variant_id,
        confidence=float(assoc.match_confidence or 0.0),
        candidates=candidates or [],
        needs_clarification=status in {"ambiguous", "not_found", "failed", "processing", "expired"},
        clarification_options=clarification_options or [],
        factual_evidence=evidence,
        question_type=question_type,
        product_payload=product,
        reply_hint=_compose_reply(
            question_type=question_type,
            product=product,
            status=status if status != "manually_confirmed" else "matched",
            candidates=candidates or [],
            tray_failed=tray_failed,
            clarification_reply=clarification_reply,
            variant_lines=variant_lines,
        ),
        shadow_only=shadow_only,
        metrics=metrics,
        resolved_at=datetime.now(timezone.utc),
    )


async def resolve_story_product_question(
    *,
    incoming: IncomingMessage,
    tenant_id: str | None = None,
    customer_context: dict[str, Any] | None = None,
    execute_tool: Any | None = None,
    principal: RequestPrincipal | None = None,
) -> StoryResolutionResult | None:
    _ = customer_context
    if not should_route_story_question(incoming):
        return None
    story = incoming.instagram_story
    if not isinstance(story, InstagramStoryContext):
        return None

    # Server-supplied tenant_id must ride an internal principal — never a raw body field.
    effective_principal = principal
    if tenant_id and effective_principal is None:
        effective_principal = principal_from_internal(
            subject_id="agent_runtime",
            tenant_id=str(tenant_id),
            source="agent_runtime",
        )

    tenant_resolution = await resolve_story_tenant(
        provider=story.provider or "brevo",
        instagram_account_id=story.instagram_account_id or "",
        integration_id=None,
        explicit_tenant_id=tenant_id,
        principal=effective_principal,
    )
    if not tenant_resolution.ok or not tenant_resolution.tenant_id:
        log_event("story_tenant_resolution", {"ok": False, "code": "story_tenant_unresolved"})
        return StoryResolutionResult(
            resolved=False,
            match_status="failed",
            failure_reason=tenant_resolution.failure_code or "story_tenant_unresolved",
            needs_clarification=True,
            question_type=detect_story_question_type(incoming.text),
            reply_hint=(
                "Não consegui localizar o catálogo desta conta agora. "
                "Posso encaminhar para o atendimento."
            ),
            metrics={"story_tenant_resolution": "failed"},
        )

    tenant = tenant_resolution.tenant_id
    allowed, rollout_reason = story_rollout_allows(
        tenant_id=tenant,
        story=story,
        conversation_id=getattr(incoming, "conversation_id", None),
        incoming=incoming,
    )
    if not allowed:
        return None

    question_type = detect_story_question_type(incoming.text)
    metrics: dict[str, Any] = {
        "story_visual_analysis_calls": 0,
        "story_media_cache_hit": 0,
        "story_db_analysis_cache_hit": 0,
        "story_deterministic_matches": 0,
        "story_ambiguous_matches": 0,
        "story_tenant_resolution": tenant_resolution.source,
        "rollout": rollout_reason,
    }
    shadow_only = rollout_reason == "shadow"
    media_id = str(story.story_media_id or "")
    log_event(
        "story_payload_detected",
        {
            "tenant_id": tenant,
            "media_type": story.media_type,
            "question_type": question_type.value,
            "rollout": rollout_reason,
            "story_media_id_hash": hashlib.sha256(media_id.encode()).hexdigest()[:12] if media_id else None,
            "media_url_present": bool(story.operational_media_url()),
            "media_log": (
                story.story_media_log_reference.model_dump(mode="json")
                if story.story_media_log_reference
                else None
            ),
        },
    )

    repo = StoryProductRepository()
    provider = story.provider or "brevo"
    account = story.instagram_account_id or "unknown"

    if not media_id:
        return StoryResolutionResult(
            resolved=False,
            tenant_id=tenant,
            match_status="failed",
            needs_clarification=True,
            failure_reason="story_media_id_missing",
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="failed",
                candidates=[],
                expired=True,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    assoc = repo.get_by_story(
        tenant_id=tenant,
        provider=provider,
        instagram_account_id=account,
        story_media_id=media_id,
    )
    if assoc is None:
        assoc = repo.create_pending(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            story_message_id=story.story_message_id,
            story_permalink=story.story_permalink,
            media_type=story.media_type,
            source_timestamp=story.source_timestamp,
            story_expires_at=story.expires_at,
        )

    async def _maybe_revalidate(product_id: str, variant_id: str | None):
        if execute_tool is None:
            return None, False, None, []
        log_event("story_revalidation", {"product_id": product_id, "has_variant": bool(variant_id)})
        product, tray_failed, code = await _revalidate_product(
            product_id=product_id,
            execute_tool=execute_tool,
            variant_id=variant_id,
        )
        evidence: list[dict[str, Any]] = []
        if product and not tray_failed:
            authorized, grounded = authorize_products_for_responder(
                [product],
                tenant_id=tenant,
            )
            product = authorized[0] if authorized else product
            evidence = [g.model_dump(mode="json") for g in grounded]
        if tray_failed and code == "variant_revalidation_failed":
            log_event("story_variant_revalidation", {"ok": False, "code": code})
            repo.mark_failed(
                tenant_id=tenant,
                provider=provider,
                instagram_account_id=account,
                story_media_id=media_id,
                explanation={"reason": code},
                failure_code=code,
            )
        return product, tray_failed, code, evidence

    # Already matched / manually confirmed → revalidate only (zero vision).
    if assoc and assoc.match_status in {"matched", "manually_confirmed"} and assoc.product_id:
        metrics["story_deterministic_matches"] = 1
        repo.touch_last_seen(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
        )
        product, tray_failed, _code, evidence = await _maybe_revalidate(
            assoc.product_id, assoc.variant_id
        )
        variant_lines: list[str] | None = None
        if (
            question_type.value == "color_options"
            and execute_tool is not None
            and product is not None
            and not tray_failed
        ):
            variants = await _list_real_variants(
                product_id=assoc.product_id, execute_tool=execute_tool
            )
            variant_lines = []
            for item in variants:
                color = item.get("color") or item.get("name") or item.get("sku")
                if color:
                    avail = item.get("available")
                    suffix = " (disponível)" if avail is not False else " (indisponível)"
                    variant_lines.append(f"{color}{suffix}")
            log_event("story_variant_revalidation", {"ok": True, "count": len(variant_lines)})
        return _reuse_assoc_result(
            assoc=assoc,
            media_id=media_id,
            tenant=tenant,
            question_type=question_type,
            shadow_only=shadow_only,
            metrics=metrics,
            product=product,
            tray_failed=tray_failed,
            evidence=evidence,
            variant_lines=variant_lines,
        )

    if assoc and assoc.match_status == "ambiguous":
        metrics["story_ambiguous_matches"] = 1
        cands = [
            StoryProductCandidate.model_validate(c)
            for c in (assoc.candidate_products or [])
            if isinstance(c, dict)
        ]
        analysis_payload = assoc.visual_analysis or {}
        try:
            analysis_obj = StoryVisualUnderstanding.model_validate(analysis_payload)
            options, reply = _clarification_from_regions(analysis_obj)
        except Exception:
            options, reply = ["primeiro", "segundo"], (
                "Nesse Story a identificação ficou parcial. "
                "Você quer o primeiro ou o segundo modelo?"
            )
        log_event("story_clarification", {"source": "reuse_ambiguous"})
        return _reuse_assoc_result(
            assoc=assoc,
            media_id=media_id,
            tenant=tenant,
            question_type=question_type,
            shadow_only=shadow_only,
            metrics=metrics,
            product=None,
            tray_failed=False,
            evidence=[],
            candidates=cands[:5],
            clarification_options=options,
            clarification_reply=reply,
        )

    if assoc and assoc.match_status == "not_found":
        # Respect prior not_found — do not re-run vision on every message.
        return _reuse_assoc_result(
            assoc=assoc,
            media_id=media_id,
            tenant=tenant,
            question_type=question_type,
            shadow_only=shadow_only,
            metrics=metrics,
            product=None,
            tray_failed=False,
            evidence=[],
        )

    if assoc and assoc.match_status == "expired":
        return StoryResolutionResult(
            resolved=False,
            tenant_id=tenant,
            story_media_id=media_id,
            match_status="expired",
            needs_clarification=True,
            failure_reason="story_expired",
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="failed",
                candidates=[],
                expired=True,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    if assoc and assoc.match_status == "processing":
        # Soft wait — never launch a second vision call without the lease.
        for _ in range(3):
            await asyncio.sleep(0.15)
            assoc = repo.get_by_story(
                tenant_id=tenant,
                provider=provider,
                instagram_account_id=account,
                story_media_id=media_id,
            )
            if assoc and assoc.match_status != "processing":
                break
        if assoc and assoc.match_status == "processing":
            log_event("story_processing_lock", {"status": "busy"})
            return StoryResolutionResult(
                resolved=False,
                tenant_id=tenant,
                story_media_id=media_id,
                match_status="processing",
                needs_clarification=True,
                failure_reason="analysis_in_progress",
                question_type=question_type,
                reply_hint=(
                    "Estou confirmando o modelo desse Story. "
                    "Só um instante — ou me manda a cor/referência se preferir."
                ),
                shadow_only=shadow_only,
                metrics=metrics,
            )
        # Fall through to reuse matched/ambiguous after wait.
        if assoc and assoc.match_status in {"matched", "manually_confirmed"} and assoc.product_id:
            product, tray_failed, _code, evidence = await _maybe_revalidate(
                assoc.product_id, assoc.variant_id
            )
            return _reuse_assoc_result(
                assoc=assoc,
                media_id=media_id,
                tenant=tenant,
                question_type=question_type,
                shadow_only=shadow_only,
                metrics=metrics,
                product=product,
                tray_failed=tray_failed,
                evidence=evidence,
            )

    owner = f"story-{uuid.uuid4().hex[:12]}"
    claimed = repo.begin_processing(
        tenant_id=tenant,
        provider=provider,
        instagram_account_id=account,
        story_media_id=media_id,
        owner=owner,
    )
    if claimed is None:
        current = repo.get_by_story(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
        )
        log_event(
            "story_processing_lock",
            {"status": "not_claimed", "current": current.match_status if current else None},
        )
        if current and current.match_status == "processing":
            return StoryResolutionResult(
                resolved=False,
                tenant_id=tenant,
                story_media_id=media_id,
                match_status="processing",
                needs_clarification=True,
                failure_reason="analysis_in_progress",
                question_type=question_type,
                reply_hint=(
                    "Estou confirmando o modelo desse Story. "
                    "Só um instante — ou me manda a cor/referência se preferir."
                ),
                shadow_only=shadow_only,
                metrics=metrics,
            )
        if current and current.match_status in {"matched", "manually_confirmed"} and current.product_id:
            product, tray_failed, _code, evidence = await _maybe_revalidate(
                current.product_id, current.variant_id
            )
            return _reuse_assoc_result(
                assoc=current,
                media_id=media_id,
                tenant=tenant,
                question_type=question_type,
                shadow_only=shadow_only,
                metrics=metrics,
                product=product,
                tray_failed=tray_failed,
                evidence=evidence,
            )
        # Do not start vision without the lock.
        return StoryResolutionResult(
            resolved=False,
            tenant_id=tenant,
            story_media_id=media_id,
            match_status=current.match_status if current else "processing",
            needs_clarification=True,
            failure_reason="analysis_in_progress",
            question_type=question_type,
            reply_hint=(
                "Estou confirmando o modelo desse Story. "
                "Só um instante."
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    log_event("story_processing_lock", {"status": "claimed", "owner_prefix": owner[:8]})
    if claimed.processing_attempts and claimed.processing_attempts > 1:
        log_event("story_processing_recovery", {"attempts": claimed.processing_attempts})

    analysis: StoryVisualUnderstanding | None = None
    media_sha: str | None = None
    storage_path: str | None = None
    media_mime: str | None = None
    media_bytes: int | None = None
    download_url = story.operational_media_url() or story.operational_thumbnail_url()

    if not download_url:
        repo.mark_expired(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            explanation={"reason": "media_url_missing"},
            failure_code="media_url_missing",
        )
        return StoryResolutionResult(
            resolved=False,
            tenant_id=tenant,
            story_media_id=media_id,
            match_status="expired",
            failure_reason="media_url_missing",
            needs_clarification=True,
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="failed",
                candidates=[],
                expired=True,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    try:
        media = await download_story_media(download_url, tenant_id=tenant)
        media_sha = media.sha256
        storage_path = media.storage_path
        media_mime = media.content_type
        media_bytes = media.byte_count
        log_event(
            "story_media_download",
            {
                "bytes": media.byte_count,
                "host": media.final_host,
                "stored": bool(storage_path),
                "sha_prefix": media_sha[:12],
            },
        )
        log_event("story_media_bytes", {"bytes": media.byte_count})

        # Order: hash → confirmed association → L1 → L2 → OpenAI
        prior = repo.find_by_media_hash(tenant_id=tenant, media_sha256=media_sha)
        if prior and prior.product_id:
            metrics["story_media_cache_hit"] = 1
            repo.confirm_match(
                tenant_id=tenant,
                provider=provider,
                instagram_account_id=account,
                story_media_id=media_id,
                catalog_item_key=prior.catalog_item_key
                or catalog_item_key_for(prior.product_id, prior.variant_id),
                product_id=prior.product_id,
                variant_id=prior.variant_id,
                match_source="visual_catalog_match",
                match_confidence=float(prior.match_confidence or 0.95),
                explanation={"via": "media_sha256"},
            )
            metrics["story_deterministic_matches"] = 1
            product, tray_failed, _code, evidence = await _maybe_revalidate(
                prior.product_id, prior.variant_id
            )
            return StoryResolutionResult(
                resolved=bool(product) and not tray_failed,
                tenant_id=tenant,
                story_media_id=media_id,
                match_status="matched",
                catalog_item_key=prior.catalog_item_key,
                product_id=prior.product_id,
                variant_id=prior.variant_id,
                confidence=float(prior.match_confidence or 0.95),
                product_payload=product,
                factual_evidence=evidence,
                question_type=question_type,
                reply_hint=_compose_reply(
                    question_type=question_type,
                    product=product,
                    status="matched",
                    candidates=[],
                    tray_failed=tray_failed,
                ),
                shadow_only=shadow_only,
                metrics=metrics,
                resolved_at=datetime.now(timezone.utc),
            )

        analysis = get_cached_visual_analysis(tenant_id=tenant, media_sha256=media_sha)
        if analysis is not None:
            metrics["story_media_cache_hit"] = 1
        else:
            analysis = repo.find_visual_analysis_by_hash(
                tenant_id=tenant, media_sha256=media_sha
            )
            if analysis is not None:
                metrics["story_db_analysis_cache_hit"] = 1
                put_cached_visual_analysis(
                    tenant_id=tenant, media_sha256=media_sha, analysis=analysis
                )

        if analysis is None:
            if media.content_type.startswith("video/"):
                frames = extract_video_frames_best_effort(
                    media.content,
                    max_frames=int(
                        getattr(get_settings(), "instagram_story_video_max_frames", 3) or 3
                    ),
                )
                thumb_url = story.operational_thumbnail_url()
                if not frames and thumb_url:
                    thumb = await download_story_media(thumb_url, tenant_id=tenant)
                    analysis = await analyze_story_image(
                        image_bytes=thumb.content,
                        content_type=thumb.content_type,
                        media_sha256=media_sha,
                        media_type="image",
                    )
                    metrics["story_visual_analysis_calls"] = 1
                elif frames:
                    analysis = await analyze_story_image(
                        image_bytes=frames[0],
                        content_type="image/jpeg",
                        media_sha256=media_sha,
                        media_type="video",
                        extra_frame_bytes=frames[1:],
                    )
                    metrics["story_visual_analysis_calls"] = 1
                else:
                    repo.mark_failed(
                        tenant_id=tenant,
                        provider=provider,
                        instagram_account_id=account,
                        story_media_id=media_id,
                        explanation={"reason": "video_decoder_unavailable"},
                        failure_code="video_decoder_unavailable",
                    )
                    return StoryResolutionResult(
                        resolved=False,
                        tenant_id=tenant,
                        story_media_id=media_id,
                        match_status="failed",
                        failure_reason="video_decoder_unavailable",
                        needs_clarification=True,
                        question_type=question_type,
                        reply_hint=_compose_reply(
                            question_type=question_type,
                            product=None,
                            status="failed",
                            candidates=[],
                            expired=True,
                        ),
                        shadow_only=shadow_only,
                        metrics=metrics,
                    )
            else:
                analysis = await analyze_story_image(
                    image_bytes=media.content,
                    content_type=media.content_type,
                    media_sha256=media_sha,
                    media_type=story.media_type,
                )
                metrics["story_visual_analysis_calls"] = 1
            if analysis is not None:
                put_cached_visual_analysis(
                    tenant_id=tenant, media_sha256=media_sha, analysis=analysis
                )
                log_event(
                    "story_visual_analysis_calls",
                    {"count": metrics["story_visual_analysis_calls"]},
                )
    except StoryMediaError as exc:
        if exc.code in {
            "host_not_allowed",
            "scheme_not_https",
            "private_ip_blocked",
            "redirect_host_not_allowed",
            "redirect_private_ip",
        }:
            repo.mark_failed(
                tenant_id=tenant,
                provider=provider,
                instagram_account_id=account,
                story_media_id=media_id,
                explanation={"reason": exc.code},
                failure_code=exc.code,
            )
            status = "failed"
        else:
            repo.mark_expired(
                tenant_id=tenant,
                provider=provider,
                instagram_account_id=account,
                story_media_id=media_id,
                explanation={"reason": exc.code},
                failure_code=exc.code,
            )
            status = "expired"
        return StoryResolutionResult(
            resolved=False,
            tenant_id=tenant,
            story_media_id=media_id,
            match_status=status,
            failure_reason=exc.code,
            needs_clarification=True,
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="failed",
                candidates=[],
                expired=True,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    if analysis is None:
        repo.mark_failed(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            explanation={"reason": "visual_analysis_missing"},
            failure_code="visual_analysis_missing",
        )
        return StoryResolutionResult(
            resolved=False,
            tenant_id=tenant,
            story_media_id=media_id,
            match_status="failed",
            failure_reason="visual_analysis_missing",
            needs_clarification=True,
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="failed",
                candidates=[],
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    repo.save_visual_analysis(
        tenant_id=tenant,
        provider=provider,
        instagram_account_id=account,
        story_media_id=media_id,
        visual_analysis=analysis.model_dump(mode="json"),
        media_sha256=media_sha,
        media_storage_path=storage_path,
        media_mime=media_mime,
        media_bytes=media_bytes,
    )

    if analysis.multiple_products or analysis.watch_count > 1:
        options, reply = _clarification_from_regions(analysis)
        repo.mark_ambiguous(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            explanation={"reason": "multiple_products_visible", "options": options},
            confidence=float(analysis.product_identity_confidence or 0.5),
        )
        metrics["story_ambiguous_matches"] = 1
        log_event("story_clarification", {"reason": "multiple_products", "options": len(options)})
        return StoryResolutionResult(
            resolved=False,
            tenant_id=tenant,
            story_media_id=media_id,
            match_status="ambiguous",
            needs_clarification=True,
            clarification_options=options,
            confidence=float(analysis.product_identity_confidence or 0.5),
            question_type=question_type,
            reply_hint=reply,
            shadow_only=shadow_only,
            metrics=metrics,
            resolved_at=datetime.now(timezone.utc),
        )

    from .tray_tools import execute_tool as default_execute

    tool = execute_tool or default_execute
    candidates = await match_story_to_catalog(
        tenant_id=tenant,
        analysis=analysis,
        execute_tool=tool,
        media_bytes=None,  # visual neighbors use caption/OCR evidence
    )
    repo.save_candidates(
        tenant_id=tenant,
        provider=provider,
        instagram_account_id=account,
        story_media_id=media_id,
        candidates=[c.model_dump(mode="json") for c in candidates],
        explanation={
            "analysis_version": getattr(
                get_settings(), "instagram_story_analysis_version", "v2"
            )
        },
    )
    status, top = classify_match(
        candidates,
        multiple_products=bool(analysis.multiple_products),
    )
    if candidates:
        log_event(
            "story_match_confidence",
            {
                "top": candidates[0].score,
                "margin": (
                    candidates[0].score - candidates[1].score if len(candidates) > 1 else None
                ),
                "source": candidates[0].source,
                "count": len(candidates),
            },
        )
        log_event("story_candidates", {"count": len(candidates)})
        log_event("story_match_source", {"source": candidates[0].source})

    if status == "matched" and top is not None:
        repo.confirm_match(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            catalog_item_key=top.catalog_item_key,
            product_id=top.product_id,
            variant_id=top.variant_id,
            match_source=(
                "visual_exact_reference"
                if any(r.startswith(("ean:", "sku:", "reference:")) for r in top.match_reasons)
                else "visual_similarity"
            ),
            match_confidence=top.score,
            explanation={"reasons": top.match_reasons},
        )
        product, tray_failed, _code, evidence = await _maybe_revalidate(
            top.product_id, top.variant_id
        )
        return StoryResolutionResult(
            resolved=bool(product) and not tray_failed,
            tenant_id=tenant,
            story_media_id=media_id,
            match_status="matched",
            catalog_item_key=top.catalog_item_key,
            product_id=top.product_id,
            variant_id=top.variant_id,
            confidence=top.score,
            candidates=candidates[:5],
            factual_evidence=evidence,
            product_payload=product,
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=product,
                status="matched",
                candidates=candidates,
                tray_failed=tray_failed,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
            resolved_at=datetime.now(timezone.utc),
        )

    if status == "ambiguous":
        options = [
            c.catalog_item_key or c.product_id for c in candidates[:3] if c.product_id
        ]
        repo.mark_ambiguous(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            explanation={"top": candidates[0].model_dump(mode="json") if candidates else {}},
            confidence=candidates[0].score if candidates else 0.0,
        )
        metrics["story_ambiguous_matches"] = 1
        log_event("story_clarification", {"reason": "close_scores"})
        return StoryResolutionResult(
            resolved=False,
            tenant_id=tenant,
            story_media_id=media_id,
            match_status="ambiguous",
            needs_clarification=True,
            clarification_options=options,
            candidates=candidates[:5],
            confidence=candidates[0].score if candidates else 0.0,
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="ambiguous",
                candidates=candidates,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
            resolved_at=datetime.now(timezone.utc),
        )

    repo.mark_not_found(
        tenant_id=tenant,
        provider=provider,
        instagram_account_id=account,
        story_media_id=media_id,
        explanation={"candidate_count": len(candidates)},
    )
    return StoryResolutionResult(
        resolved=False,
        tenant_id=tenant,
        story_media_id=media_id,
        match_status="not_found",
        needs_clarification=True,
        candidates=candidates[:5],
        question_type=question_type,
        reply_hint=_compose_reply(
            question_type=question_type,
            product=None,
            status="not_found",
            candidates=candidates,
        ),
        shadow_only=shadow_only,
        metrics=metrics,
        resolved_at=datetime.now(timezone.utc),
    )


def story_result_to_agent_result(
    resolution: StoryResolutionResult,
    *,
    incoming: IncomingMessage,
) -> AgentResult | None:
    """Convert resolution into an AgentResult. Shadow mode returns None (no reply change)."""
    if resolution.shadow_only:
        log_event(
            "story_reply_generated",
            {"shadow": True, "status": resolution.match_status},
        )
        return None
    reply = (resolution.reply_hint or "").strip()
    if not reply:
        return None
    tenant = resolution.tenant_id
    evidence = None
    if resolution.product_payload and tenant:
        try:
            evidence = evidence_from_tray_product(
                resolution.product_payload,
                tenant_id=tenant,
                confidence=float(resolution.confidence or 0.0),
                source="tray_api",
            )
        except Exception:
            evidence = None
        violations = validate_commercial_answer(
            reply,
            resolution.product_payload,
            evidence,
            tenant,
            min_confidence=float(
                getattr(get_settings(), "instagram_story_ambiguous_min_confidence", 0.65)
                or 0.65
            ),
        )
        if violations:
            log_event(
                "story_commercial_blocked",
                {"codes": violations, "status": resolution.match_status},
            )
            # Strip commercial certainty — ask for confirmation instead of inventing.
            if any(
                code.startswith("price_") or code.startswith("stock_") or "commercial" in code
                for code in violations
            ):
                reply = (
                    "Identifiquei um modelo possível, mas não posso confirmar o valor "
                    "ou o estoque sem revalidação na loja. Posso tentar de novo ou "
                    "encaminhar para o atendimento."
                )
    metadata: dict[str, Any] = {
        "domain": "commerce",
        "instagram_story": True,
        "story_match_status": resolution.match_status,
        "story_confidence": resolution.confidence,
        "story_metrics": resolution.metrics,
        "tenant_id": tenant,
        "story_media_id": resolution.story_media_id,
    }
    commercial: dict[str, Any] = {}
    if resolution.product_payload and evidence and evidence.authorizes_price():
        commercial["products"] = [resolution.product_payload]
        metadata["presented_products"] = True
        metadata["active_product"] = {
            "product_id": resolution.product_id,
            "variant_id": resolution.variant_id,
            "catalog_item_key": resolution.catalog_item_key,
            "name": resolution.product_payload.get("name"),
            "url": resolution.product_payload.get("url") if evidence.authorizes_url() else None,
        }
        metadata["last_story_product"] = StoryConversationReference(
            story_media_id=resolution.story_media_id,
            tenant_id=tenant,
            catalog_item_key=resolution.catalog_item_key,
            product_id=resolution.product_id,
            variant_id=resolution.variant_id,
            match_status=resolution.match_status,
            confidence=resolution.confidence,
            resolved_at=resolution.resolved_at or datetime.now(timezone.utc),
        ).model_dump(mode="json")
        metadata["last_presented_products"] = [
            {
                "product_id": resolution.product_id,
                "variant_id": resolution.variant_id,
                "catalog_item_key": resolution.catalog_item_key,
            }
        ]
        if resolution.factual_evidence:
            metadata["grounded_commerce_evidence"] = resolution.factual_evidence
        metadata["allowed_id_sets"] = {
            "allowed_product_ids": [resolution.product_id] if resolution.product_id else [],
            "allowed_variant_ids": [resolution.variant_id] if resolution.variant_id else [],
            "allowed_catalog_item_keys": (
                [resolution.catalog_item_key] if resolution.catalog_item_key else []
            ),
        }
        metadata["product_evidence"] = evidence.model_dump(mode="json")
    elif resolution.product_payload:
        # Identity without commercial authority — still keep reference for follow-ups.
        metadata["active_product"] = {
            "product_id": resolution.product_id,
            "variant_id": resolution.variant_id,
            "catalog_item_key": resolution.catalog_item_key,
            "name": resolution.product_payload.get("name"),
        }
        commercial["products"] = [resolution.product_payload]
    log_event(
        "story_reply_generated",
        {
            "status": resolution.match_status,
            "resolved": resolution.resolved,
            "question_type": resolution.question_type.value,
            "tenant_present": bool(tenant),
        },
    )
    _ = incoming
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason=(
            None
            if resolution.resolved
            else resolution.failure_reason or resolution.match_status
        ),
        commercial_data=commercial or None,
        response_metadata=metadata,
    )
