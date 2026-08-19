"""Honest offline replay suite (Etapa 11) — ``pytest -m offline_eval``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.evals.harness import run_replay_case
from tests.evals.scoring import score_eval_case


SCENARIOS = Path(__file__).parent / "fixtures" / "scenarios_v1.json"


def _load_scenarios() -> list[dict]:
    payload = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    return list(payload.get("cases") or [])


@pytest.mark.offline_eval
@pytest.mark.asyncio
@pytest.mark.parametrize("case", _load_scenarios(), ids=lambda c: c["id"])
async def test_offline_eval_replay_honest(case, monkeypatch):
    """Run agent with fakes; score from observations only (never copy expected)."""
    obs = await run_replay_case(case, monkeypatch)
    result = score_eval_case(
        case,
        observed_domain=obs["observed_domain"],
        observed_tools=obs["observed_tools"],
        reply_text=obs["reply_text"],
        openai_calls=obs["openai_calls"],
        factual_valid=obs["factual_valid"],
        handoff_required=obs["handoff_required"],
        invented_claim=obs["invented_claim"],
    )
    failed = [name for name, ok in result["checks"].items() if not ok]
    assert result["passed"], {
        "id": case.get("id"),
        "failed_checks": failed,
        "score": result["score"],
        "checks": result["checks"],
        "obs": {
            "domain": obs["observed_domain"],
            "tools": obs["observed_tools"],
            "handoff": obs["handoff_required"],
            "intent": obs["intent"],
            "reply_preview": (obs["reply_text"] or "")[:160],
            "openai_calls": obs["openai_calls"],
        },
    }
