"""Tests for gender/budget preference normalization and retrieval mode."""

from app.models import SalesInterpretation
from app.preference_normalize import (
    detect_gender_label,
    is_gender_only_label,
    normalize_sales_interpretation,
    preference_gender_label,
)
from app.product_retrieval import ProductRetrievalCompiler, preference_gender_tokens


def _base(**kwargs) -> SalesInterpretation:
    data = {
        "domain": "commerce",
        "goal": "discover",
        "subject": {"product_type": "relógio"},
        "preferences": {},
        "information_needed": ["catalog"],
        "references_previous_context": True,
        "enough_information_to_search": False,
        "ready_for_retrieval": False,
        "stop_clarification": False,
        "needs_clarification": True,
        "clarification_question": "Qual estilo?",
        "confidence": 0.9,
    }
    data.update(kwargs)
    return SalesInterpretation.model_validate(data)


def test_detect_gender_from_feminino_ate_3000():
    assert detect_gender_label("feminino até 3000 reais") == "feminino"


def test_normalize_moves_gender_off_model_and_enables_recommendation():
    interpretation = _base(
        subject={"product_type": "relógio", "model": "feminino"},
        preferences={"style": "feminino", "budget_max": 3000},
    )
    normalized = normalize_sales_interpretation(
        interpretation,
        message_text="feminino até 3000 reais",
    )
    assert normalized.subject.model is None
    assert preference_gender_label(normalized) == "feminino"
    assert normalized.preferences.budget_max == 3000
    assert normalized.ready_for_retrieval is True
    assert normalized.needs_clarification is False
    assert normalized.goal == "recommend"

    plan = ProductRetrievalCompiler.compile(normalized)
    assert plan.mode == "recommendation"
    names = [req.name for req in plan.requests if req.name]
    assert any("feminino" in (name or "").lower() for name in names)
    assert preference_gender_tokens(normalized)[0] == "feminino"


def test_gender_only_label_never_exact():
    assert is_gender_only_label("feminino") is True
    assert is_gender_only_label("Seastar") is False

    interpretation = _base(
        subject={"product_type": "relógio", "model": "feminino"},
        preferences={"budget_max": 3000},
        needs_clarification=False,
    )
    normalized = normalize_sales_interpretation(
        interpretation,
        message_text="feminino até 3000",
    )
    plan = ProductRetrievalCompiler.compile(normalized)
    assert plan.mode == "recommendation"
