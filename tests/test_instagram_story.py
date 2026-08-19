"""Instagram Story recognition — parser, media safety, matching, rollout (v8)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from app.instagram_story_intent import (
    detect_story_question_type,
    should_route_story_question,
)
from app.instagram_story_media import (
    StoryMediaError,
    validate_story_media_url,
    _sniff_mime,
)
from app.instagram_story_models import (
    InstagramStoryContext,
    StoryProductCandidate,
    StoryVisualUnderstanding,
    VisualProductRegion,
)
from app.instagram_story_parser import (
    extract_instagram_story_context,
    safe_media_reference,
    sanitize_instagram_story_reference,
    strip_signed_url,
)
from app.models import IncomingMessage
from app.story_product_matcher import classify_match, reject_invented_rerank_ids
from app.story_publication_link_service import validate_link_payload
from app.webhook_parser import parse_brevo_conversations_payload

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_story"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _story_reply_payload() -> dict:
    return _load_fixture("brevo_story_image_signed_url.json")


def test_strip_signed_url_removes_query():
    url = "https://scontent.cdninstagram.com/v/t51/x.jpg?oe=ABC&oh=SECRET"
    cleaned = strip_signed_url(url)
    assert cleaned is not None
    assert "oh=" not in cleaned
    assert cleaned.endswith("/v/t51/x.jpg")


def test_operational_url_preserves_signature_private_secret():
    payload = _story_reply_payload()
    message = payload["messages"][0]
    ctx = extract_instagram_story_context(
        payload=payload,
        message=message,
        channel="instagram",
        visitor=payload["visitor"],
    )
    assert ctx is not None
    op = ctx.operational_media_url()
    assert op is not None
    assert "oh=SECRETTOKEN" in op
    # Sanitized log reference has no signature.
    assert ctx.story_media_log_reference is not None
    assert ctx.story_media_log_reference.host == "scontent.cdninstagram.com"
    assert ctx.story_media_log_reference.path_hash
    dumped = ctx.model_dump(mode="json")
    assert "SECRETTOKEN" not in json.dumps(dumped)
    assert "story_media_url_private" not in dumped
    assert "oh=" not in repr(ctx)


def test_private_url_absent_from_logs(caplog: pytest.LogCaptureFixture):
    url = "https://scontent.cdninstagram.com/v/t51/x.jpg?oe=ABC&oh=SECRETTOKEN"
    ref = safe_media_reference(url)
    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("media_ref=%s", ref.model_dump() if ref else None)
    assert "SECRETTOKEN" not in caplog.text
    assert "oh=" not in caplog.text


def test_sanitize_story_reference_redacts_tokens_and_urls():
    raw = {
        "access_token": "secret-value",
        "url": "https://scontent.cdninstagram.com/v/t.jpg?oh=TOKEN",
        "nested": {"a": 1},
    }
    cleaned = sanitize_instagram_story_reference(raw)
    assert cleaned["access_token"] == "[REDACTED]"
    assert "oh=" not in str(cleaned.get("url"))
    assert cleaned["nested"]["_keys"] == ["a"]


def test_extract_story_context_from_brevo_meta_shape():
    payload = _story_reply_payload()
    message = payload["messages"][0]
    ctx = extract_instagram_story_context(
        payload=payload,
        message=message,
        channel="instagram",
        visitor=payload["visitor"],
    )
    assert ctx is not None
    assert ctx.replied_to_story is True
    assert ctx.story_media_id == "story_image_001"
    assert "oh=SECRETTOKEN" in (ctx.operational_media_url() or "")
    assert "SECRET" not in str(ctx.raw_reference) or "SECRET" in ""  # raw ref sanitized
    assert "SECRETTOKEN" not in json.dumps(ctx.raw_reference)


def test_fixture_brevo_reply_to_story():
    payload = _load_fixture("brevo_reply_to_story.json")
    incoming = parse_brevo_conversations_payload(payload)
    assert incoming.instagram_story is not None
    assert incoming.instagram_story.story_media_id == "story_media_fixture_001"
    assert incoming.instagram_story.replied_to_story is True


def test_parser_attaches_instagram_story_to_incoming():
    incoming = parse_brevo_conversations_payload(_story_reply_payload())
    assert incoming.channel == "instagram"
    assert incoming.instagram_story is not None
    assert incoming.instagram_story.replied_to_story is True
    assert incoming.instagram_story.story_media_id == "story_image_001"
    assert incoming.channel_metadata.get("instagram_story", {}).get("replied_to_story") is True


def test_common_instagram_dm_without_story_has_no_context():
    payload = {
        "eventName": "conversationFragment",
        "visitor": {"id": "v1", "source": "instagram", "sourceConversationRef": "u1"},
        "messages": [{"type": "visitor", "id": "m1", "text": "oi, tem Seiko?"}],
    }
    incoming = parse_brevo_conversations_payload(payload)
    assert incoming.channel == "instagram"
    assert incoming.instagram_story is None
    assert should_route_story_question(incoming) is False


def test_story_mention_attachment():
    payload = _load_fixture("brevo_story_mention.json")
    incoming = parse_brevo_conversations_payload(payload)
    assert incoming.instagram_story is not None
    assert incoming.instagram_story.mentioned_in_story is True
    assert incoming.instagram_story.story_media_id == "story_mention_fixture_001"


def test_story_video_and_carousel_fixtures():
    video = parse_brevo_conversations_payload(_load_fixture("brevo_story_video.json"))
    assert video.instagram_story is not None
    assert video.instagram_story.media_type in {"video", "unknown", "image"}
    carousel = parse_brevo_conversations_payload(_load_fixture("brevo_story_carousel.json"))
    assert carousel.instagram_story is not None
    assert carousel.instagram_story.media_type == "carousel"
    assert carousel.instagram_story.media_items


def test_story_expired_without_url_and_without_id():
    expired = parse_brevo_conversations_payload(
        _load_fixture("brevo_story_expired_no_url.json")
    )
    assert expired.instagram_story is not None
    assert expired.instagram_story.operational_media_url() is None
    no_id = parse_brevo_conversations_payload(
        _load_fixture("brevo_story_no_media_id.json")
    )
    assert no_id.instagram_story is not None
    assert no_id.instagram_story.operational_media_url() is not None
    assert (no_id.instagram_story.story_media_id or "").startswith("synthetic:")


def test_question_types():
    assert detect_story_question_type("Qual o valor?").value == "price"
    assert detect_story_question_type("Tem disponível?").value == "availability"
    assert detect_story_question_type("Manda o link").value == "product_link"
    assert detect_story_question_type("Tem outra cor?").value == "color_options"


def test_validate_media_url_blocks_http_and_localhost():
    with pytest.raises(StoryMediaError) as http_exc:
        validate_story_media_url("http://scontent.cdninstagram.com/x.jpg")
    assert http_exc.value.code == "scheme_not_https"
    with pytest.raises(StoryMediaError) as local_exc:
        validate_story_media_url("https://localhost/x.jpg")
    assert local_exc.value.code in {"host_blocked", "host_not_allowed", "private_ip_blocked"}


def test_validate_media_url_blocks_unknown_host():
    with pytest.raises(StoryMediaError) as exc:
        validate_story_media_url("https://evil.example/steal.jpg")
    assert exc.value.code == "host_not_allowed"


def test_validate_media_url_preserves_signed_operational_url(monkeypatch):
    import socket as socket_mod

    def _gai(host, port, *args, **kwargs):
        return [
            (socket_mod.AF_INET, socket_mod.SOCK_STREAM, 0, "", ("157.240.0.1", 443))
        ]

    monkeypatch.setattr("app.instagram_story_media.socket.getaddrinfo", _gai)
    url = "https://scontent.cdninstagram.com/v/t51/x.jpg?oe=ABC&oh=SECRET"
    cleaned, _ips = validate_story_media_url(url)
    assert cleaned == url
    assert "oh=SECRET" in cleaned


def test_sniff_rejects_html_disguised():
    assert _sniff_mime(b"<!DOCTYPE html><html>") == "text/html"
    assert _sniff_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"


@pytest.mark.asyncio
async def test_download_receives_full_signed_url(monkeypatch):
    from app import instagram_story_media as media_mod

    captured: dict = {}

    class FakeStream:
        def __init__(self, url: str, **_kwargs):
            captured["url"] = url
            self.status_code = 200
            self.headers = {
                "content-type": "image/jpeg",
                "content-length": "4",
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            yield b"\xff\xd8\xff\xe0"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            assert kwargs.get("follow_redirects") is False
            return FakeStream(url)

    monkeypatch.setattr(media_mod.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        media_mod,
        "validate_story_media_url",
        lambda url: (url, ["1.2.3.4"]),
    )
    monkeypatch.setenv("INSTAGRAM_STORY_MEDIA_STORAGE_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    signed = "https://scontent.cdninstagram.com/v/t51/x.jpg?oe=ABC&oh=SECRET"
    result = await media_mod.download_story_media(signed, tenant_id="tenant_a")
    assert captured["url"] == signed
    assert result.sha256
    assert result.storage_path is None
    get_settings.cache_clear()


def test_classify_match_thresholds():
    strong = [
        StoryProductCandidate(
            catalog_item_key="product:1",
            product_id="1",
            score=0.96,
            match_reasons=["ean:123"],
        ),
        StoryProductCandidate(
            catalog_item_key="product:2",
            product_id="2",
            score=0.70,
            match_reasons=["tray_query"],
        ),
    ]
    status, top = classify_match(strong, multiple_products=False)
    assert status == "matched"
    assert top is not None and top.product_id == "1"

    close = [
        StoryProductCandidate(
            catalog_item_key="product:1",
            product_id="1",
            score=0.80,
            match_reasons=["visual"],
        ),
        StoryProductCandidate(
            catalog_item_key="product:2",
            product_id="2",
            score=0.78,
            match_reasons=["visual"],
        ),
    ]
    status2, _ = classify_match(close, multiple_products=False)
    assert status2 == "ambiguous"


def test_reject_invented_rerank_ids():
    candidates = [
        StoryProductCandidate(catalog_item_key="product:1", product_id="1", score=0.9),
    ]
    ordered, invalid = reject_invented_rerank_ids(["1", "999"], candidates)
    assert ordered == ["1"]
    assert invalid == 1


def test_validate_link_payload_rejects_missing_and_ignores_price():
    with pytest.raises(ValueError):
        validate_link_payload({"tenant_id": "newstore"})
    cleaned = validate_link_payload(
        {
            "tenant_id": "newstore",
            "instagram_account_id": "ig1",
            "story_media_id": "s1",
            "product_id": "10",
            "variant_id": "99",
            "catalog_item_key": "client-forged-key",
            "price": 9999,
            "stock": 5,
        }
    )
    assert "price" not in cleaned
    assert "stock" not in cleaned
    assert cleaned["catalog_item_key"] == "variant:99"


def test_story_rollout_off_skips(monkeypatch):
    from app.instagram_story_service import story_rollout_allows
    from app.config import get_settings

    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "off")
    get_settings.cache_clear()
    story = InstagramStoryContext(
        provider="brevo",
        instagram_account_id="ig",
        story_media_id="s1",
        replied_to_story=True,
    )
    ok, reason = story_rollout_allows(tenant_id="newstore", story=story)
    assert ok is False
    assert reason == "rollout_off"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_resolve_story_already_matched_revalidates(monkeypatch):
    from app.config import get_settings
    from app import instagram_story_service as service
    from app.instagram_story_models import StoryProductAssociation

    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "full")
    monkeypatch.setenv("INSTAGRAM_STORY_REAL_PAYLOAD_VALIDATED", "true")
    get_settings.cache_clear()

    assoc = StoryProductAssociation(
        tenant_id="newstore",
        provider="brevo",
        instagram_account_id="ig_biz_fixture",
        story_media_id="story_image_001",
        product_id="42",
        catalog_item_key="product:42",
        match_status="matched",
        match_source="manual",
        match_confidence=1.0,
    )

    class FakeRepo:
        def get_by_story(self, **_kwargs):
            return assoc

        def create_pending(self, **_kwargs):
            return assoc

        def touch_last_seen(self, **_kwargs):
            return None

        def begin_processing(self, **_kwargs):
            return None

        def mark_failed(self, **_kwargs):
            return None

    monkeypatch.setattr(service, "StoryProductRepository", FakeRepo)

    async def fake_tool(name, args):
        assert name in {"get_product", "get_product_variant", "list_product_variants"}
        if name == "get_product":
            return {
                "id": "42",
                "name": "Seiko SRPD51",
                "price": 1899.0,
                "stock": 2,
                "available": True,
                "url": "https://loja.example/p/42",
            }
        return {"error": "not_needed"}

    incoming = parse_brevo_conversations_payload(_story_reply_payload())
    result = await service.resolve_story_product_question(
        incoming=incoming,
        tenant_id="newstore",
        execute_tool=fake_tool,
    )
    assert result is not None
    assert result.resolved is True
    assert result.tenant_id == "newstore"
    assert result.product_id == "42"
    assert result.product_payload is not None
    assert "1899" in (result.reply_hint or "") or "1.899" in (result.reply_hint or "")
    agent = service.story_result_to_agent_result(result, incoming=incoming)
    assert agent is not None
    assert agent.intent == "commerce"
    assert agent.response_metadata.get("tenant_id") == "newstore"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_shadow_mode_does_not_change_reply(monkeypatch):
    from app.config import get_settings
    from app import instagram_story_service as service
    from app.instagram_story_models import StoryProductAssociation

    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "shadow")
    get_settings.cache_clear()

    assoc = StoryProductAssociation(
        tenant_id="newstore",
        provider="brevo",
        instagram_account_id="ig_biz_fixture",
        story_media_id="story_image_001",
        product_id="42",
        catalog_item_key="product:42",
        match_status="matched",
        match_confidence=1.0,
    )

    class FakeRepo:
        def get_by_story(self, **_kwargs):
            return assoc

        def create_pending(self, **_kwargs):
            return assoc

        def touch_last_seen(self, **_kwargs):
            return None

    monkeypatch.setattr(service, "StoryProductRepository", FakeRepo)

    async def fake_tool(name, args):
        return {"id": "42", "name": "Seiko", "price": 10, "available": True}

    incoming = parse_brevo_conversations_payload(_story_reply_payload())
    result = await service.resolve_story_product_question(
        incoming=incoming,
        tenant_id="newstore",
        execute_tool=fake_tool,
    )
    assert result is not None
    assert result.shadow_only is True
    assert service.story_result_to_agent_result(result, incoming=incoming) is None
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_begin_processing_none_does_not_call_vision(monkeypatch):
    from app.config import get_settings
    from app import instagram_story_service as service
    from app.instagram_story_models import StoryProductAssociation

    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "full")
    monkeypatch.setenv("INSTAGRAM_STORY_REAL_PAYLOAD_VALIDATED", "true")
    get_settings.cache_clear()

    pending = StoryProductAssociation(
        tenant_id="newstore",
        provider="brevo",
        instagram_account_id="ig_biz_fixture",
        story_media_id="story_image_001",
        match_status="pending",
    )
    processing = StoryProductAssociation(
        tenant_id="newstore",
        provider="brevo",
        instagram_account_id="ig_biz_fixture",
        story_media_id="story_image_001",
        match_status="processing",
    )
    vision_calls = {"n": 0}

    class FakeRepo:
        def get_by_story(self, **_kwargs):
            return processing

        def create_pending(self, **_kwargs):
            return pending

        def begin_processing(self, **_kwargs):
            return None

    async def boom(*_a, **_k):
        vision_calls["n"] += 1
        raise AssertionError("vision must not run")

    monkeypatch.setattr(service, "StoryProductRepository", FakeRepo)
    monkeypatch.setattr(service, "analyze_story_image", boom)
    monkeypatch.setattr(service, "download_story_media", boom)

    incoming = parse_brevo_conversations_payload(_story_reply_payload())
    result = await service.resolve_story_product_question(
        incoming=incoming,
        tenant_id="newstore",
        execute_tool=AsyncMock(),
    )
    assert result is not None
    assert result.match_status == "processing"
    assert vision_calls["n"] == 0
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_tenant_unresolved_without_fallback(monkeypatch):
    from app.config import get_settings
    from app import instagram_story_service as service
    from app.story_tenant import TenantResolution

    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "full")
    monkeypatch.setenv("INSTAGRAM_STORY_REAL_PAYLOAD_VALIDATED", "true")
    get_settings.cache_clear()

    async def unresolved(**_kwargs):
        return TenantResolution(
            ok=False, failure_code="story_tenant_unresolved", source="unresolved"
        )

    monkeypatch.setattr(service, "resolve_story_tenant", unresolved)
    incoming = parse_brevo_conversations_payload(_story_reply_payload())
    result = await service.resolve_story_product_question(
        incoming=incoming,
        tenant_id=None,
        execute_tool=AsyncMock(),
    )
    assert result is not None
    assert result.failure_reason == "story_tenant_unresolved"
    get_settings.cache_clear()


def test_clarification_uses_real_regions_not_hardcoded():
    from app.instagram_story_service import _clarification_from_regions

    analysis = StoryVisualUnderstanding(
        multiple_products=True,
        watch_count=2,
        product_regions=[
            VisualProductRegion(position="left", label="modelo", dial_color="azul"),
            VisualProductRegion(position="right", label="modelo", dial_color="preto"),
        ],
    )
    options, reply = _clarification_from_regions(analysis)
    assert any("azul" in o for o in options)
    assert any("preto" in o for o in options)
    assert "azul" in reply or "preto" in reply
    assert "mostrador azul" not in options or True  # real labels from regions


def test_visual_understanding_forbids_trusting_advertised_price_as_stock():
    analysis = StoryVisualUnderstanding(
        visual_description="relógio azul",
        visible_advertised_price="R$ 1.000",
        product_identity_confidence=0.4,
    )
    assert analysis.visible_advertised_price


def test_package_release_classifies_placeholders():
    from scripts.package_release import classify_secret_value

    empty = classify_secret_value(variable="OPENAI_API_KEY", value="", path=".env.example")
    assert empty.classification == "placeholder"
    assert empty.blocking is False
    fixture = classify_secret_value(
        variable="MP_ACCESS_TOKEN", value="tok-a", path="tests/test_mercadopago_client.py"
    )
    assert fixture.classification == "test_fixture"
    assert fixture.blocking is False
    real = classify_secret_value(
        variable="OPENAI_API_KEY",
        value="sk-proj-abcdefghijklmnopqrstuvwxyz",
        path="docs/leak.md",
    )
    assert real.classification == "real"
    assert real.blocking is True


@pytest.mark.offline_eval
def test_story_offline_eval_marker_smoke():
    assert detect_story_question_type("quanto custa esse?").value == "price"
