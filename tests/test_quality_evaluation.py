from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.quality_evaluation import compare_answer, score_answer
from scripts.evaluate_agent_quality import summarize


@pytest.mark.asyncio
async def test_comparison_does_not_send_expected_answers_to_model():
    persona = NS(tenant_id="xnamai", workspace_id="a", persona_key="commercial",
                 instructions="Atenda com fontes.", metadata={"knowledge_documents": []})
    generate = AsyncMock(return_value=NS(text="Resposta", metrics=NS(input_tokens=20, output_tokens=5)))
    report = await compare_answer(persona, "Pergunta", settings=Settings(), required=["SECRET_EXPECTATION"], generate=generate)
    assert generate.call_count == 2
    assert all("SECRET_EXPECTATION" not in str(call.kwargs["messages"]) for call in generate.call_args_list)
    assert all(not result["score"]["passed_assertions"] for result in report["results"])
    assert summarize([report])["main"]["input_tokens"] == 20


def test_rubric_catches_missing_and_forbidden_claims():
    score = score_answer("Entrega garantida amanhã", required=["confirmar"], forbidden=["garantida"])
    assert not score["passed_assertions"]
    assert score["human_review_required"]


@pytest.mark.asyncio
async def test_contract_probe_uses_wire_schemas_and_reports_failure_without_retry():
    from app.memory_models import StructuredAgentTurnEnvelope
    from app.quality_evaluation import check_structured_contracts
    parse = AsyncMock(side_effect=[NS(parsed=StructuredAgentTurnEnvelope(reply="Olá")), ValueError("invalid")])
    report = await check_structured_contracts(settings=Settings(), parse=parse)
    assert [item["ok"] for item in report["results"]] == [True, False]
    assert report["results"][1]["error_type"] == "ValueError"
    assert parse.call_count == 2
    assert all(call.kwargs["timeout_seconds"] == 15 for call in parse.call_args_list)
