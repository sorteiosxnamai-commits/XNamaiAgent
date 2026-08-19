from app.commerce_context import CommerceConversationState
from app.observability import (
    full_obs_enabled,
    log_event,
    redact_text,
    summarize_commerce_state,
    summarize_history_turns,
    summarize_openai_messages,
    summarize_tray_result,
    summarize_webhook_payload,
)


def test_redact_text_masks_pii_and_secrets():
    text = (
        "CPF 072.810.359-18 email tironinho@hotmail.com "
        "fone 85999498149 token sk-proj-abc123Bearer xyz"
    )
    redacted = redact_text(text)
    assert "072.810.359-18" not in redacted
    assert "tironinho@hotmail.com" not in redacted
    assert "85999498149" not in redacted
    assert "sk-proj-abc123" not in redacted
    assert "[CPF]" in redacted
    assert "[EMAIL]" in redacted


def test_summarize_commerce_and_openai_messages():
    state = CommerceConversationState(
        order_id="0CC131B51070AEF",
        pending_action="awaiting_payment",
        active_product={"product_id": "1", "name": "Seiko"},
        checkout_draft={"customer": {"cpf": "07281035918", "name": "Paulo"}},
    )
    summary = summarize_commerce_state(state)
    assert summary["has_order_id"] is True
    assert summary["order_id"] == "0CC131B51070AEF"
    assert "cpf" in summary["checkout_customer_fields"]
    assert "07281035918" not in str(summary["checkout_customer_fields"])

    messages = summarize_openai_messages(
        [
            {"role": "system", "content": "COMMERCE_STATE"},
            {"role": "user", "content": "meu cpf e 07281035918"},
        ]
    )
    assert messages[1]["preview"].find("[CPF]") >= 0


def test_summarize_tray_result_keeps_useful_flags():
    summary = summarize_tray_result(
        {
            "success": True,
            "order_id": "25400",
            "payment": {"status": "pending", "has_payment": False, "payment_url": "https://x"},
            "products": [{"id": 1}, {"id": 2}],
        }
    )
    assert summary["ok"] is True
    assert summary["order_id"] == "25400"
    assert summary["payment"]["payment_url_present"] is True
    assert summary["products_count"] == 2


def test_summarize_history_turns_includes_previews(monkeypatch):
    monkeypatch.setenv("AGENT_FULL_OBS_LOGS", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        turns = [
            {"role": "user", "content": "quero um seiko"},
            {"role": "assistant", "content": "tenho estas opções"},
            {"role": "user", "content": "manda o link"},
        ]
        summarized = summarize_history_turns(turns, max_turns=10)
        assert len(summarized) == 3
        assert summarized[0]["role"] == "user"
        assert "seiko" in summarized[0]["preview"]
    finally:
        get_settings.cache_clear()


def test_history_preview_omitted_when_full_obs_off(monkeypatch):
    monkeypatch.setenv("AGENT_FULL_OBS_LOGS", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert full_obs_enabled() is False
        summarized = summarize_history_turns(
            [{"role": "user", "content": "cpf 072.810.359-18"}],
            max_turns=10,
        )
        assert summarized[0]["preview"] is None
        assert summarized[0]["chars"] > 0
    finally:
        get_settings.cache_clear()


def test_summarize_webhook_payload_keeps_channel_hints():
    summary = summarize_webhook_payload(
        {
            "eventName": "conversationFragment",
            "conversationId": "c1",
            "visitor": {
                "id": "v1",
                "source": "whatsapp",
                "sourceConversationRef": "ref",
            },
            "message": {"id": "m1", "text": "olá"},
            "messages": [{"id": "m0"}, {"id": "m1"}],
        }
    )
    assert summary["event_name"] == "conversationFragment"
    assert summary["visitor_source"] == "whatsapp"
    assert summary["messages_count"] == 2
    assert summary["message_text_preview"] == "olá"


def test_full_obs_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_FULL_OBS_LOGS", raising=False)
    from app.config import Settings, get_settings

    get_settings.cache_clear()
    try:
        assert Settings().agent_full_obs_logs is False
        assert full_obs_enabled() is False
    finally:
        get_settings.cache_clear()


def test_log_event_emits_agent_obs_json(capsys, monkeypatch):
    monkeypatch.setenv("AGENT_FULL_OBS_LOGS", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    assert full_obs_enabled() is True
    log_event("test.event", {"channel": "whatsapp", "text_preview": "oi"})
    captured = capsys.readouterr().out
    assert "[agent.obs]" in captured
    assert "test.event" in captured
    assert "whatsapp" in captured
    get_settings.cache_clear()
