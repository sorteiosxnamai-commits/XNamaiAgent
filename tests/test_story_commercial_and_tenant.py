"""Commercial truth policy + tenant principal + payload normalization tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.instagram_story_normalized import (
    StoryEventType,
    normalize_brevo_story_event,
)
from app.request_principal import (
    principal_from_admin_token,
    principal_from_internal,
)
from app.story_commercial_policy import (
    evidence_from_tray_product,
    validate_commercial_answer,
)
from app.story_tenant import resolve_story_tenant

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_story"


@pytest.mark.asyncio
async def test_explicit_tenant_requires_principal():
    result = await resolve_story_tenant(
        provider="brevo",
        instagram_account_id="ig1",
        explicit_tenant_id="tenant_a",
        principal=None,
    )
    assert result.ok is False
    assert result.failure_code == "explicit_requires_principal"


@pytest.mark.asyncio
async def test_principal_cannot_cross_tenant():
    principal = principal_from_admin_token(
        subject_id="admin-1",
        tenant_ids=["tenant_a"],
    )
    result = await resolve_story_tenant(
        provider="brevo",
        instagram_account_id="ig1",
        explicit_tenant_id="tenant_b",
        principal=principal,
    )
    assert result.ok is False
    assert result.failure_code == "tenant_forbidden"


@pytest.mark.asyncio
async def test_internal_principal_allows_explicit():
    principal = principal_from_internal(subject_id="agent", tenant_id="tenant_a")
    result = await resolve_story_tenant(
        provider="brevo",
        instagram_account_id="ig1",
        explicit_tenant_id="tenant_a",
        principal=principal,
    )
    assert result.ok is True
    assert result.tenant_id == "tenant_a"
    assert result.source == "principal"


def test_visual_candidate_cannot_authorize_price():
    evidence = evidence_from_tray_product(
        {"id": "1", "name": "Seiko", "price": 100.0, "stock": 2, "url": "https://loja/p/1"},
        tenant_id="t1",
        source="tray_api",
    ).model_copy(update={"source": "visual_candidate"})
    violations = validate_commercial_answer(
        "Esse modelo custa R$ 100,00 e está disponível.",
        {"id": "1", "tenant_id": "t1"},
        evidence,
        "t1",
    )
    assert "visual_only_commercial_forbidden" in violations or "price_not_authorized" in violations


def test_tray_evidence_allows_matching_price():
    evidence = evidence_from_tray_product(
        {
            "id": "42",
            "name": "Seiko",
            "price": 1899.0,
            "stock": 2,
            "available": True,
            "url": "https://loja/p/42",
        },
        tenant_id="newstore",
        source="tray_api",
    )
    assert evidence.price_cents == 189900
    assert evidence.authorizes_price()
    violations = validate_commercial_answer(
        "Esse é o Seiko. Consultei agora e o valor atual é R$ 1.899,00. Ele está disponível.",
        {"id": "42", "tenant_id": "newstore", "url": "https://loja/p/42"},
        evidence,
        "newstore",
    )
    assert violations == []


def test_price_mismatch_blocked():
    evidence = evidence_from_tray_product(
        {"id": "42", "name": "Seiko", "price": 1899.0, "stock": 1},
        tenant_id="newstore",
        source="tray_api",
    )
    violations = validate_commercial_answer(
        "O valor é R$ 9,99.",
        {"id": "42", "tenant_id": "newstore"},
        evidence,
        "newstore",
    )
    assert "price_differs_from_evidence" in violations


def test_tenant_leak_in_evidence_blocked():
    evidence = evidence_from_tray_product(
        {"id": "42", "name": "Seiko", "price": 10.0},
        tenant_id="tenant_a",
        source="tray_api",
    )
    violations = validate_commercial_answer(
        "Valor R$ 10,00",
        {"id": "42", "tenant_id": "tenant_b"},
        evidence,
        "tenant_b",
    )
    assert "tenant_mismatch" in violations


def test_normalize_brevo_reply_fixture():
    payload = json.loads(
        (FIXTURES / "brevo_story_image_signed_url.json").read_text(encoding="utf-8")
    )
    event = normalize_brevo_story_event(payload)
    assert event.provider == "brevo"
    assert event.event_type == StoryEventType.REPLY_TO_STORY
    assert event.tenant_id is None  # never from payload
    assert event.operational_story_url()
    assert "SECRET" in (event.operational_story_url() or "")
    dumped = event.model_dump(mode="json")
    assert "SECRET" not in json.dumps(dumped)


def test_normalize_malformed_payload():
    event = normalize_brevo_story_event({"messages": "nope"})  # type: ignore[arg-type]
    # dict with bad messages still yields unknown/incomplete rather than crash
    assert event.event_type in {StoryEventType.UNKNOWN, StoryEventType.MALFORMED}


def test_canary_blocked_without_real_payload(monkeypatch):
    from app.config import get_settings
    from app.instagram_story_models import InstagramStoryContext
    from app.instagram_story_service import story_rollout_allows

    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "canary")
    monkeypatch.setenv("INSTAGRAM_STORY_REAL_PAYLOAD_VALIDATED", "false")
    get_settings.cache_clear()
    story = InstagramStoryContext(
        provider="brevo",
        instagram_account_id="ig",
        story_media_id="s1",
        replied_to_story=True,
    )
    ok, reason = story_rollout_allows(tenant_id="t1", story=story, conversation_id="c1")
    assert ok is False
    assert reason == "real_payload_not_validated"
    get_settings.cache_clear()
