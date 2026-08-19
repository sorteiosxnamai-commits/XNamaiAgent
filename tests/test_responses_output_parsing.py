from types import SimpleNamespace

from app.openai_gateway import extract_function_calls, extract_output_text


def test_output_text_prefers_direct_property():
    response = SimpleNamespace(output_text="direto", output=[])
    assert extract_output_text(response) == "direto"


def test_output_text_from_multiple_message_parts():
    response = SimpleNamespace(
        output_text=None,
        output=[
            {"type": "function_call", "call_id": "c1"},
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "A"},
                    {"type": "output_text", "text": "B"},
                ],
            },
        ],
    )
    assert extract_output_text(response) == "AB"


def test_function_call_call_id_not_renamed():
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                call_id="call_abc",
                name="search_products",
                arguments="{}",
            )
        ]
    )
    calls = extract_function_calls(response)
    assert calls[0].call_id == "call_abc"
    assert not hasattr(calls[0], "tool_call_id") or getattr(
        calls[0], "tool_call_id", None
    ) is None
