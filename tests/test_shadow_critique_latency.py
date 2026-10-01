import asyncio
from unittest.mock import AsyncMock

import pytest

from app import response_critique as critique
from app.models import AgentResult, IncomingMessage


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_only_observation_has_short_deadline_and_cancels_the_judge(monkeypatch, mode):
    monkeypatch.setattr(critique, "SHADOW_CRITIQUE_TIMEOUT_SECONDS", .005)
    monkeypatch.setattr(critique, "is_low_risk_judge_skip", lambda *a: (False, "test"))
    monkeypatch.setattr("app.llm_call_policy.should_run_llm_critique", lambda **k: (True, "test", []))
    cancelled = []
    async def judge(**kwargs):
        try:
            await asyncio.sleep(.03)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return critique.CritiqueVerdict(score=100, pass_check=True, summary="Verified")
    monkeypatch.setattr(critique, "run_critique_judge", judge)
    execute = AsyncMock(side_effect=AssertionError("Observation cannot execute operations"))
    result = AgentResult(reply_text="As condições devem ser alinhadas com a equipe.", intent="commerce")
    final, report = await critique.apply_response_critique_loop(incoming=IncomingMessage(text="E agora?"),
        result=result, mode=mode, execute=execute)
    assert final is result and not final.handoff_required and final.safety_reason is None
    if mode == "shadow":
        assert cancelled == [True]
        assert not report.approved
        assert final.response_metadata["response_critique"]["skip_reason"] == "shadow_timeout"
    else:
        assert not cancelled and report.approved
    execute.assert_not_awaited()
