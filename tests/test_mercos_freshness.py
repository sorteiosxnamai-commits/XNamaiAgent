"""Politica de freshness: snapshot antigo nunca vira "fato atual".

A regra que estes testes protegem: fora da janela, o fato vira
``unconfirmed`` — nunca ``zero``, nunca ``indisponivel``. Ausencia de
confirmacao e diferente de ausencia do produto.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.commerce.mercos.freshness import (
    DEFAULT_WINDOWS,
    FactFreshness,
    evaluate_freshness,
)

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def _verdict(kind, *, minutes_ago):
    return evaluate_freshness(kind, NOW - timedelta(minutes=minutes_ago), now=NOW)


def test_never_synced_is_unknown_not_zero():
    verdict = evaluate_freshness("stock", None, now=NOW)
    assert verdict.freshness is FactFreshness.UNKNOWN
    assert verdict.can_be_stated_as_current is False
    assert verdict.synced_at is None


@pytest.mark.parametrize("kind", ["identity", "price", "stock"])
def test_just_synced_is_fresh(kind):
    assert _verdict(kind, minutes_ago=0).freshness is FactFreshness.FRESH


def test_stock_expires_fast():
    assert _verdict("stock", minutes_ago=30).freshness is FactFreshness.FRESH
    assert _verdict("stock", minutes_ago=90).freshness is FactFreshness.UNCONFIRMED


def test_price_window_is_longer_than_stock_but_still_bounded():
    assert _verdict("price", minutes_ago=60 * 6).freshness is FactFreshness.FRESH
    assert _verdict("price", minutes_ago=60 * 20).freshness is FactFreshness.UNCONFIRMED


def test_identity_survives_longest():
    assert _verdict("identity", minutes_ago=60 * 24 * 3).freshness is FactFreshness.FRESH
    assert _verdict("identity", minutes_ago=60 * 24 * 10).freshness is FactFreshness.UNCONFIRMED


def test_stock_window_is_the_strictest():
    """Estoque muda o tempo todo: sua janela nunca pode ser a mais folgada."""
    assert DEFAULT_WINDOWS["stock"] <= DEFAULT_WINDOWS["price"]
    assert DEFAULT_WINDOWS["price"] <= DEFAULT_WINDOWS["identity"]


def test_unknown_fact_kind_gets_the_strictest_window():
    """Na duvida, exigir confirmacao — nunca presumir validade longa."""
    strictest = min(DEFAULT_WINDOWS.values()).total_seconds()
    inside = evaluate_freshness(
        "campo_que_nao_existe", NOW - timedelta(seconds=strictest - 60), now=NOW
    )
    outside = evaluate_freshness(
        "campo_que_nao_existe", NOW - timedelta(seconds=strictest + 60), now=NOW
    )
    assert inside.freshness is FactFreshness.FRESH
    assert outside.freshness is FactFreshness.UNCONFIRMED


def test_clock_skew_does_not_grant_infinite_validity():
    """Timestamp no futuro nao pode virar 'sempre fresco'."""
    verdict = evaluate_freshness("stock", NOW + timedelta(hours=5), now=NOW)
    assert verdict.age_seconds == 0
    assert verdict.freshness is FactFreshness.FRESH


def test_naive_timestamp_is_treated_as_utc():
    naive = datetime(2026, 3, 1, 11, 30, 0)
    assert evaluate_freshness("stock", naive, now=NOW).freshness is FactFreshness.FRESH


def test_metadata_carries_no_business_payload():
    metadata = _verdict("price", minutes_ago=10).as_metadata()
    assert set(metadata) == {"fact_kind", "freshness", "synced_at", "age_seconds"}
    assert metadata["freshness"] == "fresh"


def test_expired_fact_is_unconfirmed_not_absent():
    """A distincao que evita a pior resposta: "nao confirmei" != "nao tem"."""
    verdict = _verdict("stock", minutes_ago=60 * 24)
    assert verdict.freshness is FactFreshness.UNCONFIRMED
    assert verdict.freshness is not FactFreshness.UNKNOWN
    assert verdict.synced_at is not None
