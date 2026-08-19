from app.turn_metrics import build_turn_quality_event, hash_conversation_key
from app.turn_runtime import TurnRuntimeContext


def test_turn_quality_event_redacts_conversation_key():
    runtime = TurnRuntimeContext(
        trace_id="t1",
        conversation_key="whatsapp:5511999999999",
        channel="whatsapp",
    )
    runtime.openai_call_count = 2
    runtime.tray_call_count = 1
    runtime.openai_input_tokens = 10
    runtime.openai_output_tokens = 5
    runtime.openai_api_route = "canary_responses"
    runtime.context_snapshot["cache"] = {"hits": 1, "misses": 2, "stale": 0}
    event = build_turn_quality_event(
        runtime,
        result_metadata={
            "domain": "commerce",
            "response_source": "openai",
            "factual_validation": {"valid": True},
            "quality_judge": {"triggered": False},
            "handoff_required": False,
        },
        intent="product_search",
        model="gpt-4.1-mini",
    )
    assert event["event"] == "turn.quality"
    assert event["conversation_key_hash"] == hash_conversation_key(
        "whatsapp:5511999999999"
    )
    assert "5511999999999" not in str(event)
    assert event["openai_calls"] == 2
    assert event["cache_hits"] == 1
    assert event["cache_misses"] == 2
    assert event["total_tokens"] == 15
