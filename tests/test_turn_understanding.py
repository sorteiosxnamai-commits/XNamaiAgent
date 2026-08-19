"""Tests for unified TurnUnderstanding (Etapa 3)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai.lib._pydantic import to_strict_json_schema

from app.models import IncomingMessage, SalesInterpretation
from app.turn_understanding import (
    Ambiguity,
    ConversationReference,
    ExtractedEntities,
    ProductHardConstraints,
    ProductSoftPreferences,
    RequestedAction,
    TurnUnderstanding,
    apply_clarification_policy,
    looks_like_internal_id,
    sales_to_turn_understanding,
    sanitize_turn_understanding,
    turn_understanding_to_sales,
)


def test_turn_understanding_strict_schema_has_no_default_keywords():
    schema = to_strict_json_schema(TurnUnderstanding)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])

    def _find_defaults(node, path="$"):
        found = []
        if isinstance(node, dict):
            if "default" in node:
                found.append(path)
            for key, child in node.items():
                found.extend(_find_defaults(child, f"{path}.{key}"))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                found.extend(_find_defaults(child, f"{path}[{index}]"))
        return found

    assert _find_defaults(schema) == []


def test_sanitize_strips_claimed_internal_ids():
    understanding = TurnUnderstanding.model_construct(
        primary_intent="commerce_find",
        confidence=0.9,
        language="pt-BR",
        user_goal="",
        entities=ExtractedEntities(
            brand="Casio",
            claimed_product_id="550e8400-e29b-41d4-a716-446655440000",
            claimed_variant_id="prod_123456",
            previously_mentioned_product="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ),
        references=[],
        hard_constraints=ProductHardConstraints(),
        soft_preferences=ProductSoftPreferences(),
        hypotheses=[],
        missing_data=[],
        required_tools=["search_products", "evil_tool"],
        ambiguity=[],
        clarification_required=False,
        answer_strategy="search_catalog",
        references_previous_context=False,
        domain_change_explicit=False,
    )
    cleaned = sanitize_turn_understanding(understanding)
    assert cleaned.entities.claimed_product_id is None
    assert cleaned.entities.claimed_variant_id is None
    assert cleaned.entities.previously_mentioned_product is None
    assert cleaned.entities.brand == "Casio"
    assert cleaned.required_tools == ["search_products"]


def test_looks_like_internal_id():
    assert looks_like_internal_id("550e8400-e29b-41d4-a716-446655440000")
    assert not looks_like_internal_id("MTP-1374D")
    assert not looks_like_internal_id("7891234567890")  # EAN-like handled separately


def test_clarification_suppressed_when_brand_and_budget_present():
    understanding = TurnUnderstanding(
        primary_intent="commerce_recommend",
        confidence=0.95,
        clarification_required=True,
        clarification_question="Qual cor?",
        answer_strategy="clarify",
        hard_constraints=ProductHardConstraints(
            brand="Casio",
            budget_max=500,
        ),
        entities=ExtractedEntities(brand="Casio", budget_max=500, category="relógio"),
        ambiguity=[
            Ambiguity(kind="missing_budget", blocking=True, detail="unnecessary"),
        ],
    )
    result = apply_clarification_policy(
        understanding,
        message_text="quero relógios Casio até R$ 500",
    )
    assert result.clarification_required is False
    assert result.answer_strategy == "search_catalog"
    assert result.ambiguity == []


def test_clarification_required_for_bare_demonstrative():
    understanding = TurnUnderstanding(
        primary_intent="commerce_buy",
        confidence=0.7,
        clarification_required=False,
        entities=ExtractedEntities(demonstrative_terms=["esse"]),
        answer_strategy="answer_directly",
    )
    result = apply_clarification_policy(
        understanding,
        message_text="quero esse",
        has_recoverable_reference=False,
    )
    assert result.clarification_required is True
    assert result.answer_strategy == "clarify"
    assert any(a.kind == "ambiguous_reference" for a in result.ambiguity)


def test_demonstrative_ok_with_recoverable_reference():
    understanding = TurnUnderstanding(
        primary_intent="commerce_buy",
        confidence=0.8,
        entities=ExtractedEntities(demonstrative_terms=["o segundo"]),
        references=[
            ConversationReference(kind="list_position", position=2),
        ],
        answer_strategy="answer_directly",
    )
    result = apply_clarification_policy(
        understanding,
        message_text="quero o segundo",
        has_recoverable_reference=True,
    )
    assert result.clarification_required is False


def test_roundtrip_adapters_preserve_commerce_fields():
    understanding = TurnUnderstanding(
        language="pt-BR",
        primary_intent="commerce_recommend",
        user_goal="relógio feminino até 3000",
        confidence=0.93,
        references_previous_context=True,
        entities=ExtractedEntities(
            brand=None,
            category="relógio",
            gender="feminino",
            budget_max=3000,
        ),
        hard_constraints=ProductHardConstraints(
            category="relógio",
            gender="feminino",
            budget_max=3000,
        ),
        soft_preferences=ProductSoftPreferences(
            recipient="feminino",
            attributes=["feminino"],
        ),
        required_tools=["search_products"],
        answer_strategy="search_catalog",
        clarification_required=False,
        requested_action=RequestedAction(kind="none"),
    )
    sales = turn_understanding_to_sales(understanding)
    assert sales.domain == "commerce"
    assert sales.goal == "recommend"
    assert sales.preferences.budget_max == 3000
    assert sales.preferences.recipient == "feminino"
    assert sales.needs_clarification is False
    assert sales.enough_information_to_search is True
    assert sales._turn_understanding is understanding

    back = sales_to_turn_understanding(sales, message_text="feminino até 3000")
    assert back.primary_intent == "commerce_recommend"
    assert back.hard_constraints.budget_max == 3000


def test_exclusive_marker_maps_to_hard_constraints():
    sales = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={"brand": "Seiko", "product_type": "relógio"},
        preferences={"attributes": ["somente:Seiko"]},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    turn = sales_to_turn_understanding(sales, message_text="somente Seiko")
    assert turn.hard_constraints.brand_exclusive is True
    assert turn.hard_constraints.exact_only is True
    assert turn.hard_constraints.brand == "Seiko"


@pytest.mark.asyncio
async def test_interpret_message_uses_turn_understanding_schema(monkeypatch):
    import app.sales_agent as sales_agent
    from openai_test_utils import install_fake_openai_client

    captured = {}
    parsed = TurnUnderstanding(
        primary_intent="commerce_recommend",
        confidence=0.94,
        user_goal="Casio até 500",
        entities=ExtractedEntities(
            brand="Casio",
            category="relógio",
            budget_max=500,
        ),
        hard_constraints=ProductHardConstraints(
            brand="Casio",
            budget_max=500,
            category="relógio",
        ),
        soft_preferences=ProductSoftPreferences(),
        required_tools=["search_products"],
        answer_strategy="search_catalog",
        clarification_required=False,
        references_previous_context=False,
    )

    class FakeCompletions:
        async def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(parsed=parsed, refusal=None, content="{}")
                    )
                ]
            )

    class FakeResponses:
        async def parse(self, **kwargs):
            captured.update(kwargs)
            captured["via"] = "responses"
            return SimpleNamespace(
                output_parsed=parsed,
                output_text="{}",
                status="completed",
                output=[],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())
            self.responses = FakeResponses()

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-key",
            openai_model="gpt-4.1-mini",
            openai_main_model="gpt-4.1-mini",
            openai_fast_model="gpt-4.1-nano",
            agent_turn_understanding_enabled=True,
        ),
    )
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_api_mode="responses",
            openai_responses_fallback_to_chat=True,
            openai_chat_completions_primary_allowed=False,
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
            openai_reasoning_effort="",
            openai_text_verbosity="",
            openai_max_output_tokens=None,
            openai_timeout_seconds=45.0,
            openai_use_previous_response_id=False,
        ),
    )
    install_fake_openai_client(monkeypatch, FakeClient)

    result = await sales_agent.interpret_message(
        IncomingMessage(text="quero relógios Casio até 500"),
    )
    assert result.domain == "commerce"
    assert result.subject.brand == "Casio"
    assert result.preferences.budget_max == 500
    assert result.needs_clarification is False
    assert result._turn_understanding is not None
    assert result._turn_understanding.primary_intent == "commerce_recommend"
    assert captured.get("via") == "responses" or captured.get("text_format") is TurnUnderstanding or captured.get("response_format") is TurnUnderstanding
    assert captured.get("model") == "gpt-4.1-nano"
