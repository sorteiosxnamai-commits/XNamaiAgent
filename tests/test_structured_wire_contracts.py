import pytest
from openai.lib._pydantic import to_strict_json_schema

from app.memory_models import StructuredAgentTurnEnvelope
from app.response_critique import StructuredCritiqueVerdict, CritiqueVerdict


@pytest.mark.parametrize("model", [StructuredAgentTurnEnvelope, StructuredCritiqueVerdict])
def test_sdk_wire_schema_has_no_open_objects_or_untyped_values(model):
    def check(node):
        if isinstance(node, dict):
            assert node, "Untyped schemas are invalid for this strict contract"
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for key, value in node.items():
                if key in ("properties", "$defs"):
                    for child in value.values():
                        check(child)
                elif key in ("items", "anyOf", "allOf"):
                    check(value)
        elif isinstance(node, list):
            for child in node:
                check(child)
    check(to_strict_json_schema(model))


def test_structured_critique_preserves_arguments_for_existing_validator():
    wire = StructuredCritiqueVerdict(recommended_apis=[{
        "name": "search_products", "arguments": {"query": "relógio", "limit": 3}, "reason": "consulta",
    }])
    domain = CritiqueVerdict.model_validate(wire.model_dump())
    assert domain.recommended_apis[0].arguments["query"] == "relógio"
