"""Offline eval smoke for Story commercial safety (no network)."""

from __future__ import annotations

import pytest

from app.story_commercial_policy import (
    evidence_from_tray_product,
    validate_commercial_answer,
)


@pytest.mark.offline_eval
def test_offline_eval_wrong_price_rate_zero():
    evidence = evidence_from_tray_product(
        {"id": "1", "name": "A", "price": 50.0, "stock": 1},
        tenant_id="t1",
        source="tray_api",
    )
    bad = validate_commercial_answer(
        "Custa R$ 1,00",
        {"id": "1", "tenant_id": "t1"},
        evidence,
        "t1",
    )
    good = validate_commercial_answer(
        "Custa R$ 50,00",
        {"id": "1", "tenant_id": "t1"},
        evidence,
        "t1",
    )
    assert "price_differs_from_evidence" in bad
    assert good == []


@pytest.mark.offline_eval
def test_offline_eval_tenant_leakage_rate_zero():
    evidence = evidence_from_tray_product(
        {"id": "1", "name": "A", "price": 50.0},
        tenant_id="tenant_a",
        source="tray_api",
    )
    leaks = validate_commercial_answer(
        "R$ 50,00",
        {"id": "1", "tenant_id": "tenant_b"},
        evidence,
        "tenant_b",
    )
    assert "tenant_mismatch" in leaks


@pytest.mark.offline_eval
def test_offline_eval_visual_only_never_prices():
    evidence = evidence_from_tray_product(
        {"id": "1", "name": "A", "price": 50.0, "stock": 3},
        tenant_id="t1",
        source="tray_api",
    ).model_copy(update={"source": "visual_candidate"})
    violations = validate_commercial_answer(
        "Esse custa R$ 50,00 e está disponível.",
        {"id": "1", "tenant_id": "t1"},
        evidence,
        "t1",
    )
    assert violations
