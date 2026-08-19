"""Offline deterministic eval suite — fixture schema only (no cheat scoring)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures" / "conversations.json"
SCENARIOS = Path(__file__).parent / "fixtures" / "scenarios_v1.json"
ONLINE_EVAL = False


def _load_cases() -> list[dict]:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return list(payload.get("cases") or [])


def _load_scenarios() -> list[dict]:
    payload = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    return list(payload.get("cases") or [])


def test_eval_fixture_has_at_least_50_cases():
    cases = _load_cases()
    assert len(cases) >= 50
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))


def test_scenarios_v1_has_25_cases():
    cases = _load_scenarios()
    assert len(cases) >= 25
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_offline_eval_fixture_schema(case):
    """Validate fixture shape only — does NOT invent observed_* from expected."""
    assert "id" in case
    assert "input" in case or "turns" in case
    expected = case.get("expected") or {}
    assert isinstance(expected, dict)
    assert "must_call_tools" in expected or "domain" in expected or "handoff_required" in expected
    for key in ("must_call_tools", "must_not_call_tools", "must_include", "must_not_include"):
        if key in expected:
            assert isinstance(expected[key], list)


def test_online_eval_disabled_by_default():
    assert ONLINE_EVAL is False
